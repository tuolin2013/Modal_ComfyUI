"""
电商图文层 · 主图 + 详情页

目标：主图提高搜索点击率，详情页提高转化率。
输出规格（淘宝/天猫、拼多多通用，微信小店沿用）：
  - 主图：可选 5 张或 10 张；比例可选 1:1（1440×1440）或 3:4（1440×1920），整套统一一种比例
  - 详情页：固定 15 张 3:4（1440×1920），满足淘宝"高/宽 ≤ 2、宽 ≥ 1440"，按顺序逐张上传
流程：
  1. LLM 图文策划：每张图的作用、文案（标题/副标题/角标/要点）、画面提示词
  2. AI 参考图生图（right.codes 异步绘图接口 /draw/v1/images/generations，参考图放 image 字段，
     提交后轮询 /v1/tasks/{task_id}）：以真实产品图（+ 可选角色定妆照）为参考生成画面，
     提示词要求完全保留产品外形、包装文字、logo 和配色，并为文字预留留白区。
     主图和详情页都按素材库给每张分配参考照片（包装锁外观 + 原料 / 茶汤 / 叶底等内容照片）；
     画面主体是真实产品内容、却没有对口照片的屏先不出图，等商家补拍。
     多张并行（ECOM_CONCURRENCY，默认 4）：各自提交任务、各自轮询，每完成一张就存盘回显
  3. 文字由程序排版叠加，不让 AI 写中文：避免错别字，改字也不用重新生图。
     LLM 输出结构化文案（标题、角标、价格标签、参数表、主题色等）→ HTML/CSS 模板 → 无头 Chromium 截图
     （html_render.py）；Playwright / Chromium 不可用时退回 Pillow 简版排版
  4. 广告法用词检查：拦截极限词、医疗功效宣称和未填写的【占位】

注意：
  - 广告法检查只是关键词初筛，降低风险，不能替代人工/法务审核。
  - AI 参考图编辑仍可能改动包装细节，每张图都要人工核对后再上架。
  - 中转平台若不支持 1440 自定义尺寸，自动退回 1024 尺寸生成再裁切放大到目标尺寸（会略软）。
"""

import base64
import copy
import io
import json
import os
import re
import shutil
import threading
import time
import traceback
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from functools import lru_cache
from pathlib import Path

import requests
from markupsafe import Markup
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat

import asset_library as al
import categories
import html_render
import category_playbook as cp
import positioning as pos
import quality_check as qc
from layer1_planning import RIGHT_CODES_BASE_URL, RIGHT_CODES_LLM_KEY, PLANNING_MODEL, LLM_TIMEOUT
from layer2_visual_assets import RIGHT_CODES_DRAW_KEY

# ── 配置 ──────────────────────────────────────────────────────────────────────
ECOM_DIR = Path(__file__).resolve().parent / "output" / "ecom"
PLAN_PATH = ECOM_DIR / "plan.json"
BASE_DIR = ECOM_DIR / "base"           # AI 生成的无字底图（实拍直出的底图也放这里）
FINAL_DIR = ECOM_DIR / "final"         # 叠加文字后的成品（文件名即上传顺序）

EDIT_MODEL = os.environ.get("ECOM_IMAGE_MODEL", "gpt-image-2.5")
# right.codes 绘图接口（官方文档）：异步提交 → 轮询任务。任务查询是站点级接口，不带 /draw
DRAW_BASE_URL = os.environ.get("RIGHT_CODES_DRAW_URL", "https://www.rightapi.ai/draw/v1")
TASK_URL = f"{RIGHT_CODES_BASE_URL.rstrip('/')}/tasks"   # https://www.rightapi.ai/v1/tasks/{task_id}
POLL_INTERVAL = 5      # 秒
POLL_TIMEOUT = 600     # 单张最长等待秒数
# 同时出几张图：接口是「提交任务 → 轮询」，时间几乎都花在等待上，多张并行提交、各自轮询；平台限流（429）时调小
CONCURRENCY = max(1, int(os.environ.get("ECOM_CONCURRENCY", "4")))
# OpenAI 文档：input_fidelity=high 只在 gpt-image-1 / 1.5 上可用，gpt-image-2 系列需省略
FIDELITY_MODELS = {"gpt-image-1", "gpt-image-1.5"}
MAX_PRODUCT_REFS = 3
MAX_EDIT_REFS = 8

# 成品尺寸（淘宝推荐 ≥1440；拼多多要求宽高 >480，同样满足）
TARGET_SIZES = {"1:1": (1440, 1440), "3:4": (1440, 1920)}
# 先按目标尺寸直接生成（gpt-image-2 支持 16 的倍数自定义尺寸）；不支持时退回标准尺寸再裁切放大
GEN_SIZES = {"1:1": "1440x1440", "3:4": "1440x1920"}
FALLBACK_GEN_SIZES = {"1:1": "1024x1024", "3:4": "1024x1536"}
_custom_size_ok = True                 # 运行中发现不支持自定义尺寸后，后续请求直接用标准尺寸

MAIN_RATIOS = ("1:1", "3:4")           # 主图可选比例（整套统一一种）
MAIN_COUNTS = (5, 10)                  # 主图可选张数
DETAIL_RATIO = "3:4"

# 文字上图的两种方式（生成时可选）：
#   overlay：AI 只出无字底图，中文由程序叠加。不会错字，改字只重排、不花钱；版式较模板化
#   ai     ：把文案写进提示词，由 AI 直接在画面里排版。设计感和整体感更强；
#            但可能错字/漏字/乱码，改一个字也要重新生成画面，需逐张核对
TEXT_MODES = {"overlay": "程序叠字（稳定，改字免费）", "ai": "AI 直接写字（设计感强，可能错字）"}
DEFAULT_TEXT_MODE = "overlay"
# 程序叠字的排版引擎：html = HTML/CSS 模板 + 无头 Chromium（默认）；pil = Pillow 简版（服务器装不了 Chromium 时用）
RENDERER = os.environ.get("ECOM_RENDERER", "html").strip().lower()
_STEP_MARKS = "①②③④⑤⑥⑦⑧⑨⑩"         # 详情页要点以这些序号开头时按「步骤」样式排版

# ── 详情页版式 ────────────────────────────────────────────────────────────────
# standard：标题 + 正文 + 要点（可带参数表）；其余三种是信息图，由程序画（文字准确、改字免费）
DETAIL_LAYOUTS = {
    "standard": "标准（标题 + 正文 + 要点 / 参数表）",
    "steps": "流程步骤（工艺、用法、冲泡方法）",
    "grid": "卖点图标网格",
    "compare": "对比表（不点名竞品）",
}
INFO_LAYOUTS = {"steps", "grid", "compare"}
MAX_COMPARE_ROWS = 4

# 线性图标（24×24，描边绘制）。只从这个白名单里取，模板里按原样嵌入
ICONS = {
    "leaf": '<path d="M5 19c0-8 6-14 14-14 0 8-6 14-14 14z"/><path d="M5 19l8-8"/>',
    "mountain": '<path d="M3 20l6-10 4 6 2-3 6 7z"/>',
    "drop": '<path d="M12 3c4 5 6 8 6 11a6 6 0 0 1-12 0c0-3 2-6 6-11z"/>',
    "cup": '<path d="M5 8h12v5a6 6 0 0 1-12 0z"/><path d="M17 9h2a2 2 0 0 1 0 4h-2"/><path d="M8 3v2M11 3v2M14 3v2"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "thermo": '<path d="M10 14V5a2 2 0 0 1 4 0v9a4 4 0 1 1-4 0z"/><path d="M12 9v6"/>',
    "shield": '<path d="M12 3l8 3v6c0 5-4 8-8 9-4-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
    "box": '<path d="M3 7l9-4 9 4-9 4z"/><path d="M3 7v10l9 4 9-4V7"/><path d="M12 11v10"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5'
           'M17.5 17.5L19 19M5 19l1.5-1.5M17.5 6.5L19 5"/>',
    "flame": '<path d="M12 3c1 4 5 5 5 10a5 5 0 0 1-10 0c0-3 2-4 2-7 1 1 2 2 3 2z"/>',
    "scale": '<path d="M5 20h14l-2-11H7z"/><circle cx="12" cy="6" r="2"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-7 8-7s8 3 8 7"/>',
    "home": '<path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/>',
    "truck": '<path d="M3 6h11v10H3z"/><path d="M14 9h4l3 3v4h-7"/><circle cx="7" cy="17" r="2"/>'
             '<circle cx="17" cy="17" r="2"/>',
    "star": '<path d="M12 3l2.6 5.6 6 .7-4.5 4.1 1.2 6-5.3-3-5.3 3 1.2-6L3.4 9.3l6-.7z"/>',
    "paw": '<ellipse cx="12" cy="15.5" rx="4.3" ry="3.5"/><circle cx="6.6" cy="10" r="2"/>'
           '<circle cx="10.1" cy="6.3" r="2"/><circle cx="14.1" cy="6.3" r="2"/>'
           '<circle cx="17.6" cy="10" r="2"/>',
    "check": '<circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-6"/>',
}
# 没有指定图标时按文字猜（顺序即优先级）
ICON_KEYWORDS = (
    (r"杀青|炒|烘|焙|火", "flame"), (r"晒|日照|阳光|晾", "sun"), (r"温度|℃|水温|\d+\s*度", "thermo"),
    (r"产地|山|海拔|云雾|高原", "mountain"), (r"冲泡|泡|水|溶", "drop"), (r"杯|喝|饮|口感|回甘|甘甜", "cup"),
    (r"叶|茶|原料|植物|天然|嫩", "leaf"), (r"分钟|秒|小时|时间|保质|天", "clock"),
    (r"用量|克|勺|片|每次", "scale"), (r"安全|认证|检测|保障|资质|许可|标准", "shield"),
    (r"包装|规格|盒|罐|袋|礼", "box"), (r"人群|适合|老少|家人|宝宝|宠物", "user"), (r"居家|家里|客厅|卧室", "home"),
    (r"发货|物流|快递|包邮", "truck"), (r"品质|精选|严选|工艺", "star"),
)


def guess_icon(text: str, cat=None) -> str:
    for pattern, name in tuple(getattr(cat, "icon_rules", ()) or ()) + ICON_KEYWORDS:
        if re.search(pattern, str(text or "")):
            return name
    return "check"


def fill_icons(icons, bullets: list[str], cat=None) -> list[str]:
    """每条要点一个图标：给定的不在白名单里就按文字猜（类目自己的规则优先）"""
    icons = list(icons or [])
    return [icons[k] if k < len(icons) and icons[k] in ICONS else guess_icon(b, cat) for k, b in enumerate(bullets)]


def normalize_compare(value) -> dict:
    """对比表 → {"headers": [左列名, 右列名], "rows": [{"name", "left", "right"}]}；接受 dict 或界面文本"""
    if isinstance(value, str):
        return parse_compare(value)
    empty = {"headers": [], "rows": []}
    if not isinstance(value, dict):
        return empty
    headers = [clean_text(h, 12) for h in (value.get("headers") or [])][:2]
    rows = []
    for r in value.get("rows") or []:
        if isinstance(r, dict):
            cells = [r.get("name"), r.get("left"), r.get("right")]
        elif isinstance(r, (list, tuple)):
            cells = list(r)[:3]
        else:
            continue
        cells = [clean_text(c, 20) for c in cells] + ["", "", ""]
        if cells[1] or cells[2]:
            rows.append({"name": cells[0], "left": cells[1], "right": cells[2]})
    if not rows:
        return empty
    if len(headers) != 2 or not all(headers):
        headers = ["普通做法", "本品"]
    return {"headers": headers, "rows": rows[:MAX_COMPARE_ROWS]}


def parse_compare(text) -> dict:
    """界面文本：第一行「左列名｜右列名」（可省略），之后每行「对比项｜左｜右」"""
    headers, rows = [], []
    for i, line in enumerate(l for l in str(text or "").splitlines() if l.strip()):
        parts = [p.strip() for p in re.split(r"[|｜]", line)]
        if i == 0 and len(parts) == 2:
            headers = parts
        elif len(parts) >= 3:
            rows.append(parts[:3])
        elif len(parts) == 2:
            rows.append([""] + parts)
    return normalize_compare({"headers": headers, "rows": rows})


def compare_text(value) -> str:
    cmp = normalize_compare(value)
    if not cmp["rows"]:
        return ""
    return "\n".join(["｜".join(cmp["headers"])] + [f"{r['name']}｜{r['left']}｜{r['right']}" for r in cmp["rows"]])


def category_for(key: str | None):
    """这类目手册的主图 / 详情页生成策略（categories.Category，没专属策略时返回 GENERIC）"""
    return cp.category(key)


def default_layout(role: str, cat=None) -> str:
    if "对比" in str(role):
        return "compare"
    steps = r"方法|步骤|流程"
    if cat is not None and getattr(cat, "step_pattern", ""):
        steps = f"{steps}|{cat.step_pattern}"
    if re.search(steps, str(role)):
        return "steps"
    return "standard"


def normalize_layout(value, item: dict, cat=None) -> str:
    """版式与内容不匹配时退回标准版式（如选了对比表却没有对比行、流程只有 1 步）"""
    layout = str(value or "").strip().lower()
    if layout not in DETAIL_LAYOUTS:
        layout = default_layout(item.get("role", ""), cat)
    if layout in ("steps", "grid") and len(item.get("bullets") or []) < 2:
        return "standard"
    if layout == "compare" and not (item.get("compare") or {}).get("rows"):
        return "standard"
    return layout


def detail_share(item: dict) -> float:
    """详情页顶部留给文字 / 信息图的比例；生成底图和排版用同一个值"""
    if item.get("layout") in INFO_LAYOUTS:
        return 0.55
    return 0.50 if item.get("specs") else 0.35


def layout_choices() -> list[tuple[str, str]]:
    return [(label, key) for key, label in DETAIL_LAYOUTS.items()]

DETAIL_ROLES = [
    "首屏海报：品牌 + 一句核心利益，3 秒内让人想继续往下看",
    "痛点共鸣：说出目标用户的困扰，让人觉得\"说的就是我\"",
    "产品亮相：产品就是解决方案，一句话定位",
    "核心卖点 1：最重要的利益点，配直观画面",
    "核心卖点 2",
    "核心卖点 3",
    "对比：用前/用后或与普通做法对比（不点名、不贬低竞品）",
    "成分/材质/工艺：讲清楚为什么有效",
    "品质证据：检测、认证、专利（编号与数据一律用【占位】）",
    "使用场景 1：典型生活场景中使用产品",
    "使用场景 2 / 适用人群：还有谁、在什么时候适合用",
    "使用方法：2-4 步讲清怎么用",
    "细节特写：质地、做工、包装细节",
    "规格参数：规格、容量/尺寸、包装清单",
    "售后保障与收口：发货、退换、客服承诺，最后推动下单",
]

ECOM_PLATFORMS: dict[str, dict] = {
    "淘宝/天猫": {
        "guide": "搜索流量为主：首图决定点击率，主体大而清晰、只讲一个利益点、与同类目搜索结果拉开差异；"
                 "第 5 张为白底图（部分类目要求）。",
        "main_roles": [
            "点击图：产品主体 + 一个核心利益点，在搜索结果中最抢眼",
            "卖点图：第二核心卖点，可用对比或特写",
            "场景图：真人或生活场景中使用产品",
            "细节/证据图：材质、成分、工艺特写",
            "白底图：纯白背景产品正面，不加任何文字",
            "第三卖点图：另一个打动人的利益点",
            "痛点图：直观呈现用户困扰与产品带来的改变",
            "人群图：适合谁用、什么时候用",
            "规格图：规格、容量/尺寸、包装内含",
            "保障图：品质与售后保障（证书编号等用【占位】）",
        ],
    },
    "拼多多": {
        "guide": "价格与价值驱动：用户比价心强，主图突出划算感（规格、量大、赠品、真实的促销活动）；"
                 "画面明亮饱满，信息直给。不要编造价格和活动。",
        "main_roles": [
            "点击图：产品 + 核心利益点 + 真实促销钩子（价格用【券后价】占位）",
            "价值图：规格、数量、赠品，强调划算",
            "卖点图：最打动人的一个卖点",
            "场景图：生活场景中使用产品",
            "细节图：材质、质地、做工特写",
            "第二卖点图：另一个打动人的利益点",
            "痛点图：直观呈现用户困扰与产品带来的改变",
            "使用方法图：2-3 步讲清怎么用",
            "规格图：规格、容量/尺寸、包装内含",
            "白底图：纯白背景产品正面，不加任何文字",
        ],
    },
    "微信小店": {
        "guide": "社交与私域流量：用户多从朋友圈、视频号、公众号点进来，重信任感和生活感；"
                 "画面自然真实，像朋友分享，避免硬广和夸张。",
        "main_roles": [
            "点击图：产品 + 一个打动人的利益点，干净有质感",
            "场景图：生活化真人场景，有代入感",
            "卖点图：核心卖点直观呈现",
            "信任图：真实使用感受或品质保障（不编造评价和数据）",
            "细节图：材质、质地、做工特写",
            "第二卖点图：另一个打动人的利益点",
            "痛点图：直观呈现用户困扰与产品带来的改变",
            "人群图：适合谁用、什么时候用",
            "规格图：规格、容量/尺寸、包装内含",
            "白底图：纯白背景产品正面，不加任何文字",
        ],
    },
}
DEFAULT_ECOM_PLATFORM = "淘宝/天猫"


# ── 广告法用词检查（关键词初筛）：规则在 quality_check，这里保留旧名字给已有调用 ──────────
BANNED_PATTERNS = qc.BANNED_PATTERNS
RISKY_PATTERNS = qc.RISKY_PATTERNS


def check_compliance(text: str, playbook: str | None = None) -> list[str]:
    """返回问题列表，如 ['禁用词「第一」', '风险宣称「医生认证」', '未填写占位【券后价】']"""
    return qc.check_compliance(text, playbook)


def item_texts(item: dict) -> str:
    return qc.item_texts(item)


def item_issues(item: dict, playbook: str | None = None) -> list[str]:
    return qc.item_issues(item, playbook)


# ── 网络请求 ──────────────────────────────────────────────────────────────────
def _proxies() -> dict:
    """
    系统代理（环境变量优先，没有就读 Windows 注册表 / macOS 系统设置），忽略 NO_PROXY（与其它层一致）。
    显式传给 requests，不再临时删 os.environ 里的 NO_PROXY：环境变量是进程级的，多张图并行请求时会互相覆盖。
    注意 urllib 的 getproxies() 只要设了 NO_PROXY 就不读系统设置，所以这里分两步取
    """
    env = {k: v for k, v in urllib.request.getproxies_environment().items() if k != "no"}
    if env:
        return env
    system = getattr(urllib.request, "getproxies_registry", None) or \
        getattr(urllib.request, "getproxies_macosx_sysconf", None)
    return {k: v for k, v in (system() if system else {}).items() if k != "no"}


def _request(method: str, url: str, retries: int = 3, **kw):
    """对 5xx / 429 限流 / 网络异常重试"""
    resp = None
    for attempt in range(retries):
        try:
            resp = requests.request(method, url, proxies=_proxies(), **kw)
            if (resp.status_code < 500 and resp.status_code != 429) or attempt == retries - 1:
                return resp
            print(f"[电商图] [{attempt + 1}/{retries}] 接口返回 {resp.status_code}，稍后重试...")
        except requests.exceptions.RequestException as e:
            if attempt == retries - 1:
                raise
            print(f"[电商图] [{attempt + 1}/{retries}] 网络异常 ({type(e).__name__})，稍后重试...")
        time.sleep(5 * (attempt + 1))
    return resp


def _chat_json(system: str, user, max_tokens: int = 12000, history: list[dict] | None = None,
               temperature: float = 0.7, model: str = "") -> dict:
    """history：自检时带上前一轮的 assistant 输出和修改要求；user 可以是多模态的 content 列表（图文核对用）"""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}] + (history or [])
    resp = _request(
        "POST", f"{RIGHT_CODES_BASE_URL.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {RIGHT_CODES_LLM_KEY}", "Content-Type": "application/json",
                 "Connection": "close"},
        json={"model": model or PLANNING_MODEL, "temperature": temperature, "max_tokens": max_tokens,
              "response_format": {"type": "json_object"}, "messages": messages},
        timeout=max(LLM_TIMEOUT, 240.0),
    )
    if resp.status_code != 200:
        raise Exception(f"LLM 请求失败 ({resp.status_code}): {resp.text[:500]}")
    raw = resp.json()["choices"][0]["message"]["content"].strip()
    # 个别模型仍会包一层 ```json
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    return json.loads(raw)


def _encode_ref(path: str) -> tuple[bytes, str]:
    """缩到长边 ≤1536 再上传，减小请求体；保留透明通道的用 PNG，其它用高质量 JPEG"""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)  # 手机照片按 EXIF 方向摆正
    img.thumbnail((1536, 1536))
    buf = io.BytesIO()
    if img.mode in ("RGBA", "LA", "P"):
        img.convert("RGBA").save(buf, format="PNG")
        return buf.getvalue(), "image/png"
    img.convert("RGB").save(buf, format="JPEG", quality=92)
    return buf.getvalue(), "image/jpeg"


def _save_image_response(data: dict, out_path: Path):
    item = data["data"][0]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if item.get("b64_json"):
        out_path.write_bytes(base64.b64decode(item["b64_json"]))
    elif item.get("url"):
        # 不用 urlretrieve：它没有超时，图床卡住时这张会一直挂着
        resp = _request("GET", item["url"], timeout=120.0)
        if resp.status_code != 200:
            raise ServiceError(f"下载生成的图片失败 ({resp.status_code}): {_brief_error(resp)}")
        out_path.write_bytes(resp.content)
    else:
        raise ValueError("图像接口响应中既没有 b64_json 也没有 url")


class ServiceError(Exception):
    """接口服务端故障（5xx / 网络不通）：不是这张图的问题，继续跑后面的图也会失败"""


def _brief_error(resp, limit: int = 300) -> str:
    """错误响应摘要：网关返回的 HTML 错误页只取 <title>，避免整页 HTML 刷屏"""
    text = resp.text or ""
    if text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        m = re.search(r"<title>(.*?)</title>", text, re.S | re.I)
        return f"网关错误页「{m.group(1).strip() if m else 'HTML'}」"
    return text[:limit]


def _is_size_error(resp) -> bool:
    return 400 <= resp.status_code < 500 and "size" in resp.text.lower()


class _SizeError(Exception):
    """平台不接受该尺寸（提交时 4xx 或任务失败信息里提到 size）"""


def _auth() -> dict:
    return {"Authorization": f"Bearer {RIGHT_CODES_DRAW_KEY}", "Connection": "close"}


def _submit_draw(prompt: str, refs: list[tuple[bytes, str]], size: str, model: str):
    """按 right.codes 文档提交异步绘图任务：参考图放 image（data URL 数组），async 固定 true；
    没有参考图时（纯生成的屏）不带 image 字段"""
    payload = {"model": model, "prompt": prompt, "n": 1, "size": size, "async": True}
    if refs:
        payload["image"] = [f"data:{mime};base64,{base64.b64encode(b).decode()}" for b, mime in refs]
    if model in FIDELITY_MODELS and refs:
        payload["input_fidelity"] = "high"
    return _request("POST", f"{DRAW_BASE_URL.rstrip('/')}/images/generations",
                    headers={**_auth(), "Content-Type": "application/json"}, json=payload, timeout=120.0)


def _task_error(task: dict) -> str:
    e = task.get("error")
    if isinstance(e, dict):
        return str(e.get("message") or e)
    return str(e or task.get("message") or task)[:300]


def _poll_task(task_id: str) -> dict:
    """轮询 GET /v1/tasks/{task_id}，直到 completed / failed；临时 5xx 或断网继续轮询"""
    deadline = time.time() + POLL_TIMEOUT
    last, last_progress = "未查询到", None
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            resp = _request("GET", f"{TASK_URL}/{task_id}", headers=_auth(), timeout=60.0)
        except requests.exceptions.RequestException as e:
            last = f"网络异常 {type(e).__name__}"
            continue
        if resp.status_code >= 500 or resp.status_code == 429:
            last = f"查询返回 {resp.status_code}"
            continue
        if resp.status_code != 200:
            raise Exception(f"任务查询失败 ({resp.status_code}): {_brief_error(resp)}")
        task = resp.json()
        status = str(task.get("status", "")).lower()
        # 文档：完成后"按提交协议回译"，Images 请求直接返回 {"created", "data": [...]}，可能不带 status
        if status in ("completed", "succeeded", "success") or _has_image_data(task):
            return task
        if not status:
            # 既没 status 也没图片：打印原始响应便于排查（截断，避免 base64 刷屏）
            print(f"[电商图]   任务 {task_id} 未识别的响应：{str(task)[:300]}")
        if status in ("failed", "error", "cancelled", "canceled"):
            msg = _task_error(task)
            if "size" in msg.lower():
                raise _SizeError(msg)
            raise Exception(f"绘图任务失败：{msg}")
        last = status or "未知状态"
        if task.get("progress") != last_progress:
            last_progress = task.get("progress")
            print(f"[电商图]   任务 {task_id} {last} {last_progress if last_progress is not None else ''}")
    raise ServiceError(f"任务 {task_id} 超过 {POLL_TIMEOUT} 秒未完成（最后状态：{last}）")


def _image_sources(data: dict) -> list:
    return [data, data.get("result"), data.get("response"), data.get("output")]


def _has_image_data(data: dict) -> bool:
    return any(isinstance(s, dict) and isinstance(s.get("data"), list) and s["data"]
               for s in _image_sources(data))


def _image_payload(data: dict) -> dict:
    """从提交响应或任务结果里找出 Images 形状的 {"data": [{url|b64_json}]}"""
    for src in _image_sources(data):
        if isinstance(src, dict) and isinstance(src.get("data"), list) and src["data"]:
            return src
    raise ValueError(f"任务已完成但响应里没找到图片：{str(data)[:300]}")


def _draw_once(prompt: str, refs: list[tuple[bytes, str]], size: str, model: str, out_path: Path):
    resp = _submit_draw(prompt, refs, size, model)
    if resp.status_code == 429:
        raise ServiceError(f"绘图接口限流 (429)，已重试 3 次：在 .env 里把 ECOM_CONCURRENCY 调小（当前 {CONCURRENCY}）")
    if resp.status_code >= 500:
        raise ServiceError(f"绘图接口服务端错误 ({resp.status_code}): {_brief_error(resp)}，已重试 3 次")
    if not 200 <= resp.status_code < 300:
        if _is_size_error(resp):
            raise _SizeError(_brief_error(resp))
        raise Exception(f"提交绘图任务失败 ({resp.status_code}): {_brief_error(resp)}")
    data = resp.json()
    if not (isinstance(data.get("data"), list) and data["data"]):  # 平台若直接同步返回了图片就不用轮询
        task_id = data.get("task_id") or data.get("id")
        if not task_id:
            raise Exception(f"提交成功但没拿到 task_id：{str(data)[:300]}")
        print(f"[电商图]   {out_path.stem} 已提交任务 {task_id}，每 {POLL_INTERVAL} 秒查询一次")
        data = _poll_task(task_id)
    _save_image_response(_image_payload(data), out_path)


def edit_image(prompt: str, ref_paths: list[str], out_path: Path, ratio: str, model: str = EDIT_MODEL,
               allow_empty: bool = False) -> Path:
    """以参考图为基础生成 ratio 比例的新图（right.codes 异步绘图接口：提交任务 → 轮询结果）。
    allow_empty：允许不带参考图（纯生成、不出现产品的屏）"""
    global _custom_size_ok
    if not ref_paths and not allow_empty:
        raise ValueError("参考图生图至少需要 1 张产品图")
    refs = [_encode_ref(p) for p in ref_paths[:MAX_EDIT_REFS]]
    size = GEN_SIZES[ratio] if _custom_size_ok else FALLBACK_GEN_SIZES[ratio]

    print(f"[电商图] 生成 {out_path.name} | {model} | {size} | {len(refs)} 张参考图")
    try:
        try:
            _draw_once(prompt, refs, size, model, out_path)
        except _SizeError as e:
            fallback = FALLBACK_GEN_SIZES[ratio]
            if size == fallback:
                raise Exception(f"平台不接受尺寸 {size}：{e}") from e
            print(f"[电商图] 不支持尺寸 {size}（{e}），改用 {fallback} 生成后放大")
            _custom_size_ok = False
            _draw_once(prompt, refs, fallback, model, out_path)
    except requests.exceptions.RequestException as e:
        raise ServiceError(f"连不上绘图接口（{type(e).__name__}），已重试 3 次") from e
    return out_path


# ── 图文策划 ──────────────────────────────────────────────────────────────────
_PLAN_TEMPLATE = """\
你是资深电商视觉策划兼文案，熟悉淘宝/天猫、拼多多、微信小店的主图与详情页。
目标：主图提高搜索点击率，详情页提高转化率，并严格遵守《广告法》。
详情页每屏是一张 3:4 竖图，用户在手机上逐屏下滑浏览。

【文案规则】
1. 只讲用户能感知的利益和结果，少讲参数；一张图只讲一件事。各张主图之间、15 屏详情页之间不要重复同一句话。
2. 主图：headline ≤ 10 字，subline ≤ 16 字，badges 最多 3 个、每个 ≤ 6 字。
   详情页：title ≤ 12 字，body ≤ 40 字，bullets 最多 4 条、每条 ≤ 14 字；要点是操作步骤时用 ①②③④ 开头。
3. 禁用极限词（最好、第一、唯一、顶级、国家级、100%、绝对、永久、XX之王 等），不做医疗功效宣称（治疗、消炎、根治 等）；
   不换个说法暗示功效（如「守护血管」「喝出好睡眠」），画面也不暗示疗效（如用前难受、用后舒展的对比，医院和药品元素）。
4. 不编造价格、销量、检测数据、认证和用户评价；需要具体数字或证书的地方写成【占位】，
   例如【券后价】【检测报告编号】，由商家填写。
5. 白底图不写任何文案（所有文案字段留空）。
6. 版式元素（按需使用，不需要就留空，宁缺毋滥）：
   - tag：小标签 ≤ 4 字。主图放在左上角（如「新品」「现货」，不要与 badges 重复）；
     详情页放在标题上方，标明这一屏讲什么（如「使用方法」「品质保障」）。
   - 主图 price_label + price：只在需要促销钩子的图（如拼多多点击图、价值图）使用；
     price 一律写【券后价】这类占位，不写具体数字；price_label 写「券后价」「到手价」等，≤ 4 字。
   - 详情页 specs：只用于规格参数屏，形如 [{"name": "规格", "value": "..."}]，最多 6 行，
     name ≤ 4 字、value ≤ 16 字；用了 specs 就不要再用 bullets 重复同样的信息，body 也尽量简短。
7. theme_color：整套图的版式主色（#RRGGBB），用于价格标签、角标、强调线和标题强调色。
   以品牌主色为基准，可微调明度和饱和度，让它在画面上醒目、与场景色调协调；不要用接近纯白或纯黑的颜色。
8. 详情页 layout（版式，由程序排版，中文不会错字）。按内容选，不要每屏都用信息图：
   - "standard"：标题 + 正文 + 要点（默认；参数表也用它）
   - "steps"：流程步骤，bullets 写 2-4 步、每步 ≤ 10 字、不带 ①②③（程序自动编号），适合工艺、用法、冲泡方法
   - "grid"：卖点图标网格，bullets 写 2-4 个短卖点、每个 ≤ 8 字，icons 为每条选一个图标
   - "compare"：对比表，compare = {"headers": ["普通做法", "本品"], "rows": [["对比项", "左", "右"], ...]}，
     最多 4 行、每格 ≤ 10 字；左列写「普通做法 / 使用前」，不点名、不贬低竞品，只写可证明的事实
   icons 只能从这些里选：{icon_names}
   15 屏里信息图（steps / grid / compare）建议 3-5 屏，其余用 standard 配场景图。

【画面规则 scene_prompt_en】
1. 用英文描述背景、场景、道具、光线、构图和色调（融入品牌色），以及产品在画面中的位置和大小；
   构图按用户指定的主图比例设计。
2. 产品外观以参考图为准：不要描述产品的形状、包装文字和颜色，避免与真实产品冲突。
3. 为文字预留干净的留白：主图按 text_position 留出上方或下方约 1/3；详情页统一留出上方约 35%，
   有 specs 参数表的那一屏留出上方约一半，信息图（steps / grid / compare）那一屏留出上方约 55%，
   这几屏的画面要简洁、低对比（如浅色桌面、柔和渐变、虚化的场景），产品放在下方。
4. 画面中不要出现任何文字、字母、数字、水印或 logo（产品包装自带的除外）。
5. 需要真人出镜时，在 use_characters 中填角色 id，并在 scene_prompt_en 中描述此人的动作和位置，不描述长相。
6. 15 屏详情页的画面要有变化（特写、场景、平铺、手持、局部），但色调统一，像同一套设计。
7. 场景图要有代入感：按目标人群画像写具体的地点、时间、光线和 2-3 个真实道具
   （如 "{scene_example}"），
   画面像买家自己的生活而不是样板间；有人物时写普通人的日常动作（{action_example}），不对镜头摆拍。
   手持 / 第一人称视角（POV）的画面最容易让人代入。
8. art_direction：整套图共用的视觉设定（英文，≤ 60 词）：主光、台面 / 背景材质与色系、2-3 个反复出现的道具、
   镜头质感（如 "{art_example}"）。
   系统会把它拼进每一张图的提示词，所以各张的 scene_prompt_en 不要和它矛盾，也不用重复它。
   场景图不要出现医院、医生、药品、病床，也不要表现生病难受的人。
9. 详情页每屏后面【】里是程序按商家的实拍素材分配好的出图方式和拍法，照着写：
   - 【实拍直出】这屏直接用商家的实拍照片（放在下方），scene_prompt_en 留空，文案围绕照片里看得见的内容写；
   - 【参考重绘】按给定拍法写 scene_prompt_en，提到的素材照片会作为参考图（内容以照片为准，不要另行描述外观）；
   - 【纯生成】画面里不出现产品和包装。
   只有【实拍直出】的屏可以在文案里写「实拍」「实物图」「产地实景」；其它屏是 AI 画面，不能这样说。
   主图后面的【参考重绘 · 素材：…】同理：那张照片会作为参考图，画面围绕照片里的真实内容来写，不要另行描述它的外观。

输出纯 JSON，不加 markdown：
{
  "theme_color": "#RRGGBB",
  "art_direction": "...",
  "main_images": [
    {"role": "...", "tag": "", "headline": "...", "subline": "...", "badges": ["..."],
     "price_label": "", "price": "",
     "text_position": "top" 或 "bottom", "use_characters": [], "scene_prompt_en": "..."}
  ],
  "detail_sections": [
    {"role": "...", "layout": "standard", "tag": "", "title": "...", "body": "...", "bullets": ["..."],
     "icons": [], "specs": [], "compare": {}, "use_characters": [], "scene_prompt_en": "..."}
  ]
}
""".replace("{icon_names}", " / ".join(ICONS))


def category_prompt_block(cat=None) -> str:
    """策划输入里的【本类目画面要求】：与生图提示词用同一份类目要求，免得 LLM 写出矛盾的画面描述"""
    rules = [str(r).strip() for r in (getattr(cat, "image_rules", ()) or ()) if str(r).strip()]
    return "【本类目画面要求】" + " ".join(rules) if rules else ""


def plan_system(cat=None) -> str:
    """图文策划的提示词：场景 / 动作 / 视觉设定的示例由类目提供，没写的用通用示例"""
    cat = categories.GENERIC if cat is None else cat
    return (_PLAN_TEMPLATE
            .replace("{scene_example}", cat.scene_example)
            .replace("{action_example}", cat.action_example)
            .replace("{art_example}", cat.art_example))


PLAN_SYSTEM = plan_system()          # 通用版；生成时用 plan_system(类目)


def generate_ecom_plan(
    product_name: str,
    usp: list[str],
    brand_color: str,
    platform: str = DEFAULT_ECOM_PLATFORM,
    characters: list[dict] | None = None,
    main_count: int = 5,
    main_ratio: str = "1:1",
    product_context: str = "",
    playbook: str = cp.GENERAL,
    self_review: bool = False,
    library: list[dict] | None = None,
    positioning: str = "",
) -> dict:
    """
    生成主图 + 15 屏详情页的图文策划。
    main_count: 主图张数（5 或 10）；main_ratio: 主图比例（"1:1" 或 "3:4"）
    characters: 可出镜的角色（需已有定妆照）
    playbook: 类目手册 key，决定详情页屏序、类目要点和类目合规词
    self_review: 生成后按评分标准 + 程序检查结果让模型再改一轮（多一次 LLM 调用）
    library: 产品素材库（默认读 asset_library）。有素材时先给每屏分配素材和出图方式，再让 LLM 按分配写文案
    positioning: 产品定位（positioning.POSITIONINGS 的键）；空或「跟随平台」时按平台默认。决定详情页屏序、语气、
                 画面方向和价格策略
    """
    spec = ECOM_PLATFORMS.get(platform, ECOM_PLATFORMS[DEFAULT_ECOM_PLATFORM])
    cat = category_for(playbook)
    main_count = int(main_count) if int(main_count) in MAIN_COUNTS else MAIN_COUNTS[0]
    main_ratio = main_ratio if main_ratio in MAIN_RATIOS else MAIN_RATIOS[0]
    # 平台决定主图有哪几张，类目决定每张拍什么（如莓茶首图照实拍画白霜，宠物补充剂首图放宠物进食）
    roles = cat.main_roles(platform, spec["main_roles"])[:main_count]
    position = pos.resolve(positioning, platform)
    detail_roles_list = pos.detail_roles(cp.detail_roles(DETAIL_ROLES, playbook), position,
                                         cp.get(playbook).get("positioning_plans"))
    lib = al.load_library() if library is None else library
    assets = al.assign(detail_roles_list, lib, cat=cat) if lib else None
    main_assets = al.assign(roles, lib, main=True, cat=cat) if lib else None
    shots = al.plan_shots(detail_roles_list, assets, cat)
    characters = characters or []
    char_ids = {c["id"] for c in characters}
    main_roles = "\n".join(
        f"{i}. {r}{'' if '白底' in r or not main_assets else asset_hint(main_assets[i - 1], '', lib)}"
        for i, r in enumerate(roles, 1))
    detail_roles = "\n".join(f"{i}. {r}{asset_hint(assets[i - 1] if assets else None, shots[i - 1], lib)}"
                             for i, r in enumerate(detail_roles_list, 1))
    product_context = "\n".join(s for s in (product_context, al.label_context(lib)) if s)
    chars_text = "\n".join(
        f"- {c['id']} {c.get('name', '')}（{c.get('role', '')}）：{c.get('appearance_zh', '')}" for c in characters
    ) or "（无，不安排真人出镜，use_characters 一律填 []）"

    user = f"""
商品名称：{product_name}
核心卖点：
{chr(10).join(f'- {u}' for u in usp)}
品牌主色：{brand_color}
{product_context}
{cp.prompt_block(playbook, "ecom")}
{pos.prompt_block(position, "ecom")}
{category_prompt_block(cat)}
目标平台：{platform}
平台特点：{spec['guide']}

可出镜的模特（已有定妆照，与带货视频中是同一个人）：
{chars_text}

请输出 {len(roles)} 张主图（比例 {main_ratio}{'，方图' if main_ratio == '1:1' else '，竖图'}），按顺序对应：
{main_roles}

以及 {len(detail_roles_list)} 屏详情页，按顺序对应：
{detail_roles}
"""

    def build(raw_plan):
        p = attach_assets(_normalize_plan(raw_plan, roles, main_ratio, char_ids, detail_roles_list, cat), assets,
                          shots, main_assets)
        pos.apply_price_policy(p["main_images"], position)
        return p

    system = plan_system(cat)
    raw = _chat_json(system, user)
    plan = build(raw)
    issues = review_plan(plan, usp, playbook, raw.get("art_direction"), lib, product_context)
    reviewed = False
    if self_review:
        try:
            revised = _chat_json(system, user, history=[
                {"role": "assistant", "content": json.dumps(raw, ensure_ascii=False)},
                {"role": "user", "content": qc.revision_request(qc.ECOM_RUBRIC, issues)},
            ], temperature=0.4)
            new_plan = build(revised)
            new_issues = review_plan(new_plan, usp, playbook, revised.get("art_direction"), lib, product_context)
            # 按严重程度比较：改稿引入新的 ❌ 或总分变差（模型偶尔删内容）就保留原稿
            if qc.accept_revision(issues, new_issues):
                raw, plan, issues, reviewed = revised, new_plan, new_issues, True
        except Exception as e:      # 自检失败不影响初稿
            print(f"[电商图] 模型自检失败，保留初稿：{e}")
    # 定位的画面方向由程序拼进视觉设定（不靠 LLM 记得写），整套图都带上
    art = "; ".join(s for s in (clean_art_direction(raw.get("art_direction")), pos.get(position)["visual"]) if s)
    plan["meta"] = {"platform": platform, "product_name": product_name, "brand_color": brand_color,
                    "theme_color": _theme_color(raw.get("theme_color"), brand_color),
                    "art_direction": art[:600], "positioning": position,
                    "main_count": main_count, "main_ratio": main_ratio,
                    "playbook": playbook, "self_reviewed": reviewed, "library_sig": al.signature(lib),
                    "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    plan["quality_issues"] = issues
    return plan


def asset_hint(a: dict | None, shot: str, lib: list[dict]) -> str:
    """策划输入里每屏后面的【出图方式 · 拍法 · 素材】"""
    if not a:
        return f"【拍法：{al.shot_label(shot)}】" if shot else ""
    content = al.find(lib, a.get("content") or "")
    if a["mode"] == "photo":
        return f"【实拍直出：{al.describe(content)}】"
    bits = [al.MODES[a["mode"]]] + ([f"拍法：{al.shot_label(shot)}"] if shot else [])
    if content:
        bits.append(f"素材：{al.describe(content)}")
    if a.get("needs_photo"):
        bits.append(f"待补拍「{al.type_label(a.get('missing') or al.DEFAULT_TYPE)}」，文案只写照片能证实的内容")
    return "【" + " · ".join(bits) + "】"


def attach_assets(plan: dict, assets: list[dict] | None, shots: list[str],
                  main_assets: list[dict] | None = None) -> dict:
    """把素材分配和拍法写进详情页每屏、素材分配写进主图（白底图除外）；实拍直出的屏不需要画面描述"""
    for i, m in enumerate(plan.get("main_images", [])):
        if main_assets and i < len(main_assets) and main_assets[i] and not m.get("white_bg"):
            m["assets"] = dict(main_assets[i])
    for i, d in enumerate(plan.get("detail_sections", [])):
        d["shot"] = shots[i] if i < len(shots) else ""
        if assets and i < len(assets) and assets[i]:
            d["assets"] = dict(assets[i])
            if assets[i]["mode"] == "photo":
                d["scene_prompt_en"] = ""
    return plan


# 只有实拍直出的屏能这样说；AI 画面写「实拍」属于虚假宣传风险
REAL_PHOTO_CLAIM = re.compile(r"实拍|实物图|实物拍摄|产地实景|实景拍摄|真实拍摄|无修图")


def check_assets(plan: dict, lib: list[dict] | None = None) -> list[str]:
    """素材相关的检查：AI 画面却写「实拍」、详情页雷同、缺照片类型"""
    details = plan.get("detail_sections", [])
    issues = []
    for d in details:
        mode = (d.get("assets") or {}).get("mode")
        hit = REAL_PHOTO_CLAIM.search(qc.item_texts(d))
        if hit and mode != "photo":
            issues.append(f"{qc.ERR} {d['id']} 文案写了「{hit.group()}」，但这屏是 AI 画面，"
                          "请改文案或在卡片上换成实拍直出")
    blocked = [d["id"] for d in details if (d.get("assets") or {}).get("needs_photo")]
    if blocked:
        issues.append(f"{qc.WARN} {'、'.join(blocked)} 画面主体是真实的产品内容，素材库里却没有对口的照片："
                      "补拍前这几屏不出图（免得 AI 凭空画），也可以在卡片上手动选出图方式")
    pending = [e["id"] for e in lib or [] if e.get("type_source") == "pending"]
    if pending:
        issues.append(f"{qc.WARN} {'、'.join(pending)} 还没识别类型，分配素材时只能当包装图用；"
                      "点「🤖 自动识别类型」或手动选类型")
    issues += [f"{qc.WARN} {s}" for s in al.check_variety(details)]
    if lib is not None and not lib:
        issues.append(f"{qc.INFO} 素材库是空的：详情页全部是 AI 画面，建议上传包装正反面和产品实拍")
    missing = al.missing_types([d.get("assets") for d in details])
    if missing:
        issues.append(f"{qc.INFO} 建议补拍：{'、'.join(missing)}（补拍后会自动换上）")
    return issues


def reassign_assets(plan: dict, lib: list[dict], keep_manual: bool = True, cat=None) -> dict:
    """
    素材库变了（补拍、改类型、删图）后重新分配详情页素材和拍法，文案不动。
    keep_manual：卡片上手动指定过的屏（source = user）保持不变，且素材还在库里
    返回 {"changed": [屏 id], "issues": [...]}；换了素材的屏下次生成时会重出底图（见 needs_regen）
    """
    cat = plan_category(plan) if cat is None else cat
    details = plan.get("detail_sections", [])
    roles = [d["role"] for d in details]
    ids = {e["id"] for e in lib}

    def manual(items):
        return {i: d["assets"] for i, d in enumerate(items)
                if keep_manual and (d.get("assets") or {}).get("source") == "user"
                and all(r in ids for r in d["assets"].get("refs") or [])}

    changed = []
    mains = [m for m in plan.get("main_images", []) if not m.get("white_bg")]
    fixed = manual(mains)
    main_assets = (al.assign([m["role"] for m in mains], lib, fixed=fixed, main=True, cat=cat) if lib
                   else [None] * len(mains))
    for i, m in enumerate(mains):
        if i in fixed:
            continue
        old = (m.get("assets") or {}).get("refs"), (m.get("assets") or {}).get("mode")
        if main_assets[i]:
            m["assets"] = dict(main_assets[i])
        else:
            m.pop("assets", None)
        if ((m.get("assets") or {}).get("refs"), (m.get("assets") or {}).get("mode")) != old:
            changed.append(m["id"])

    fixed = manual(details)
    assets = al.assign(roles, lib, fixed=fixed, cat=cat) if lib else [None] * len(details)
    shots = al.plan_shots(roles, assets, cat)
    for i, d in enumerate(details):
        old = (d.get("assets") or {}).get("refs"), (d.get("assets") or {}).get("mode"), d.get("shot")
        if i not in fixed:
            if assets[i]:
                d["assets"] = dict(assets[i])
            else:
                d.pop("assets", None)
            d["shot"] = shots[i]
        new = (d.get("assets") or {}).get("refs"), (d.get("assets") or {}).get("mode"), d.get("shot")
        if new != old:
            changed.append(d["id"])
    plan.setdefault("meta", {})["library_sig"] = al.signature(lib)
    return {"changed": changed, "issues": check_assets(plan, lib)}


def sync_assets(plan: dict, lib: list[dict]) -> dict | None:
    """
    策划之后补拍 / 删图 / 改了照片类型（素材库指纹和策划时不同）：重新分配素材，手动指定过的屏不动。
    素材库没变返回 None；变了返回 reassign_assets 的结果
    """
    if not plan.get("main_images") or (plan.get("meta") or {}).get("library_sig") == al.signature(lib):
        return None
    return reassign_assets(plan, lib)


def set_item_asset(item: dict, lib: list[dict], content_id: str = "", mode: str = "", shot: str = "",
                   main: bool = False, cat=None) -> dict:
    """
    卡片上手动改这一屏：内容照片（"" = 不指定）、出图方式（"" = 自动）、拍法。记为 source = user，
    重新分配素材时保持不变。main：主图（不实拍直出、没有拍法）；cat：类目（决定这屏要哪类照片）
    """
    need = al.role_need(item.get("role", ""), main, cat)
    content = al.find(lib, content_id) if content_id else None
    if main and mode == "photo":
        mode = ""
    a = al.plan_one(need, content, al.package_refs(lib, 2), allow_photo=not main,
                    mode=mode if mode in al.MODES else "")
    if content and content.get("type") in need["types"]:
        a["missing"] = ""
    a["source"] = "user"
    item["assets"] = a
    if main:
        return a
    if a["mode"] == "photo":
        item["shot"] = "photo"
    elif shot in al.SHOTS:
        item["shot"] = shot
    elif item.get("shot") in ("", "photo", None):
        item["shot"] = need["shots"][0]
    return a


def clean_art_direction(value) -> str:
    """整套图共用的视觉设定：接受字符串或 {"light": ..., "props": [...]} 这类对象，压成一段英文"""
    if isinstance(value, dict):
        value = "; ".join(f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}"
                          for k, v in value.items() if v)
    elif isinstance(value, list):
        value = "; ".join(map(str, value))
    return re.sub(r"\s+", " ", str(value or "")).strip()[:500]


def review_plan(plan: dict, usp: list[str], playbook: str | None = None, art_direction: str = "",
                lib: list[dict] | None = None, source: str = "") -> list[str]:
    """
    整套图文的硬校验：字数、重复、卖点覆盖 + 逐张的广告法/类目合规（不含【占位】，占位由商家填）
    + 画面描述里暗示医疗场景或疗效的词 + 素材检查（AI 画面写「实拍」、相邻屏雷同、缺照片）
    + 商用标准包：文案里没有依据的说法（source 是产品信息原文）
    """
    issues = qc.check_ecom_plan(plan, usp)
    for it in plan.get("main_images", []) + plan.get("detail_sections", []):
        risky = qc.check_compliance(qc.item_texts(it), playbook, placeholders=False)
        if risky:
            issues.append(f"{qc.ERR} {it['id']} {'、'.join(risky)}")
        visual = qc.check_visual(it.get("scene_prompt_en"))
        if visual:
            issues.append(f"{qc.WARN} {it['id']} {'、'.join(visual)}，画面不要暗示医疗场景或疗效")
    visual = qc.check_visual(clean_art_direction(art_direction))
    if visual:
        issues.append(f"{qc.WARN} 整套视觉设定 {'、'.join(visual)}，画面不要暗示医疗场景或疗效")
    copy = "\n".join(qc.item_texts(it) for it in plan.get("main_images", []) + plan.get("detail_sections", []))
    return issues + check_assets(plan, lib) + qc.check_evidence(copy, source, playbook)


def _str_list(v, limit):
    if isinstance(v, str):
        v = re.split(r"[\n、,，]", v)
    return [str(x).strip() for x in (v or []) if str(x).strip()][:limit]


# 控制字符、零宽字符和双向文本控制符：LLM 偶尔会混进来，渲染出来是乱码或让文字顺序错乱
_INVISIBLE = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]")


def clean_text(value, limit: int) -> str:
    """文案清洗：去不可见字符、合并空白，超过 limit 字截断（兜底防止撑爆版式，正常长度由提示词约束）"""
    s = "" if value is None or isinstance(value, (dict, list)) else str(value)
    s = re.sub(r"\s+", " ", _INVISIBLE.sub(" ", s)).strip()
    return s[:limit].rstrip()


def clean_price(value) -> str:
    """价格只留数字或【占位】本身；模板会自己加 ¥，这里去掉重复的 ¥ / 元"""
    return re.sub(r"^[¥￥]\s*|\s*元$", "", clean_text(value, 16)).strip()


def normalize_specs(value, limit: int = 6) -> list[dict]:
    """参数表：接受 [{"name", "value"}]、["规格：xxx"] 或多行文本「名称：值」，返回 [{"name", "value"}]"""
    if isinstance(value, str):
        value = value.splitlines()
    rows = []
    for r in value if isinstance(value, list) else []:
        if isinstance(r, dict):
            name, val = r.get("name"), r.get("value")
        else:
            s = str(r)
            name, sep, val = s.partition("：") if "：" in s else s.partition(":")
            if not sep:  # 没有「名称：」的整行当作值
                name, val = "", s
        name, val = clean_text(name, 8), clean_text(val, 32)
        if not val:
            name, val = "", name
        if val:
            rows.append({"name": name, "value": val})
    return rows[:limit]


def specs_text(specs) -> str:
    """参数表 → 界面里编辑用的多行文本"""
    return "\n".join(f"{s['name']}：{s['value']}" if s["name"] else s["value"] for s in normalize_specs(specs))


def _theme_color(value, brand_color) -> str:
    """LLM 给的版式主色；无效、接近纯白或纯黑时退回品牌主色"""
    s = str(value or "").strip()
    if re.fullmatch(r"#?[0-9a-fA-F]{6}", s):
        rgb = _parse_color(s)
        if 25 <= _lum(rgb) <= 235:
            return _hex(rgb)
    return _hex(_parse_color(brand_color))


def plan_color(plan: dict) -> tuple[int, int, int]:
    """版式主色：优先用策划里的 theme_color，旧策划没有就用品牌主色"""
    meta = plan.get("meta", {})
    return _parse_color(meta.get("theme_color") or meta.get("brand_color"))


def _normalize_plan(raw: dict, roles: list[str], main_ratio: str, char_ids: set,
                    detail_roles: list[str] | None = None, cat=None) -> dict:
    raw_mains = raw.get("main_images") if isinstance(raw.get("main_images"), list) else []
    raw_details = raw.get("detail_sections") if isinstance(raw.get("detail_sections"), list) else []
    detail_roles = detail_roles or DETAIL_ROLES

    def pick(lst, i):
        return lst[i - 1] if i <= len(lst) and isinstance(lst[i - 1], dict) else {}

    def texts(v, n, limit):
        return [t for t in (clean_text(x, limit) for x in _str_list(v, n)) if t]

    mains = []
    for i, role in enumerate(roles, 1):
        it = pick(raw_mains, i)
        white = "白底" in role
        price = "" if white else clean_price(it.get("price"))
        mains.append({
            "id": f"M{i}", "seq": i, "ratio": main_ratio, "role": role,
            "tag": "" if white else clean_text(it.get("tag"), 8),
            "headline": "" if white else clean_text(it.get("headline"), 24),
            "subline": "" if white else clean_text(it.get("subline"), 32),
            "badges": [] if white else texts(it.get("badges"), 3, 10),
            "price_label": clean_text(it.get("price_label"), 6) if price else "",
            "price": price,
            "text_position": "bottom" if str(it.get("text_position")).lower() == "bottom" else "top",
            "use_characters": [] if white else [c for c in _str_list(it.get("use_characters"), 2) if c in char_ids],
            "scene_prompt_en": str(it.get("scene_prompt_en", "")).strip(),
            "white_bg": white, "base_path": "", "final_path": "",
        })
    details = []
    for i, role in enumerate(detail_roles, 1):
        it = pick(raw_details, i)
        bullets = texts(it.get("bullets"), 4, 28)
        d = {
            "id": f"D{i}", "seq": i, "ratio": DETAIL_RATIO, "role": role,
            "tag": clean_text(it.get("tag"), 8),
            "title": clean_text(it.get("title"), 24),
            "body": clean_text(it.get("body"), 80),
            "bullets": bullets,
            "icons": fill_icons(_str_list(it.get("icons"), 4), bullets, cat),
            "specs": normalize_specs(it.get("specs")),
            "compare": normalize_compare(it.get("compare")),
            "use_characters": [c for c in _str_list(it.get("use_characters"), 2) if c in char_ids],
            "scene_prompt_en": str(it.get("scene_prompt_en", "")).strip(),
            "base_path": "", "final_path": "",
        }
        d["layout"] = normalize_layout(it.get("layout"), d, cat)
        details.append(d)
    return {"main_images": mains, "detail_sections": details}


# ── 策划文件 / 产品图 ─────────────────────────────────────────────────────────
def load_plan() -> dict:
    try:
        plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    # 旧版策划（规格不同）不再兼容，要求重新策划
    if "main_ratio" not in plan.get("meta", {}):
        return {}
    return plan


def save_plan(plan: dict):
    ECOM_DIR.mkdir(parents=True, exist_ok=True)
    PLAN_PATH.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")


def reset_outputs():
    """新策划与旧图无关，清掉旧底图和成品"""
    for d in (BASE_DIR, FINAL_DIR):
        if d.exists():
            shutil.rmtree(d)


EXPORT_DIR = ECOM_DIR / "exports"      # 历次打包的 zip，清空时保留


def clear_all():
    """开始新产品：清掉策划、底图、成品和本页产品图；已打包的 zip（exports/）保留"""
    reset_outputs()
    al.clear_library()
    if PLAN_PATH.exists():
        PLAN_PATH.unlink()


def get_product_images() -> list[str]:
    """锁定产品外观用的包装图（素材库里包装正面优先，最多 MAX_PRODUCT_REFS 张）"""
    return [e["path"] for e in al.package_refs(al.load_library(), MAX_PRODUCT_REFS)]


def product_slots(extra: int = 3) -> list[tuple[int, str, str]]:
    """给视频产品镜头图用的参考：包装图在前，再加最多 extra 张内容照片（干品、冲泡、原料…）。
    [(序号, 路径, 中文说明)]，序号从 1 起"""
    lib = al.load_library()
    pkg = al.package_refs(lib, MAX_PRODUCT_REFS)
    ids = {e["id"] for e in pkg}
    others = [e for e in lib if e["id"] not in ids and e.get("type") not in al.PHOTO_ONLY_TYPES][:extra]
    return [(i, e["path"], al.describe(e)) for i, e in enumerate(pkg + others, 1)]


# ── 素材自动分类：视觉模型给每张照片打类型；包装背面顺带读出标签字段 ─────────────────
TAG_BATCH = 6          # 每次调用最多送几张（图越多越容易漏答、错位）
_TAG_TEMPLATE = f"""\
你是电商美工助理。用户上传了同一个商品的多张照片，请逐张判断类型。类型只能从下面选（填英文 key）：
{al.tag_guide()}

规则：
1. 一张照片只给一个最主要的类型。包装正面清晰可见时优先判为 package_front，即使背景是生活场景。
2. note：用中文写这张照片的内容，≤ 12 字（如 {{note_example}}）。
3. 判为 package_back 的照片，把能看清的标签信息抄进 label_fields（照抄原文，看不清的项不写，绝不推测）：
   可能有的项：产品名称、配料 / 原料组成、添加剂组成、净含量、规格、保质期、贮存条件、生产日期、
   生产许可证编号、执行标准、产地、生产商、适用对象、用法用量、成分分析保证值。其它类型的照片 label_fields 为 {{}}。
只输出 JSON：{{"photos": [{{"index": 1, "type": "...", "note": "...", "label_fields": {{}}}}]}}，index 是照片序号（从 1 起）。"""


def tag_system(cat=None) -> str:
    """照片自动分类的提示词：note 的示例按类目给（莓茶说茶汤，宠物补充剂说宠物进食）"""
    note = (cat.note_example if cat is not None else "") or "「玻璃杯里的琥珀色茶汤」「三袋装礼盒」"
    return _TAG_TEMPLATE.replace("{note_example}", note)


TAG_SYSTEM = tag_system()            # 通用版；识别时用 tag_system(类目)


def classify_photos(entries: list[dict], model: str = "") -> tuple[int, list[str]]:
    """
    给素材库照片打类型（结果直接写回素材库，type_source = "auto"）。返回 (识别成功张数, 失败说明)。
    用户改过类型的照片不要传进来（见 asset_library.needs_tagging）
    """
    done, errors = 0, []
    for start in range(0, len(entries), TAG_BATCH):
        batch = entries[start:start + TAG_BATCH]
        content = [{"type": "text", "text": f"共 {len(batch)} 张照片，按顺序编号 1-{len(batch)}。"}]
        for k, e in enumerate(batch, 1):
            content += [{"type": "text", "text": f"照片 {k}："},
                        {"type": "image_url", "image_url": {"url": _data_url(e["path"])}}]
        try:
            raw = _chat_json(TAG_SYSTEM, content, max_tokens=2000, temperature=0.0, model=model or CHECK_MODEL)
        except Exception as ex:
            errors.append(f"{'、'.join(e['id'] for e in batch)} 识别失败（{type(ex).__name__}: {str(ex)[:120]}）")
            continue
        items = raw.get("photos") if isinstance(raw, dict) else []
        fields = {}
        for k, it in enumerate(items if isinstance(items, list) else []):
            if isinstance(it, dict):
                try:
                    fields[int(it.get("index")) - 1] = it.get("label_fields")
                except (TypeError, ValueError):
                    fields[k] = it.get("label_fields")
        for k, (e, tag) in enumerate(zip(batch, al.normalize_tags(raw, len(batch)))):
            if not tag:
                errors.append(f"{e['id']} 模型没有给出结果")
                continue
            kind, note = tag
            update = {"type": kind, "note": note or e.get("note", "")}
            if kind == "package_back":
                update["label_fields"] = fields.get(k)
            al.update_photo(e["id"], type_source="auto", **update)
            done += 1
    return done, errors


def find_item(plan: dict, kind: str, item_id: str) -> dict | None:
    key = "main_images" if kind == "main" else "detail_sections"
    return next((it for it in plan.get(key, []) if it.get("id") == item_id), None)


def final_filename(kind: str, item: dict) -> str:
    """文件名即上传顺序：main_1x1_01.jpg（或 main_3x4_01.jpg）/ detail_01.jpg"""
    if kind == "main":
        return f"main_{item['ratio'].replace(':', 'x')}_{item['seq']:02d}.jpg"
    return f"detail_{item['seq']:02d}.jpg"


def has_text(item: dict) -> bool:
    return bool(item_texts(item).strip())


def needs_regen(item: dict, text_mode: str, kind: str = "detail", lib: list[dict] | None = None) -> bool:
    """
    底图是否需要重新生成：没有底图；底图的写字方式与当前选择不同（无字的图、实拍直出的图不受影响）；
    或者传了素材库、且这屏换过素材 / 出图方式（base_key 对不上）
    """
    if not (item.get("base_path") and os.path.exists(item["base_path"])):
        return True
    if lib is not None and item.get("base_key", "legacy") != asset_key(item, kind, lib) \
            and not _legacy_main_same(item, kind, lib):
        return True
    if item.get("base_mode") == "photo":
        return False
    return has_text(item) and item.get("base_mode", DEFAULT_TEXT_MODE) != text_mode


# ── 生成画面 ──────────────────────────────────────────────────────────────────
def _legacy_main_same(item: dict, kind: str, lib: list[dict]) -> bool:
    """旧版主图（base_key = legacy）本来就只用包装图出图：现在补记了素材分配、但没用到包装以外的照片，不算换素材"""
    a = item.get("assets") or {}
    content = al.find(lib, a.get("content") or "")
    return kind == "main" and item.get("base_key", "legacy") == "legacy" and a.get("mode") == "reference" \
        and (not content or content.get("type") in al.PACKAGE_TYPES)


def _ai_text_block(item: dict, kind: str, brand_hex: str) -> list[str]:
    """AI 写字模式：把文案和版式要求写进提示词"""
    def q(s):
        return '"' + str(s).replace('"', "'") + '"'

    if kind == "main":
        parts = []
        if item.get("tag"):
            parts.append(f"a small brand-color corner label in the top-left corner {q(item['tag'])}")
        if item.get("headline"):
            parts.append(f"a large bold headline {q(item['headline'])}")
        if item.get("subline"):
            parts.append(f"a smaller subheadline below it {q(item['subline'])}")
        if item.get("price"):
            label = f"{item['price_label']} " if item.get("price_label") else ""
            cur = "¥" if str(item["price"])[:1].isdigit() else ""
            parts.append(f"a bold brand-color price tag {q(label + cur + item['price'])}")
        if item.get("badges"):
            parts.append("a row of rounded pill-shaped tags filled with the brand color, reading "
                         + ", ".join(q(b) for b in item["badges"]))
        area = "top third" if item.get("text_position") != "bottom" else "bottom third"
        align = "centered"
    else:
        parts = []
        layout = item.get("layout") if item.get("layout") in DETAIL_LAYOUTS else "standard"
        bullets = [str(b) for b in item.get("bullets") or []]
        if item.get("tag"):
            parts.append(f"a small brand-color label above the title {q(item['tag'])}")
        if item.get("title"):
            parts.append(f"a large bold title {q(item['title'])} with a short brand-color accent line under it")
        if item.get("body"):
            parts.append(f"a paragraph of body text {q(item['body'])}")
        cmp = normalize_compare(item.get("compare"))
        if layout == "steps" and len(bullets) >= 2:
            steps = [b.lstrip(_STEP_MARKS).strip() for b in bullets]
            parts.append(f"a horizontal {len(steps)}-step process infographic: {len(steps)} rounded translucent "
                         "cards connected by small brand-color arrows, each card with a brand-color numbered circle "
                         "on top, a simple thin line icon and its caption: "
                         + "; ".join(f"step {k}: {q(s)}" for k, s in enumerate(steps, 1)))
        elif layout == "grid" and len(bullets) >= 2:
            cols = 3 if len(bullets) == 3 else 2
            parts.append(f"an icon feature grid with {len(bullets)} rounded translucent cards in {cols} columns, "
                         "each card with a simple thin brand-color line icon and a short caption: "
                         + "; ".join(q(b) for b in bullets))
        elif layout == "compare" and cmp["rows"]:
            left, right = cmp["headers"]
            parts.append(f"a clean comparison table with a rounded translucent background, three columns "
                         f"(item, {q(left)}, {q(right)}); the {q(right)} column is highlighted with the brand color; "
                         "rows: " + "; ".join(q(f"{r['name']} | {r['left']} | {r['right']}") for r in cmp["rows"]))
        elif bullets:
            parts.append("a bullet list: " + "; ".join(q(b) for b in bullets))
        if item.get("specs") and layout == "standard":
            parts.append("a clean two-column specification table with rows: "
                         + "; ".join(q(f"{s['name']} | {s['value']}" if s["name"] else s["value"])
                                     for s in item["specs"]))
        area = f"top {round(detail_share(item) * 100)}%"
        align = "left-aligned with generous margins"
    if not parts:
        return []
    return [
        f"Typeset the following Simplified Chinese text in the {area} of the image, {align}: " + "; ".join(parts) + ".",
        "Render every Chinese character exactly as given: do not add, drop, change, translate or garble any "
        "character, and do not add punctuation that is not given.",
        f"Use clean modern Chinese sans-serif typography (like Source Han Sans / PingFang), clear hierarchy, "
        f"high contrast against the background, brand color {brand_hex} for accents. "
        f"Professional e-commerce poster layout; text must not cover the product.",
    ]


def build_edit_prompt(item: dict, kind: str, n_product: int, chars: list[dict],
                      text_mode: str = DEFAULT_TEXT_MODE, brand_hex: str = "", art_direction: str = "",
                      refs: list[dict] | None = None, content_id: str = "", cat=None) -> str:
    """
    art_direction：整套图共用的视觉设定（plan.meta.art_direction），白底图不用
    refs：素材库里分配给这一屏的参考图条目（包装在前）。None 时按旧方式把 n_product 张都当产品图；
          [] 表示纯生成，画面里不出现产品
    content_id：refs 里哪一张是内容照片（原料、茶汤、叶底…），其余是锁定产品外观的包装图
    """
    ratio = item.get("ratio", "1:1")
    ai_text = _ai_text_block(item, kind, brand_hex or "#C8A882") if text_mode == "ai" else []
    lines = []
    if refs is None:
        if n_product == 1:
            lines.append("Reference image 1 is the real product.")
        else:
            lines.append(f"Reference images 1-{n_product} show the same real product from different angles.")
        has_product = True
    else:
        lines += _ref_lines(refs, content_id, cat)
        n_product = len(refs)
        has_product = any(_is_product_ref(r, content_id) for r in refs)
    if has_product:
        lines.append(
            "Reproduce this product exactly as in the reference: same shape, proportions, packaging text, logo, "
            "colors, materials and label layout. Do not redesign, retype, translate or blur any text printed on it.")
    else:
        lines.append("Do not show the product, its packaging or any branded item in this image.")
    for k, c in enumerate(chars, n_product + 1):
        lines.append(f"Reference image {k} is a portrait of the model {c.get('name', c['id'])}; "
                     f"the person in the image must be this exact person with the same face, hairstyle and outfit.")

    frame = "Square 1:1" if ratio == "1:1" else "Vertical 3:4"
    subject = "product" if has_product else "main subject"
    if item.get("white_bg"):
        lines.append(f"{frame} image. Pure white background (#FFFFFF), product front view centered and filling "
                     f"about 80% of the frame width, soft natural contact shadow only, no props, no people.")
    else:
        if art_direction:
            lines.append(f"Shared art direction for the whole image set (keep consistent): {art_direction}.")
        if al.shot_prompt(item.get("shot", "")):
            lines.append(f"Shot type: {al.shot_prompt(item['shot'])}.")
        lines.append(item.get("scene_prompt_en", ""))
        if cat is not None:
            lines += list(getattr(cat, "image_rules", ()) or ())
        purpose = "the typeset text below" if ai_text else "a text overlay"
        if kind == "main":
            area = "top" if item.get("text_position") != "bottom" else "bottom"
            lines.append(f"{frame} e-commerce main image. Keep the {area} third of the frame as clean, uncluttered "
                         f"background for {purpose}; the {subject} must not overlap that area.")
        else:
            share = detail_share(item)
            what = {"steps": "a step-by-step infographic", "grid": "an icon feature grid",
                    "compare": "a comparison table"}.get(item.get("layout"), "")
            lines.append(f"Vertical 3:4 e-commerce detail page section. Keep the top {round(share * 100)}% of the frame "
                         f"as clean, softly lit, low-contrast, uncluttered background for "
                         f"{what + ' and ' if what and not ai_text else ''}{purpose}; "
                         f"the {subject} sits in the lower part.")
    if ai_text:
        lines += ai_text
        lines.append("Apart from the text specified above and what is printed on the product, "
                     "add no other text, letters, numbers, watermarks or logos.")
    else:
        lines.append("Do not add any text, letters, numbers, watermarks or logos other than what is printed on the product.")
    lines.append("Photorealistic commercial product photography, sharp focus, high detail, accurate colors.")
    return "\n".join(l for l in lines if l)


def _is_product_ref(r: dict, content_id: str = "") -> bool:
    """
    锁定产品外观的参考图：包装类照片，或不是这屏内容照片的参考图（分配时作为包装图带上的；
    照片还没识别类型时 package_refs 会退回其它照片，这些也是商家的产品照）
    """
    return r.get("type") in al.PACKAGE_TYPES or r.get("id") != content_id


# 环境类照片：画面就是那个真实的地方，不能换成别处
_KEEP_SCENE_TYPES = {"raw_material", "lifestyle"}


def _ref_lines(refs: list[dict], content_id: str = "", cat=None) -> list[str]:
    """
    逐张说明参考图是什么、怎么用：包装图锁外观；内容照片里的东西照着画、不凭空捏造，
    产品类内容（干茶、茶汤、叶底）可以换角度和构图，原料 / 产地这类环境照片保留真实环境。
    每张照片怎么描述由类目决定（如宠物补充剂的「使用中」是宠物进食），没写的用通用说明
    """
    keep_scene = getattr(cat, "keep_scene_types", None)
    keep_scene = _KEEP_SCENE_TYPES if keep_scene is None else keep_scene
    lines = []
    for k, r in enumerate(refs, 1):
        default = al.PHOTO_TYPES.get(r.get("type"), al.PHOTO_TYPES[al.DEFAULT_TYPE])[1]
        what = cat.photo_desc(r.get("type"), default) if cat is not None else default
        if r.get("type") in al.PACKAGE_TYPES:
            lines.append(f"Reference image {k} shows {what}.")
        elif _is_product_ref(r, content_id):
            lines.append(f"Reference image {k} is a real photo of the product as sold (its packaging and appearance).")
        elif r.get("type") in keep_scene:
            lines.append(f"Reference image {k} is a real photo of {what}. Depict this real place, its plants, objects "
                         "and colors faithfully; you may change the framing and light for the shot described below, "
                         "but do not invent a different place or different-looking plants.")
        else:
            lines.append(f"Reference image {k} is a real photo of {what}. Keep its true colors, shape, size and "
                         "texture exactly; do not invent a different look. Recompose it for the shot described below; "
                         "do not copy the photo's background or framing.")
    return lines


def render_item(plan: dict, kind: str, item: dict, product_paths: list[str], portraits: dict[str, dict],
                text_mode: str = DEFAULT_TEXT_MODE, library: list[dict] | None = None) -> str:
    """
    生成底图并出成品。portraits: {角色id: 角色dict(含 image_path)}
    text_mode="overlay"：底图无字，再由程序叠字；"ai"：AI 直接把文案画进图里，成品只做裁切缩放
    详情页按 item["assets"] 出图：实拍直出（本地合成、不调接口，文字一律程序叠加）/ 参考重绘 / 纯生成
    """
    text_mode = text_mode if text_mode in TEXT_MODES else DEFAULT_TEXT_MODE
    lib = al.load_library() if library is None else library
    cat = plan_category(plan)
    mode, refs = _resolve_refs(item, kind, lib)
    a = item.get("assets") or {}
    if a.get("needs_photo") and mode != "photo":
        raise NeedsPhoto(f"缺「{al.type_label(a.get('missing') or al.DEFAULT_TYPE)}」实拍照片，没有出图（免得 AI 凭空画）："
                         "在卡片上传这屏的参考照片，或手动选出图方式")
    out = BASE_DIR / f"{item['id']}_base.png"
    item.pop("fidelity", None)      # 新画面，旧的核对结果作废
    if mode == "photo":
        item["base_path"] = str(photo_base(item, kind, refs[0]["path"], out))
        item["base_mode"], item["base_key"] = "photo", asset_key(item, kind, lib)
        return compose_item(plan, kind, item, cat)
    chars = [portraits[c] for c in item.get("use_characters", []) if c in portraits]
    paths = product_paths[:MAX_PRODUCT_REFS] if refs is None else [e["path"] for e in refs]
    if not paths and mode != "generate":
        raise ValueError("参考图生图至少需要 1 张产品图")
    brand_hex = _hex(plan_color(plan))
    art = (plan.get("meta") or {}).get("art_direction", "")
    prompt = build_edit_prompt(item, kind, len(paths), chars, text_mode, brand_hex, art, refs,
                               (item.get("assets") or {}).get("content") or "", cat)
    base = edit_image(prompt, paths + [c["image_path"] for c in chars], out, item.get("ratio", "1:1"),
                      allow_empty=mode == "generate")
    item["base_path"] = str(base)
    item["base_mode"] = text_mode
    item["base_key"] = asset_key(item, kind, lib)
    return compose_item(plan, kind, item, cat)


class NeedsPhoto(Exception):
    """这屏的画面主体是真实的产品内容（原料、茶汤、叶底…），素材库里却没有对口的照片：先不出图，等商家补拍"""


def render_job(plan: dict, kind: str, item: dict, product_paths: list[str], portraits: dict[str, dict],
               text_mode: str, lib: list[dict], force: bool = False, check_fidelity: bool = False) -> str:
    """
    出一张图：底图能复用就只重排文字（"reused"），否则生成底图（"generated"）；check_fidelity 时新生成的主图再核对产品。
    在线程池里跑：item 和 plan 都要是调用方给的副本，写回和存盘由调用方在一个线程里做
    """
    if not force and not needs_regen(item, text_mode, kind, lib):
        compose_item(plan, kind, item)
        return "reused"
    render_item(plan, kind, item, product_paths, portraits, text_mode, lib)
    if check_fidelity and kind == "main" and item.get("base_mode") != "photo":
        check_product_fidelity(item, product_paths)
    return "generated"


def render_many(plan: dict, jobs: list[tuple[str, dict]], product_paths: list[str], portraits: dict[str, dict],
                text_mode: str = DEFAULT_TEXT_MODE, library: list[dict] | None = None, force: bool = False,
                check_fidelity: bool = False, workers: int = CONCURRENCY):
    """
    并行出图的生成器：同时跑 workers 张（各自提交任务、各自轮询），每完成一张就把结果写回 plan 里的那一项，
    再 yield (kind, item, status, 说明)。完成顺序不一定是序号顺序。
    status：generated / reused / needs_photo（缺实拍，没出图）/ service（接口故障）/ failed / skipped（没有尝试）。
    线程里操作的是每张图的副本，plan 只在调用方的线程（这个生成器所在的线程）里改，存盘也由调用方做，
    不会出现一边序列化 plan、一边有线程在改它。
    同一时间只提交 workers 张，跑完一张再补一张：接口连续 2 张服务端故障时，后面的直接不再提交
    """
    lib = al.load_library() if library is None else library
    view = {"meta": copy.deepcopy(plan.get("meta") or {})}       # 线程只读 meta（主题色、视觉设定）
    workers = max(1, int(workers))
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ecom-draw")
    todo, running, service_fails = list(jobs), {}, 0
    try:
        while todo or running:
            while todo and len(running) < workers:
                kind, it = todo.pop(0)
                work = copy.deepcopy(it)
                running[pool.submit(render_job, view, kind, work, product_paths, portraits, text_mode, lib, force,
                                    check_fidelity)] = (kind, it, work)
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for fut in done:
                kind, it, work = running.pop(fut)
                try:
                    status = fut.result()
                except NeedsPhoto as e:
                    yield kind, it, "needs_photo", str(e)
                    continue
                except ServiceError as e:
                    service_fails += 1
                    yield kind, it, "service", str(e)
                    if service_fails >= 2:
                        skipped, todo = todo, []
                        for k, i in skipped:
                            yield k, i, "skipped", ""
                    continue
                except Exception as e:
                    print("".join(traceback.format_exception(e)))
                    yield kind, it, "failed", str(e)[:300]
                    continue
                if status == "generated":
                    service_fails = 0
                it.clear()
                it.update(work)
                yield kind, it, status, ""
    finally:
        # 调用方中途放弃（页面断开）：后面的不再提交；已在跑的线程跑完就结束，结果不再写回
        pool.shutdown(wait=False, cancel_futures=True)


def _resolve_refs(item: dict, kind: str, lib: list[dict]):
    """
    这一屏实际用的素材：返回 (mode, 参考图条目列表或 None)。
    None 表示没有素材分配（旧策划、素材库为空），沿用旧逻辑：调用方传入的包装图全当产品图。
    分配的照片被删了就退一档：实拍直出 → 参考重绘 → 旧逻辑
    """
    if kind == "main" and item.get("white_bg"):
        # 白底主图：素材库里有白底的包装正面就直接用（实物原图最可信），否则让 AI 抠白底
        white = [e for e in al.package_refs(lib, 3)
                 if e.get("type") == "package_front" and (e.get("quality") or {}).get("white_bg")]
        return ("photo", white[:1]) if white else ("reference", None)
    a = item.get("assets")
    if not a:
        return "reference", None
    if kind == "main" and a.get("mode") == "photo":      # 主图不实拍直出（旧数据兜底）
        a = {**a, "mode": "reference"}
    if kind == "main" and a.get("mode") == "generate":   # 主图必须有产品：素材库没有能锁外观的照片时沿用旧逻辑
        return "reference", None
    if a.get("mode") == "generate":
        return "generate", []
    entries = [e for e in (al.find(lib, r) for r in a.get("refs") or []) if e]
    if a.get("mode") == "photo":
        content = al.find(lib, a.get("content") or "")
        if content:
            return "photo", [content]
        entries = [e for e in entries if e.get("type") not in al.PHOTO_ONLY_TYPES]
    return ("reference", entries) if entries else ("reference", None)


def asset_key(item: dict, kind: str, lib: list[dict]) -> str:
    """当前素材分配对应的底图 key；与 item["base_key"] 不同说明换过素材或出图方式，底图要重做"""
    mode, refs = _resolve_refs(item, kind, lib)
    return f"{mode}:{','.join(e['id'] for e in refs)}" if refs is not None else "legacy"


def photo_base(item: dict, kind: str, photo_path: str, out_path: Path) -> Path:
    """
    实拍直出：不调生图接口。照片完整放进画面（不裁切，标签、证书上的字不会丢）。
    详情页：照片放在文字区下方、圆角加阴影，背景是这张照片缩到极小再放大的柔和色块（与照片同色调）；
    主图（白底图）：放在纯白画布中央，占约 80%
    """
    W, H = TARGET_SIZES[item.get("ratio", DETAIL_RATIO)]
    src = ImageOps.exif_transpose(Image.open(photo_path)).convert("RGB")
    if kind == "main":
        canvas = Image.new("RGB", (W, H), "white")
        img = ImageOps.contain(src, (int(W * 0.8), int(H * 0.8)), Image.LANCZOS)
        canvas.paste(img, ((W - img.width) // 2, (H - img.height) // 2))
    else:
        bg = ImageOps.fit(src, (8, 10), Image.LANCZOS).resize((W, H), Image.BICUBIC)
        canvas = Image.blend(bg, Image.new("RGB", (W, H), (246, 244, 240)), 0.55).convert("RGBA")
        m = int(W * 0.06)
        top = int(H * detail_share(item)) + m // 2
        img = ImageOps.contain(src, (W - 2 * m, H - m - top), Image.LANCZOS)
        x, y = (W - img.width) // 2, H - m - img.height
        r, off = int(W * 0.025), int(W * 0.012)
        shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle((x, y + off, x + img.width, y + img.height + off), r,
                                                 fill=(0, 0, 0, 70))
        canvas = Image.alpha_composite(canvas, shadow.filter(ImageFilter.GaussianBlur(off * 1.5)))
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.width - 1, img.height - 1), r, fill=255)
        canvas.paste(img, (x, y), mask)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out_path, format="PNG")
    return out_path


# ── 产品还原度核对（可选）：视觉模型对比实拍图和成品，看包装文字 / logo / 配色是否一致 ─────────
CHECK_MODEL = os.environ.get("ECOM_CHECK_MODEL", PLANNING_MODEL)
FIDELITY_SYSTEM = """\
你是电商图片质检员。用户先给产品实拍图（参考），最后一张是 AI 生成的电商图。
只核对 AI 图里的产品与实拍图是否一致：包装上的品牌名和产品名文字、logo、主色、形状和比例。
不评价构图、背景、模特和美观；AI 图里另外叠加的标题、标签文字不算产品包装，不要核对。
只输出 JSON：{"match": true 或 false, "problems": ["..."]}。problems 用中文，每条一句，最多 3 条；一致时为空数组。"""


def _data_url(path: str, max_side: int = 768) -> str:
    img = Image.open(path).convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def check_product_fidelity(item: dict, product_paths: list[str]) -> dict:
    """
    返回 {"match": bool, "problems": [...]}，写进 item["fidelity"]。每张多一次视觉模型调用。
    核对失败（接口错误、模型不支持图片）时返回 {"match": None, "problems": [原因]}，不影响出图
    """
    final = item.get("final_path") or item.get("base_path")
    refs = [p for p in product_paths[:MAX_PRODUCT_REFS] if p and os.path.exists(p)]
    if not (final and os.path.exists(final) and refs):
        return {"match": None, "problems": ["缺少成品图或产品实拍图，未核对"]}
    content = [{"type": "text", "text": f"前 {len(refs)} 张是产品实拍图，最后 1 张是 AI 生成的电商图。"}]
    content += [{"type": "image_url", "image_url": {"url": _data_url(p)}} for p in refs + [final]]
    try:
        raw = _chat_json(FIDELITY_SYSTEM, content, max_tokens=600, temperature=0.0, model=CHECK_MODEL)
    except Exception as e:
        return {"match": None, "problems": [f"核对失败（{type(e).__name__}: {str(e)[:120]}）"]}
    problems = [clean_text(p, 80) for p in (raw.get("problems") or []) if clean_text(p, 80)][:3]
    match = raw.get("match")
    match = bool(match) if isinstance(match, bool) else not problems
    result = {"match": match and not problems, "problems": problems}
    item["fidelity"] = result
    return result


def fidelity_note(item: dict) -> str:
    """卡片上显示的核对结论；没核对过返回空字符串"""
    f = item.get("fidelity")
    if not f:
        return ""
    if f.get("match") is None:
        return "📦 产品核对未完成：" + "；".join(f.get("problems") or [])
    if f.get("match"):
        return ""
    return "📦 产品与实拍图可能不一致：" + "；".join(f.get("problems") or ["请人工核对"])


# ── 留白区检查：底图生成后量一下文字区干不干净（不调接口、不花钱）────────────────
# 边缘密度：FIND_EDGES 后的平均亮度。纯色≈0，柔和渐变 / 虚化背景 < 6，有清晰物体、纹理时明显更高
CLUTTER_EDGE_LIMIT = 9.0


def text_zone_clutter(kind: str, item: dict, img: Image.Image) -> float:
    """文字区的边缘密度（缩到 360 宽再算，与原图尺寸无关）。先整图滤波再裁切，并去掉 2 像素边框，
    避免滤波在图像边界产生的假边缘"""
    g = img.convert("L")
    g.thumbnail((360, 480))
    W, H = g.size
    _, (x0, y0, x1, y1), _ = _text_zone(kind, item, W, H)
    edges = g.filter(ImageFilter.FIND_EDGES)
    box = (max(x0, 2), max(y0, 2), min(x1, W - 2), min(y1, H - 2))
    return ImageStat.Stat(edges.crop(box)).mean[0]


def clutter_warning(kind: str, item: dict, img: Image.Image) -> str:
    """留白区太乱时的提示；底图干净、没有文字或文字是 AI 画进去的时返回空字符串"""
    if not has_text(item) or item.get("white_bg") or item.get("base_mode") == "ai":
        return ""
    score = text_zone_clutter(kind, item, img)
    if score <= CLUTTER_EDGE_LIMIT:
        return ""
    return f"文字区背景较杂（{score:.1f}），文字可能不清楚或压到物体，建议点「重新生成画面」"


# ── 文字排版 ──────────────────────────────────────────────────────────────────
_FONT_CANDIDATES = {
    True: ["C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/simhei.ttf",
           "/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Medium.ttc",
           "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
           "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"],
    False: ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
            "/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Light.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc", "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"],
}


@lru_cache(maxsize=None)
def _font_path(bold: bool) -> str:
    env = os.environ.get("ECOM_FONT_BOLD" if bold else "ECOM_FONT")
    for p in ([env] if env else []) + _FONT_CANDIDATES[bold]:
        if p and os.path.exists(p):
            return p
    raise FileNotFoundError("未找到中文字体，请在 .env 中设置 ECOM_FONT / ECOM_FONT_BOLD 为 .ttf/.ttc 字体路径")


# 字体对象按字号缓存、多线程共用；FreeType 不保证同一字体被多个线程同时使用是安全的，Pillow 排版时串行
_PIL_TEXT_LOCK = threading.Lock()


@lru_cache(maxsize=64)
def _font(size: int, bold: bool = False):
    return ImageFont.truetype(_font_path(bold), size)


def _parse_color(value) -> tuple[int, int, int]:
    s = str(value or "").strip()
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", s) or re.fullmatch(r"#?([0-9a-fA-F]{3})", s)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    nums = re.findall(r"[\d.]+", s)
    if s.startswith("rgb") and len(nums) >= 3:
        return tuple(int(float(n)) for n in nums[:3])
    return (200, 168, 130)


def _lum(rgb) -> float:
    r, g, b = rgb[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b


_NO_LINE_START = set("，。！？、；：）》」』”’,.!?;:)%")


def _wrap(text: str, font, max_w: float) -> list[str]:
    """按字符折行（中文无空格），避免标点出现在行首"""
    lines = []
    for para in str(text).split("\n"):
        cur = ""
        for ch in para:
            if not cur or font.getlength(cur + ch) <= max_w or ch in _NO_LINE_START:
                cur += ch
            else:
                lines.append(cur)
                cur = ch
        if cur:
            lines.append(cur)
    return lines


def _gradient(img: Image.Image, top: bool, height: int, color, max_alpha: int) -> Image.Image:
    """在顶部/底部叠一条渐隐色带，保证文字可读又不生硬"""
    w, h = img.size
    height = max(1, min(height, h))
    grad = Image.linear_gradient("L").resize((w, height))       # 上 0 → 下 255
    if top:
        grad = ImageOps.invert(grad)                              # 上实下虚
    mask = grad.point(lambda v: v * max_alpha // 255)
    layer = Image.new("RGBA", (w, height), (*color, 0))
    layer.putalpha(mask)
    canvas = Image.new("RGBA", img.size, (0, 0, 0, 0))
    canvas.paste(layer, (0, 0 if top else h - height))
    return Image.alpha_composite(img, canvas)


def _theme(img: Image.Image, box) -> dict:
    """根据文字区域的亮度选深字白底或白字暗底"""
    lum = ImageStat.Stat(img.convert("L").crop(box)).mean[0]
    if lum > 140:
        return {"text": (28, 28, 28), "sub": (70, 70, 70), "band": (255, 255, 255), "shadow": None}
    return {"text": (255, 255, 255), "sub": (235, 235, 235), "band": (0, 0, 0), "shadow": (0, 0, 0, 140)}


def _draw_lines(draw, lines, font, x, y, color, line_h, shadow=None, center_w=None):
    for line in lines:
        lx = x if center_w is None else x + (center_w - font.getlength(line)) / 2
        if shadow:
            draw.text((lx + 2, y + 2), line, font=font, fill=shadow)
        draw.text((lx, y), line, font=font, fill=color)
        y += line_h
    return y


def _load_base(item: dict) -> Image.Image:
    """底图居中裁切到目标比例并缩放到成品尺寸（退回 1024 生成时会在这里放大）"""
    W, H = TARGET_SIZES[item.get("ratio", "1:1")]
    return ImageOps.fit(Image.open(item["base_path"]).convert("RGBA"), (W, H), Image.LANCZOS)


def _save_final(img: Image.Image, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out, format="JPEG", quality=92, optimize=True)
    return out


def _hex(rgb) -> str:
    return "#%02X%02X%02X" % tuple(int(c) for c in rgb[:3])


def _mix(rgb, target, t: float) -> tuple[int, int, int]:
    return tuple(round(a + (b - a) * t) for a, b in zip(rgb[:3], target))


def _palette(img: Image.Image, box, accent) -> tuple[str, dict]:
    """按留白区亮度选浅底深字 / 暗底白字，并配出与主题色协调、对比度够的各处颜色"""
    dark = ImageStat.Stat(img.convert("L").crop(box)).mean[0] <= 140
    common = {"accent": _hex(accent), "on_accent": "#FFFFFF" if _lum(accent) < 170 else "#1C1C1C"}
    if dark:
        accent_text = accent if _lum(accent) >= 120 else _mix(accent, (255, 255, 255), 0.5)
        return "dark", {**common, "accent_text": _hex(accent_text), "head": "#FFFFFF", "text": "#FFFFFF",
                        "sub": "rgba(255, 255, 255, .9)", "scrim": "0, 0, 0", "panel": "16, 16, 16",
                        "line": "255, 255, 255"}
    accent_text = accent if _lum(accent) < 140 else _mix(accent, (0, 0, 0), 0.45)
    head = accent if _lum(accent) < 150 else (28, 28, 28)   # 主题色够深才用作标题色，保证对比度
    return "light", {**common, "accent_text": _hex(accent_text), "head": _hex(head), "text": "#1C1C1C",
                     "sub": "#464646", "scrim": "255, 255, 255", "panel": "255, 255, 255", "line": "0, 0, 0"}


def _font_uri(bold: bool) -> str:
    """ECOM_FONT / ECOM_FONT_BOLD 或自动找到的系统字体；都没有时模板退回浏览器自带的中文字体"""
    try:
        return Path(_font_path(bold)).as_uri()
    except FileNotFoundError:
        return ""


def _text_zone(kind: str, item: dict, W: int, H: int) -> tuple[str, tuple, int]:
    """
    文字区 = 生成底图时要求预留的留白区。返回 (位置, 判断明暗用的区域, 文字块最大高度)。
    最大高度比留白区略宽松（渐隐色带会盖住边缘），超出后先自动缩字，仍放不下才给排版提示
    """
    if kind == "main":
        top = item.get("text_position") != "bottom"
        third = H // 3
        return ("top" if top else "bottom"), ((0, 0, W, third) if top else (0, H - third, W, H)), int(H * 0.40)
    share = detail_share(item)
    return "top", (0, 0, W, int(H * share)), int(H * (share + 0.05))


def _template_data(kind: str, item: dict, img: Image.Image, accent, cat=None) -> dict:
    """plan 里的文案 → 模板变量（文字原样交给模板，由 Jinja2 自动转义）"""
    W, H = img.size
    position, box, zone_max = _text_zone(kind, item, W, H)
    tone, colors = _palette(img, box, accent)
    data = {"tone": tone, "position": position, "colors": colors, "zone_max": zone_max,
            "font_regular": _font_uri(False), "font_bold": _font_uri(True),
            "tag": clean_text(item.get("tag"), 40)}
    if kind == "main":
        price = clean_price(item.get("price"))
        data.update(headline=clean_text(item.get("headline"), 200), subline=clean_text(item.get("subline"), 200),
                    badges=[b for b in (clean_text(x, 40) for x in item.get("badges", [])[:3]) if b],
                    price=price, price_label=clean_text(item.get("price_label"), 40) if price else "")
        return data
    bullets = []
    for b in (clean_text(x, 200) for x in item.get("bullets", [])[:4]):
        if b:
            num = b[0] if b[0] in _STEP_MARKS else ""
            bullets.append({"num": num, "text": b[1:].strip() if num else b})
    short = len(bullets) == 4 and max(len(b["text"]) for b in bullets) <= 9   # 短要点排两列，少占留白区
    layout = item.get("layout") if item.get("layout") in DETAIL_LAYOUTS else "standard"
    # 信息图：步骤由模板自动编号；图标只从白名单 ICONS 取（开发者写的固定 SVG，不含 LLM 文本），
    # 所以可以标记为安全 HTML 原样嵌入，文案仍由 Jinja2 自动转义
    icons = fill_icons(item.get("icons"), [b["text"] for b in bullets], cat)
    data.update(title=clean_text(item.get("title"), 200), body=clean_text(item.get("body"), 400),
                bullets=bullets, bullet_cols=2 if short else 1, specs=normalize_specs(item.get("specs")),
                layout=layout, compare=normalize_compare(item.get("compare")),
                cells=[{"text": b["text"], "icon": Markup(ICONS[ic])} for b, ic in zip(bullets, icons)])
    return data


_fallback_noted = False


def _compose(kind: str, item: dict, brand, out: Path, cat=None) -> Path:
    """HTML 模板排版；Playwright / Chromium 不可用或出错时退回 Pillow 简版排版。排版提示写进 item"""
    global _fallback_noted
    img = _load_base(item)
    item["layout_warnings"], item["renderer"] = [], ""
    if not has_text(item):
        return _save_final(img, out)
    clutter = clutter_warning(kind, item, img)

    reason = ""
    if RENDERER != "pil":
        try:
            final, warnings = html_render.render(f"{kind}.html", _template_data(kind, item, img, brand, cat), img)
            item["layout_warnings"], item["renderer"] = warnings + ([clutter] if clutter else []), "html"
            return _save_final(final, out)
        except html_render.RendererUnavailable as e:
            reason = str(e)
        except Exception as e:
            reason = f"HTML 排版出错（{type(e).__name__}: {str(e)[:200]}）"
        if not _fallback_noted:
            print(f"[电商图] {reason}，改用 Pillow 简版排版（不支持角标、价格标签的样式和参数表的表格样式）")
            _fallback_noted = True

    with _PIL_TEXT_LOCK:
        out = (_pil_compose_main if kind == "main" else _pil_compose_detail)(item, brand, out, img)
    item["renderer"] = "pil"
    if reason:
        item["layout_warnings"] = [f"已退回简版排版：{reason}"]
    if clutter:
        item["layout_warnings"] = item["layout_warnings"] + [clutter]
    return out


def compose_main(item: dict, brand, out: Path, cat=None) -> Path:
    return _compose("main", item, brand, out, cat)


def compose_detail(item: dict, brand, out: Path, cat=None) -> Path:
    return _compose("detail", item, brand, out, cat)


# ── Pillow 简版排版（无 Chromium 时的兜底）─────────────────────────────────────
def _pil_compose_main(item: dict, brand, out: Path, img: Image.Image) -> Path:
    W, H = img.size
    if not (item.get("headline") or item.get("subline") or item.get("badges") or item.get("price")):
        return _save_final(img, out)

    top = item.get("text_position") != "bottom"
    margin, max_w = int(W * 0.07), W * 0.86
    f_head, f_sub, f_badge = _font(int(W * 0.085), True), _font(int(W * 0.045)), _font(int(W * 0.036), True)
    head = _wrap(item.get("headline", ""), f_head, max_w)[:2]
    sub = _wrap(item.get("subline", ""), f_sub, max_w)[:2]
    price = clean_price(item.get("price"))
    cur = "¥" if price[:1].isdigit() else ""
    price_chip = [f"{clean_text(item.get('price_label'), 40)} {cur}{price}".strip()] if price else []
    badges = (price_chip + item.get("badges", []))[:3]
    lh_head, lh_sub = int(f_head.size * 1.25), int(f_sub.size * 1.35)
    pad_x, pad_y, gap = int(W * 0.022), int(W * 0.010), int(W * 0.02)
    b_widths = [f_badge.getlength(b) + pad_x * 2 for b in badges]
    b_h = f_badge.size + pad_y * 2
    b_total = sum(b_widths) + gap * max(len(badges) - 1, 0)

    block_h = len(head) * lh_head + len(sub) * lh_sub + ((b_h + int(W * 0.03)) if badges else 0)
    y0 = margin if top else H - margin - block_h
    theme = _theme(img, (0, max(0, y0 - margin), W, min(H, y0 + block_h + margin)))
    img = _gradient(img, top, block_h + margin * 3, theme["band"], 175)

    draw = ImageDraw.Draw(img)
    # 深字时标题用品牌色（品牌色够深才用，保证对比度）
    head_color = brand if theme["shadow"] is None and _lum(brand) < 150 else theme["text"]
    y = _draw_lines(draw, head, f_head, 0, y0, head_color, lh_head, theme["shadow"], W)
    y = _draw_lines(draw, sub, f_sub, 0, y, theme["sub"], lh_sub, theme["shadow"], W)
    if badges:
        y += int(W * 0.03)
        x = (W - b_total) / 2
        badge_text = (255, 255, 255) if _lum(brand) < 170 else (28, 28, 28)
        for b, bw in zip(badges, b_widths):
            draw.rounded_rectangle((x, y, x + bw, y + b_h), radius=b_h // 2, fill=(*brand, 255))
            draw.text((x + pad_x, y + pad_y - f_badge.size * 0.08), b, font=f_badge, fill=badge_text)
            x += bw + gap
    return _save_final(img, out)


def _pil_compose_detail(item: dict, brand, out: Path, img: Image.Image) -> Path:
    W, H = img.size

    margin, max_w = int(W * 0.08), W * 0.84
    f_title, f_body, f_bullet = _font(int(W * 0.068), True), _font(int(W * 0.036)), _font(int(W * 0.036))
    title = _wrap(item.get("title", ""), f_title, max_w)[:2]
    body = _wrap(item.get("body", ""), f_body, max_w)[:4]
    points = item.get("bullets", [])[:4] + specs_text(item.get("specs")).splitlines()   # 参数表退化为要点
    cmp = normalize_compare(item.get("compare")) if item.get("layout") == "compare" else {"rows": []}
    points += [f"{r['name']}：{r['right']}" if r["name"] else r["right"] for r in cmp["rows"]]   # 对比表只留本品一列
    bullets = [l for b in points[:6] for l in _wrap("· " + b, f_bullet, max_w)[:2]]
    lh_t, lh_b = int(f_title.size * 1.3), int(f_body.size * 1.6)
    accent_h, accent_gap = max(3, int(W * 0.008)), int(W * 0.035)

    block_h = len(title) * lh_t + (accent_h + accent_gap * 2 if title else 0) + len(body) * lh_b \
        + (int(W * 0.02) if bullets else 0) + len(bullets) * lh_b
    if not block_h:
        return _save_final(img, out)

    theme = _theme(img, (0, 0, W, min(H, margin * 2 + block_h)))
    img = _gradient(img, True, max(int(H * 0.40), block_h + margin * 3), theme["band"], 200)
    draw = ImageDraw.Draw(img)
    head_color = brand if theme["shadow"] is None and _lum(brand) < 150 else theme["text"]

    y = _draw_lines(draw, title, f_title, margin, margin, head_color, lh_t, theme["shadow"])
    if title:
        y += accent_gap
        draw.rectangle((margin, y, margin + int(W * 0.08), y + accent_h), fill=(*brand, 255))
        y += accent_h + accent_gap
    y = _draw_lines(draw, body, f_body, margin, y, theme["sub"], lh_b, theme["shadow"])
    if bullets:
        y += int(W * 0.02)
        _draw_lines(draw, bullets, f_bullet, margin, y, theme["text"], lh_b, theme["shadow"])
    return _save_final(img, out)


def plan_category(plan: dict):
    """策划文件对应的生成策略（plan.meta.playbook → categories.Category）"""
    return category_for((plan.get("meta") or {}).get("playbook"))


def compose_item(plan: dict, kind: str, item: dict, cat=None) -> str:
    """只重排文字（改字后用，不重新调用生图接口）"""
    if not (item.get("base_path") and os.path.exists(item["base_path"])):
        raise FileNotFoundError(f"{item['id']} 还没有底图，请先生成")
    brand = plan_color(plan)
    cat = cat if cat is not None else plan_category(plan)
    out = FINAL_DIR / final_filename(kind, item)
    if item.get("base_mode") == "ai":
        # AI 已把文字画进图里，只裁切缩放到成品尺寸，不再叠字
        item["layout_warnings"], item["renderer"] = [], ""
        out = _save_final(_load_base(item), out)
    elif kind == "main":
        out = compose_main(item, brand, out, cat)
    else:
        out = compose_detail(item, brand, out, cat)
    item["final_path"] = str(out)
    return str(out)
