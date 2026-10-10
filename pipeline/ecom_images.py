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
     提示词要求完全保留产品外形、包装文字、logo 和配色，并为文字预留留白区
  3. 文字由程序排版叠加（Pillow），不让 AI 写中文：避免错别字，改字也不用重新生图
  4. 广告法用词检查：拦截极限词、医疗功效宣称和未填写的【占位】

注意：
  - 广告法检查只是关键词初筛，降低风险，不能替代人工/法务审核。
  - AI 参考图编辑仍可能改动包装细节，每张图都要人工核对后再上架。
  - 中转平台若不支持 1440 自定义尺寸，自动退回 1024 尺寸生成再裁切放大到目标尺寸（会略软）。
"""

import base64
import io
import json
import os
import re
import shutil
import time
import urllib.request
from functools import lru_cache
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageStat

from layer1_planning import RIGHT_CODES_BASE_URL, RIGHT_CODES_LLM_KEY, PLANNING_MODEL, LLM_TIMEOUT
from layer2_visual_assets import RIGHT_CODES_DRAW_KEY

# ── 配置 ──────────────────────────────────────────────────────────────────────
ECOM_DIR = Path(__file__).resolve().parent / "output" / "ecom"
PLAN_PATH = ECOM_DIR / "plan.json"
PRODUCT_DIR = ECOM_DIR / "product"     # 电商图专用的产品原图
BASE_DIR = ECOM_DIR / "base"           # AI 生成的无字底图
FINAL_DIR = ECOM_DIR / "final"         # 叠加文字后的成品（文件名即上传顺序）

EDIT_MODEL = os.environ.get("ECOM_IMAGE_MODEL", "gpt-image-2.5")
# right.codes 绘图接口（官方文档）：异步提交 → 轮询任务。任务查询是站点级接口，不带 /draw
DRAW_BASE_URL = os.environ.get("RIGHT_CODES_DRAW_URL", "https://www.rightapi.ai/draw/v1")
TASK_URL = f"{RIGHT_CODES_BASE_URL.rstrip('/')}/tasks"   # https://www.rightapi.ai/v1/tasks/{task_id}
POLL_INTERVAL = 5      # 秒
POLL_TIMEOUT = 600     # 单张最长等待秒数
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


# ── 广告法用词检查（关键词初筛）─────────────────────────────────────────────────
BANNED_PATTERNS = [
    r"最[佳好高低优强大新先美全牛]", r"第一", r"唯一", r"首[个家选款]", r"顶级", r"顶尖", r"极致", r"极品",
    r"国家级", r"世界级", r"全球[首领]", r"全网[最第]", r"销量冠军", r"100\s*%", r"百分之百", r"绝对",
    r"永久", r"万能", r"无敌", r"史无前例", r"独家", r"王牌", r"至尊", r"NO\.?\s*1",
]
RISKY_PATTERNS = [
    r"治疗|治愈|根治|疗效|药到病除", r"消炎|抗炎|杀菌|抑菌|抗菌", r"无副作用|零副作用|无毒",
    r"祛斑|美白|防脱|生发|抗敏", r"减肥|瘦身|燃脂", r"医生推荐|医院|临床证明|医生认证|专家推荐",
    r"零添加|纯天然", r"立即见效|立刻见效|\d+\s*[天秒分钟]+见效",
]


def check_compliance(text: str) -> list[str]:
    """返回问题列表，如 ['禁用词「第一」', '风险宣称「医生认证」', '未填写占位【券后价】']"""
    text = str(text or "")
    issues = []
    for p in BANNED_PATTERNS:
        issues += [f"禁用词「{m}」" for m in re.findall(p, text, flags=re.I)]
    for p in RISKY_PATTERNS:
        issues += [f"风险宣称「{m}」" for m in re.findall(p, text)]
    issues += [f"未填写占位{m}" for m in re.findall(r"【[^】]*】", text)]
    return list(dict.fromkeys(issues))


def item_texts(item: dict) -> str:
    parts = [item.get(k, "") for k in ("headline", "subline", "title", "body")]
    parts += item.get("badges", []) + item.get("bullets", [])
    return "\n".join(str(p) for p in parts if p)


def item_issues(item: dict) -> list[str]:
    return check_compliance(item_texts(item))


# ── 网络请求 ──────────────────────────────────────────────────────────────────
def _request(send, retries: int = 3):
    """临时移除 NO_PROXY（与其它层一致），并对 5xx / 网络异常重试"""
    resp = None
    for attempt in range(retries):
        old = os.environ.pop("NO_PROXY", None)
        old_l = os.environ.pop("no_proxy", None)
        try:
            resp = send()
            if resp.status_code < 500 or attempt == retries - 1:
                return resp
            print(f"[电商图] [{attempt + 1}/{retries}] 接口返回 {resp.status_code}，5 秒后重试...")
        except requests.exceptions.RequestException as e:
            if attempt == retries - 1:
                raise
            print(f"[电商图] [{attempt + 1}/{retries}] 网络异常 ({type(e).__name__})，5 秒后重试...")
        finally:
            if old is not None:
                os.environ["NO_PROXY"] = old
            if old_l is not None:
                os.environ["no_proxy"] = old_l
        time.sleep(5)
    return resp


def _chat_json(system: str, user: str, max_tokens: int = 12000) -> dict:
    resp = _request(lambda: requests.post(
        f"{RIGHT_CODES_BASE_URL.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {RIGHT_CODES_LLM_KEY}", "Content-Type": "application/json",
                 "Connection": "close"},
        json={"model": PLANNING_MODEL, "temperature": 0.7, "max_tokens": max_tokens,
              "response_format": {"type": "json_object"},
              "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
        timeout=max(LLM_TIMEOUT, 240.0),
    ))
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
        urllib.request.urlretrieve(item["url"], str(out_path))
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
    """按 right.codes 文档提交异步绘图任务：参考图放 image（data URL 数组），async 固定 true"""
    payload = {
        "model": model, "prompt": prompt, "n": 1, "size": size, "async": True,
        "image": [f"data:{mime};base64,{base64.b64encode(b).decode()}" for b, mime in refs],
    }
    if model in FIDELITY_MODELS:
        payload["input_fidelity"] = "high"
    return _request(lambda: requests.post(
        f"{DRAW_BASE_URL.rstrip('/')}/images/generations",
        headers={**_auth(), "Content-Type": "application/json"}, json=payload, timeout=120.0,
    ))


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
            resp = _request(lambda: requests.get(f"{TASK_URL}/{task_id}", headers=_auth(), timeout=60.0))
        except requests.exceptions.RequestException as e:
            last = f"网络异常 {type(e).__name__}"
            continue
        if resp.status_code >= 500:
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
        print(f"[电商图]   已提交任务 {task_id}，每 {POLL_INTERVAL} 秒查询一次")
        data = _poll_task(task_id)
    _save_image_response(_image_payload(data), out_path)


def edit_image(prompt: str, ref_paths: list[str], out_path: Path, ratio: str, model: str = EDIT_MODEL) -> Path:
    """以参考图为基础生成 ratio 比例的新图（right.codes 异步绘图接口：提交任务 → 轮询结果）"""
    global _custom_size_ok
    if not ref_paths:
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
PLAN_SYSTEM = """\
你是资深电商视觉策划兼文案，熟悉淘宝/天猫、拼多多、微信小店的主图与详情页。
目标：主图提高搜索点击率，详情页提高转化率，并严格遵守《广告法》。
详情页每屏是一张 3:4 竖图，用户在手机上逐屏下滑浏览。

【文案规则】
1. 只讲用户能感知的利益和结果，少讲参数；一张图只讲一件事。各张主图之间、15 屏详情页之间不要重复同一句话。
2. 主图：headline ≤ 10 字，subline ≤ 16 字，badges 最多 3 个、每个 ≤ 6 字。
   详情页：title ≤ 12 字，body ≤ 40 字，bullets 最多 4 条、每条 ≤ 14 字。
3. 禁用极限词（最好、第一、唯一、顶级、国家级、100%、绝对、永久 等），不做医疗功效宣称（治疗、消炎、根治 等）。
4. 不编造价格、销量、检测数据、认证和用户评价；需要具体数字或证书的地方写成【占位】，
   例如【券后价】【检测报告编号】，由商家填写。
5. 白底图不写任何文案（headline、subline、badges 留空）。

【画面规则 scene_prompt_en】
1. 用英文描述背景、场景、道具、光线、构图和色调（融入品牌色），以及产品在画面中的位置和大小；
   构图按用户指定的主图比例设计。
2. 产品外观以参考图为准：不要描述产品的形状、包装文字和颜色，避免与真实产品冲突。
3. 为文字预留干净的留白：主图按 text_position 留出上方或下方约 1/3；详情页统一留出上方约 35%。
4. 画面中不要出现任何文字、字母、数字、水印或 logo（产品包装自带的除外）。
5. 需要真人出镜时，在 use_characters 中填角色 id，并在 scene_prompt_en 中描述此人的动作和位置，不描述长相。
6. 15 屏详情页的画面要有变化（特写、场景、平铺、手持、局部），但色调统一，像同一套设计。

输出纯 JSON，不加 markdown：
{
  "main_images": [
    {"role": "...", "headline": "...", "subline": "...", "badges": ["..."],
     "text_position": "top" 或 "bottom", "use_characters": [], "scene_prompt_en": "..."}
  ],
  "detail_sections": [
    {"role": "...", "title": "...", "body": "...", "bullets": ["..."],
     "use_characters": [], "scene_prompt_en": "..."}
  ]
}
"""


def generate_ecom_plan(
    product_name: str,
    usp: list[str],
    brand_color: str,
    platform: str = DEFAULT_ECOM_PLATFORM,
    characters: list[dict] | None = None,
    main_count: int = 5,
    main_ratio: str = "1:1",
    product_context: str = "",
) -> dict:
    """
    生成主图 + 15 屏详情页的图文策划。
    main_count: 主图张数（5 或 10）；main_ratio: 主图比例（"1:1" 或 "3:4"）
    characters: 可出镜的角色（需已有定妆照）
    """
    spec = ECOM_PLATFORMS.get(platform, ECOM_PLATFORMS[DEFAULT_ECOM_PLATFORM])
    main_count = int(main_count) if int(main_count) in MAIN_COUNTS else MAIN_COUNTS[0]
    main_ratio = main_ratio if main_ratio in MAIN_RATIOS else MAIN_RATIOS[0]
    roles = spec["main_roles"][:main_count]
    characters = characters or []
    main_roles = "\n".join(f"{i}. {r}" for i, r in enumerate(roles, 1))
    detail_roles = "\n".join(f"{i}. {r}" for i, r in enumerate(DETAIL_ROLES, 1))
    chars_text = "\n".join(
        f"- {c['id']} {c.get('name', '')}（{c.get('role', '')}）：{c.get('appearance_zh', '')}" for c in characters
    ) or "（无，不安排真人出镜，use_characters 一律填 []）"

    user = f"""
商品名称：{product_name}
核心卖点：
{chr(10).join(f'- {u}' for u in usp)}
品牌主色：{brand_color}
{product_context}
目标平台：{platform}
平台特点：{spec['guide']}

可出镜的模特（已有定妆照，与带货视频中是同一个人）：
{chars_text}

请输出 {len(roles)} 张主图（比例 {main_ratio}{'，方图' if main_ratio == '1:1' else '，竖图'}），按顺序对应：
{main_roles}

以及 {len(DETAIL_ROLES)} 屏详情页，按顺序对应：
{detail_roles}
"""
    raw = _chat_json(PLAN_SYSTEM, user)
    plan = _normalize_plan(raw, roles, main_ratio, {c["id"] for c in characters})
    plan["meta"] = {"platform": platform, "product_name": product_name, "brand_color": brand_color,
                    "main_count": main_count, "main_ratio": main_ratio,
                    "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    return plan


def _str_list(v, limit):
    if isinstance(v, str):
        v = re.split(r"[\n、,，]", v)
    return [str(x).strip() for x in (v or []) if str(x).strip()][:limit]


def _normalize_plan(raw: dict, roles: list[str], main_ratio: str, char_ids: set) -> dict:
    raw_mains = raw.get("main_images") if isinstance(raw.get("main_images"), list) else []
    raw_details = raw.get("detail_sections") if isinstance(raw.get("detail_sections"), list) else []

    def pick(lst, i):
        return lst[i - 1] if i <= len(lst) and isinstance(lst[i - 1], dict) else {}

    mains = []
    for i, role in enumerate(roles, 1):
        it = pick(raw_mains, i)
        white = "白底" in role
        mains.append({
            "id": f"M{i}", "seq": i, "ratio": main_ratio, "role": role,
            "headline": "" if white else str(it.get("headline", "")).strip(),
            "subline": "" if white else str(it.get("subline", "")).strip(),
            "badges": [] if white else _str_list(it.get("badges"), 3),
            "text_position": "bottom" if str(it.get("text_position")).lower() == "bottom" else "top",
            "use_characters": [] if white else [c for c in _str_list(it.get("use_characters"), 2) if c in char_ids],
            "scene_prompt_en": str(it.get("scene_prompt_en", "")).strip(),
            "white_bg": white, "base_path": "", "final_path": "",
        })
    details = []
    for i, role in enumerate(DETAIL_ROLES, 1):
        it = pick(raw_details, i)
        details.append({
            "id": f"D{i}", "seq": i, "ratio": DETAIL_RATIO, "role": role,
            "title": str(it.get("title", "")).strip(),
            "body": str(it.get("body", "")).strip(),
            "bullets": _str_list(it.get("bullets"), 4),
            "use_characters": [c for c in _str_list(it.get("use_characters"), 2) if c in char_ids],
            "scene_prompt_en": str(it.get("scene_prompt_en", "")).strip(),
            "base_path": "", "final_path": "",
        })
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
    if PRODUCT_DIR.exists():
        shutil.rmtree(PRODUCT_DIR)
    if PLAN_PATH.exists():
        PLAN_PATH.unlink()


def product_slot_path(slot: int) -> str | None:
    """第 slot 个产品图位（1 起）当前的文件路径，没有则 None"""
    if not PRODUCT_DIR.exists():
        return None
    hits = sorted(PRODUCT_DIR.glob(f"product_{slot}.*"))
    return str(hits[0]) if hits else None


def set_product_image(slot: int, path: str | None) -> str | None:
    """设置/删除第 slot 个产品图位：path 为 None 时删除。返回保存后的路径"""
    if not 1 <= slot <= MAX_PRODUCT_REFS:
        raise ValueError(f"产品图位只有 1-{MAX_PRODUCT_REFS}")
    PRODUCT_DIR.mkdir(parents=True, exist_ok=True)
    for old in PRODUCT_DIR.glob(f"product_{slot}.*"):
        old.unlink()
    if not path:
        return None
    dest = PRODUCT_DIR / f"product_{slot}{Path(path).suffix.lower() or '.png'}"
    shutil.copy(path, dest)
    return str(dest)


def get_product_images() -> list[str]:
    """按图位顺序返回已上传的产品图（图 1 作为主参考）"""
    return [p for p in (product_slot_path(i) for i in range(1, MAX_PRODUCT_REFS + 1)) if p]


# 每个图位的角度/内容说明（如"正面平视"），视频剧本据此给镜头选图
PRODUCT_LABELS_PATH = PRODUCT_DIR / "labels.json"


def get_product_labels() -> dict[int, str]:
    try:
        data = json.loads(PRODUCT_LABELS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {int(k): str(v) for k, v in data.items() if str(k).isdigit() and str(v).strip()}


def product_label(slot: int) -> str:
    return get_product_labels().get(slot, "")


def set_product_label(slot: int, text: str):
    labels = get_product_labels()
    text = (text or "").strip()
    if text:
        labels[slot] = text
    else:
        labels.pop(slot, None)
    PRODUCT_DIR.mkdir(parents=True, exist_ok=True)
    PRODUCT_LABELS_PATH.write_text(json.dumps({str(k): v for k, v in labels.items()}, ensure_ascii=False),
                                   encoding="utf-8")


def product_slots() -> list[tuple[int, str, str]]:
    """已上传的图位：[(图位号, 路径, 说明)]，按图位顺序"""
    labels = get_product_labels()
    return [(i, p, labels.get(i, "")) for i in range(1, MAX_PRODUCT_REFS + 1) if (p := product_slot_path(i))]


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


def needs_regen(item: dict, text_mode: str) -> bool:
    """底图是否需要重新生成：没有底图，或底图的写字方式与当前选择不同（无字的图不受影响）"""
    if not (item.get("base_path") and os.path.exists(item["base_path"])):
        return True
    return has_text(item) and item.get("base_mode", DEFAULT_TEXT_MODE) != text_mode


# ── 生成画面 ──────────────────────────────────────────────────────────────────
def _ai_text_block(item: dict, kind: str, brand_hex: str) -> list[str]:
    """AI 写字模式：把文案和版式要求写进提示词"""
    def q(s):
        return '"' + str(s).replace('"', "'") + '"'

    if kind == "main":
        parts = []
        if item.get("headline"):
            parts.append(f"a large bold headline {q(item['headline'])}")
        if item.get("subline"):
            parts.append(f"a smaller subheadline below it {q(item['subline'])}")
        if item.get("badges"):
            parts.append("a row of rounded pill-shaped tags filled with the brand color, reading "
                         + ", ".join(q(b) for b in item["badges"]))
        area = "top third" if item.get("text_position") != "bottom" else "bottom third"
        align = "centered"
    else:
        parts = []
        if item.get("title"):
            parts.append(f"a large bold title {q(item['title'])} with a short brand-color accent line under it")
        if item.get("body"):
            parts.append(f"a paragraph of body text {q(item['body'])}")
        if item.get("bullets"):
            parts.append("a bullet list: " + "; ".join(q(b) for b in item["bullets"]))
        area = "top 35%"
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
                      text_mode: str = DEFAULT_TEXT_MODE, brand_hex: str = "") -> str:
    ratio = item.get("ratio", "1:1")
    ai_text = _ai_text_block(item, kind, brand_hex or "#C8A882") if text_mode == "ai" else []
    lines = []
    if n_product == 1:
        lines.append("Reference image 1 is the real product.")
    else:
        lines.append(f"Reference images 1-{n_product} show the same real product from different angles.")
    lines.append(
        "Reproduce this product exactly as in the reference: same shape, proportions, packaging text, logo, "
        "colors, materials and label layout. Do not redesign, retype, translate or blur any text printed on it."
    )
    for k, c in enumerate(chars, n_product + 1):
        lines.append(f"Reference image {k} is a portrait of the model {c.get('name', c['id'])}; "
                     f"the person in the image must be this exact person with the same face, hairstyle and outfit.")

    frame = "Square 1:1" if ratio == "1:1" else "Vertical 3:4"
    if item.get("white_bg"):
        lines.append(f"{frame} image. Pure white background (#FFFFFF), product front view centered and filling "
                     f"about 80% of the frame width, soft natural contact shadow only, no props, no people.")
    else:
        lines.append(item.get("scene_prompt_en", ""))
        purpose = "the typeset text below" if ai_text else "a text overlay"
        if kind == "main":
            area = "top" if item.get("text_position") != "bottom" else "bottom"
            lines.append(f"{frame} e-commerce main image. Keep the {area} third of the frame as clean, uncluttered "
                         f"background for {purpose}; the product must not overlap that area.")
        else:
            lines.append("Vertical 3:4 e-commerce detail page section. Keep the top 35% of the frame as clean, "
                         f"softly lit, uncluttered background for {purpose}; the product sits in the lower part.")
    if ai_text:
        lines += ai_text
        lines.append("Apart from the text specified above and what is printed on the product, "
                     "add no other text, letters, numbers, watermarks or logos.")
    else:
        lines.append("Do not add any text, letters, numbers, watermarks or logos other than what is printed on the product.")
    lines.append("Photorealistic commercial product photography, sharp focus, high detail, accurate colors.")
    return "\n".join(l for l in lines if l)


def render_item(plan: dict, kind: str, item: dict, product_paths: list[str], portraits: dict[str, dict],
                text_mode: str = DEFAULT_TEXT_MODE) -> str:
    """
    生成底图并出成品。portraits: {角色id: 角色dict(含 image_path)}
    text_mode="overlay"：底图无字，再由程序叠字；"ai"：AI 直接把文案画进图里，成品只做裁切缩放
    """
    text_mode = text_mode if text_mode in TEXT_MODES else DEFAULT_TEXT_MODE
    chars = [portraits[c] for c in item.get("use_characters", []) if c in portraits]
    products = product_paths[:MAX_PRODUCT_REFS]
    brand_hex = "#%02X%02X%02X" % _parse_color(plan.get("meta", {}).get("brand_color"))
    prompt = build_edit_prompt(item, kind, len(products), chars, text_mode, brand_hex)
    base = edit_image(prompt, products + [c["image_path"] for c in chars],
                      BASE_DIR / f"{item['id']}_base.png", item.get("ratio", "1:1"))
    item["base_path"] = str(base)
    item["base_mode"] = text_mode
    return compose_item(plan, kind, item)


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


def compose_main(item: dict, brand, out: Path) -> Path:
    img = _load_base(item)
    W, H = img.size
    if not (item.get("headline") or item.get("subline") or item.get("badges")):
        return _save_final(img, out)

    top = item.get("text_position") != "bottom"
    margin, max_w = int(W * 0.07), W * 0.86
    f_head, f_sub, f_badge = _font(int(W * 0.085), True), _font(int(W * 0.045)), _font(int(W * 0.036), True)
    head = _wrap(item.get("headline", ""), f_head, max_w)[:2]
    sub = _wrap(item.get("subline", ""), f_sub, max_w)[:2]
    badges = item.get("badges", [])[:3]
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


def compose_detail(item: dict, brand, out: Path) -> Path:
    img = _load_base(item)
    W, H = img.size

    margin, max_w = int(W * 0.08), W * 0.84
    f_title, f_body, f_bullet = _font(int(W * 0.068), True), _font(int(W * 0.036)), _font(int(W * 0.036))
    title = _wrap(item.get("title", ""), f_title, max_w)[:2]
    body = _wrap(item.get("body", ""), f_body, max_w)[:4]
    bullets = [l for b in item.get("bullets", [])[:4] for l in _wrap("· " + b, f_bullet, max_w)[:2]]
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


def compose_item(plan: dict, kind: str, item: dict) -> str:
    """只重排文字（改字后用，不重新调用生图接口）"""
    if not (item.get("base_path") and os.path.exists(item["base_path"])):
        raise FileNotFoundError(f"{item['id']} 还没有底图，请先生成")
    brand = _parse_color(plan.get("meta", {}).get("brand_color"))
    out = FINAL_DIR / final_filename(kind, item)
    if item.get("base_mode") == "ai":
        # AI 已把文字画进图里，只裁切缩放到成品尺寸，不再叠字
        out = _save_final(_load_base(item), out)
    elif kind == "main":
        out = compose_main(item, brand, out)
    else:
        out = compose_detail(item, brand, out)
    item["final_path"] = str(out)
    return str(out)
