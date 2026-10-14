"""
HTML/CSS 模板排版：主图 / 详情页「程序叠字」的渲染引擎

流程：Jinja2 把结构化文案填进 templates/ecom/*.html → Playwright 无头 Chromium 打开 → 截图
  - 底图由调用方先裁切缩放到成品尺寸，页面里按 1:1 像素铺底，不经浏览器缩放，画质与原来一致
  - 只放行 file:// 本地资源（底图、字体），其余请求一律拦截，渲染全程离线
  - 模板开启自动转义：LLM / 用户写的文案只会被当作文字，不会被当作 HTML 或脚本执行
  - 浏览器由一个专用后台线程持有（Gradio 每次回调可能在不同线程），渲染任务排队交给它；
    首次启动约 1 秒，之后每张约 0.5 秒
  - 该线程自建事件循环跑 Playwright 异步 API：modal 等库 import 时会把全局策略设成
    WindowsSelectorEventLoopPolicy，Selector 循环在 Windows 上不能开子进程，Chromium 会起不来；
    这里在 Windows 上固定用 Proactor 循环，且不改全局策略
  - 没装 playwright、Chromium 未下载或起不来时抛 RendererUnavailable，由调用方退回 Pillow 排版
"""

import asyncio
import atexit
import io
import queue
import sys
import tempfile
import threading
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from PIL import Image

try:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import async_playwright
except ImportError:  # 未安装 playwright：render() 抛 RendererUnavailable，调用方退回 Pillow
    async_playwright = None
    PlaywrightError = Exception

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "ecom"
RENDER_TIMEOUT = 90          # 单张最长等待秒数（含排队和首次启动浏览器）
PAGE_TIMEOUT_MS = 20_000     # 页面内单个操作（打开、排版、截图）的超时

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
    undefined=StrictUndefined,   # 模板用到但没传的字段直接报错，不静默留空
    trim_blocks=True,
    lstrip_blocks=True,
)


class RendererUnavailable(RuntimeError):
    """playwright 未安装 / Chromium 未下载或无法启动"""


def render_html(template: str, data: dict) -> str:
    """只填模板、不截图（测试和排查版式用）"""
    return _env.get_template(template).render(**data)


def render(template: str, data: dict, base_img: Image.Image) -> tuple[Image.Image, list[str]]:
    """
    以 base_img 为背景渲染模板，返回 (同尺寸的 RGB 图, 排版提示)。
    排版提示如「主标题超过 2 行」「文字超出预留的留白区」，由模板里的 ecomLayout() 自动缩字后仍放不下时给出。
    data 里的文字会被自动转义；模板另外拿到 width / height / base_src。
    """
    if async_playwright is None:
        raise RendererUnavailable("未安装 playwright，请运行 pip install -r requirements.txt")
    w, h = base_img.size
    with tempfile.TemporaryDirectory(prefix="ecom_html_", ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        base_img.convert("RGB").save(tmp / "base.png", compress_level=1)
        page_file = tmp / "index.html"
        page_file.write_text(render_html(template, {**data, "width": w, "height": h, "base_src": "base.png"}),
                             encoding="utf-8")
        png, report = _worker.run(lambda browser: _screenshot(browser, page_file.as_uri(), w, h))
    warnings = [str(m) for m in report.get("warnings", [])]
    return Image.open(io.BytesIO(png)).convert("RGB"), list(dict.fromkeys(warnings))


async def _allow_local_only(route):
    if route.request.url.startswith("file:"):
        await route.continue_()
    else:
        await route.abort()


async def _screenshot(browser, url: str, w: int, h: int) -> tuple[bytes, dict]:
    page = await browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=1)
    try:
        page.set_default_timeout(PAGE_TIMEOUT_MS)
        # 通过模块属性取处理函数，测试可以替换它来观察被拦截的请求
        await page.route("**/*", lambda route: _allow_local_only(route))
        await page.goto(url, wait_until="load")
        # 模板里的 ecomLayout()：等字体和底图就绪 → 超出留白区的文字自动缩小 → 返回提示
        report = await page.evaluate("() => window.ecomLayout ? window.ecomLayout() : {}") or {}
        return await page.screenshot(type="png"), report
    finally:
        await page.close()


class _BrowserWorker:
    """唯一持有 Playwright / Chromium 的后台线程，任务按提交顺序串行执行"""

    def __init__(self):
        self._jobs: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._pw = None
        self._browser = None
        self.unavailable = ""  # 启动失败的原因。失败后不再反复尝试，装好 Chromium 后重启界面即可

    def run(self, fn, timeout: float = RENDER_TIMEOUT):
        if self.unavailable:
            raise RendererUnavailable(self.unavailable)
        fut: Future = Future()
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="ecom-html-render", daemon=True)
                self._thread.start()
            self._jobs.put((fn, fut))
        try:
            return fut.result(timeout)
        except FutureTimeout:
            fut.cancel()  # 还在排队就取消；已在执行的跑完后结果丢弃
            raise TimeoutError(f"HTML 排版超过 {timeout:.0f} 秒未完成") from None

    def close(self, timeout: float = 10):
        with self._lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                return
            self._jobs.put(None)
        thread.join(timeout)

    def _loop(self):
        loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            while (job := self._jobs.get()) is not None:
                fn, fut = job
                if not fut.set_running_or_notify_cancel():
                    continue  # 调用方已超时放弃
                try:
                    fut.set_result(loop.run_until_complete(self._call(fn)))
                except BaseException as e:
                    fut.set_exception(e)
        finally:
            loop.run_until_complete(self._shutdown())
            loop.close()

    async def _call(self, fn):
        for attempt in (1, 2):
            browser = await self._ensure_browser()
            try:
                return await fn(browser)
            except PlaywrightError:
                # 浏览器进程崩溃 / 断开：重启后重试一次；页面自身的错误（超时等）直接抛出
                if attempt == 2 or browser.is_connected():
                    raise
                print("[电商图] Chromium 已断开，重新启动后重试")
                await self._shutdown()

    async def _ensure_browser(self):
        if self.unavailable:
            raise RendererUnavailable(self.unavailable)
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        try:
            if self._pw is None:
                self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch()
            return self._browser
        except Exception as e:
            first_line = (str(e).strip().splitlines() or [""])[0][:200]
            self.unavailable = (f"Chromium 无法启动（{type(e).__name__}: {first_line}）。"
                                "请运行 python -m playwright install chromium 后重启界面")
            await self._shutdown()
            raise RendererUnavailable(self.unavailable) from e

    async def _shutdown(self):
        browser, pw = self._browser, self._pw
        self._browser = self._pw = None
        for stop in (browser.close if browser else None, pw.stop if pw else None):
            if stop:
                try:
                    await stop()
                except Exception:
                    pass


_worker = _BrowserWorker()
atexit.register(_worker.close)
