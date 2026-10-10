"""
层 1 · 策划层
使用 right.codes LLM API 将商品卖点 → 带货分镜 JSON
right.codes 兼容 OpenAI 协议，直接使用 openai SDK 即可

Base URL（来自平台令牌管理页）：https://www.right.codes
OpenAI SDK 需要加 /v1 后缀：  https://www.right.codes/v1
令牌分工：
  - Claude awsq key → 策划层 LLM 调用（RIGHT_CODES_LLM_KEY）
  - draw key        → 视觉资产层生图调用（RIGHT_CODES_DRAW_KEY）
"""

import json
import os
import random
import re
import time
import requests
from pathlib import Path
from openai import OpenAI

# 加载 pipeline/.env
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

# ── right.codes 接入配置 ─────────────────────────────────────────────────────
# 在 .env 文件中设置（参见 pipeline/.env.example）：
RIGHT_CODES_BASE_URL = "https://www.rightapi.ai/v1"
RIGHT_CODES_LLM_KEY = os.environ.get("RIGHT_CODES_LLM_KEY", "")

# 策划层：使用 Claude awsq 高质量令牌对应的模型
# 请确保此处的模型名称在 right.codes 是真实存在的，例如 claude-3-5-sonnet-20240620 或 gpt-4o
PLANNING_MODEL = os.environ.get("PLANNING_MODEL", "claude-opus-5-5")

# LLM 单次请求的读取超时（秒）和重试次数。长输出（完整剧本 / 25 张图文策划）常超过 2 分钟
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "300"))
LLM_RETRIES = 3


# ── 剧本模板：定义镜头数量与每个镜头的叙事节拍 ────────────────────────────────
# beats: (节拍名, A-Roll/B-Roll, 这一镜要完成的叙事任务)
# 节拍名会作为 emotion_beat 写入分镜，层 2 按它匹配生图风格（EMOTION_BEAT_STYLE）
STORY_TEMPLATES: dict[str, dict] = {
    "经典反转 · 4镜": {
        "desc": "先用人物冲突抓住注意力，产品作为解决冲突的关键道具出场，结尾反转。适合有明确痛点的产品。",
        "beats": [
            ("开场钩子", "A-Roll", "前 2 秒抛出悬念或冲突，人物处于尴尬/焦虑/意外的处境，不出现产品"),
            ("使用场景", "A-Roll", "冲突升级，把痛点放大到观众感同身受，仍不出现产品"),
            ("卖点展示", "B-Roll", "产品登场特写，用画面直观呈现核心卖点（质地、细节、包装）"),
            ("行动召唤", "A-Roll", "反转：人物因产品摆脱困境，情绪释放，口播自然带出购买理由"),
        ],
    },
    "痛点-解决 · 5镜": {
        "desc": "PAS 结构：痛点 → 加剧 → 方案 → 证据 → 召唤。卖点多、需要讲清功效时使用。",
        "beats": [
            ("开场钩子", "A-Roll", "直击一个具体痛点场景，引发共鸣"),
            ("使用场景", "A-Roll", "展示用错方法/不用产品的后果，加剧焦虑"),
            ("卖点展示", "B-Roll", "产品登场，特写呈现最核心的一个卖点"),
            ("信任背书", "B-Roll", "用细节、成分、质地或对比画面证明效果"),
            ("行动召唤", "A-Roll", "人物使用后的满意状态，口播给出明确购买指令"),
        ],
    },
    "种草分享 · 5镜": {
        "desc": "第一人称真实体验口吻，像朋友推荐。适合小红书、视频号等重信任感的平台。",
        "beats": [
            ("开场钩子", "A-Roll", "博主口吻的生活化开场，说出\"最近发现一个好东西\"类的引子"),
            ("卖点展示", "B-Roll", "开箱/手持产品，展示包装与外观"),
            ("使用场景", "A-Roll", "日常场景中自然地使用产品"),
            ("信任背书", "B-Roll", "质地、细节特写，配合真实感受描述"),
            ("行动召唤", "A-Roll", "总结推荐理由，自然地引导收藏或下单"),
        ],
    },
}
DEFAULT_TEMPLATE = "经典反转 · 4镜"

# ── 目标平台：决定画幅（影响层 2 生图尺寸）和剧本节奏/风格 ──────────────────────
PLATFORMS: dict[str, dict] = {
    "抖音/快手（竖版 9:16）": {
        "aspect_ratio": "9:16", "image_size": "1024x1792",
        "guide": "竖屏全屏观看，前 2 秒必须有强钩子；节奏快、镜头短；口播口语化、有网感；主体居中，上下留出字幕区。",
    },
    "视频号（竖版 9:16）": {
        "aspect_ratio": "9:16", "image_size": "1024x1792",
        "guide": "用户偏熟龄、重信任；节奏可稍缓，突出真实感、生活化和口碑；避免过度夸张的表演。",
    },
    "淘宝/京东 主图视频（方形 1:1）": {
        "aspect_ratio": "1:1", "image_size": "1024x1024",
        "guide": "用户已有购买意向，重点讲清卖点和细节；产品占画面主体，剧情可弱化，B-Roll 比重提高。",
    },
    "淘宝/京东 详情页（横版 16:9）": {
        "aspect_ratio": "16:9", "image_size": "1792x1024",
        "guide": "横屏构图，信息完整；适合展示使用场景全景和多卖点并列，口播偏讲解型。",
    },
}
DEFAULT_PLATFORM = "抖音/快手（竖版 9:16）"

# 一个剧本最多向用户索要的真实产品图数量
MAX_PRODUCT_REFS = 3
DEFAULT_REF_DESC = "产品主图：包装正面平视，背景干净，品牌名与包装文字清晰可辨"


# ── 系统提示词 ────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """\
你是一位顶级 4A 广告公司的创意总监和短剧编剧，精通“软广剧情植入”与 MiniMax H3 视频生成规范。根据商品信息，输出一个结构化的带货分镜 JSON。

【创作原则】
1. 绝对不要写直白的电视购物/硬广（严禁开局直接介绍产品功能参数）。
2. 遵循“麦格芬法则”：让产品成为推动剧情、解决人际冲突、或是安抚人物情绪的关键道具。
3. 结构必须严格遵循用户给定的【剧情叙事节拍】模板。
   - A-Roll（人戏）：高悬念/共鸣，以人物为主；开场/冲突镜头不出现产品，结尾反转/召唤镜头可以让人物手持或使用产品；
   - B-Roll（物戏）：产品高光特写，必须是刚体静止、电影级扫光、微距运镜（防变形核心）。
4. 人物与场景一致性：先在 script 中定好角色和场景设定，所有镜头只引用这些设定；
   同一角色全片服装、发型、妆容不变（除非剧情需要，并在 full_text 中写明）。

script 字段说明（完整剧本：给人阅读，也是保证镜头间一致性的设定集）：
- title: 片名
- logline: 一句话剧情梗概
- characters: 角色设定列表，每项 { "id": "C1", "name": 角色名, "role": 剧中身份,
    "appearance_zh": 中文外貌（年龄、发型、妆容、服装及颜色、配饰、体态）,
    "appearance_en": 与 appearance_zh 同内容的英文，务必具体（gender, age, ethnicity, face shape, hairstyle, makeup, outfit with colors, accessories），
                     会被用来生成角色定妆照，并原样复制进每个镜头的视频提示词，不要写情绪和动作 }
  有角色出场的镜头，系统会自动改为 Ref2VA 并绑定该角色的定妆照，你只需正确填写 characters 字段。
- scenes: 场景设定列表，每项 { "id": "S1", "name": 场景名,
    "description_en": 英文场景描述（地点、时间、光线、主色调、关键陈设），会被原样复用 }
- full_text: 中文完整剧本，按镜头顺序书写，包含场景、人物动作、表情和台词，像真正的短剧剧本

每个镜头字段说明：
- id: 镜头编号
- duration_sec: 时长（5.0–6.0 秒，H3 模型要求 ≥ 5.0）
- mode: 只能取 "T2VA" 或 "Ref2VA"。没有产品入画的镜头用 "T2VA"；凡是产品入画（包括 B-Roll 特写、人物手持/使用产品）必须用 "Ref2VA" 并填写 ref_needed（锁定真实产品特征，保证刚体无变形）；不要使用 I2VA / R2V
- voiceover: 中文台词或画外音（每镜不超过 18 字，口语、短句；字数多会被截断）
- visual_intent: 视觉构图描述（中文，给生图 AI 和人类导演看的方向）
- camera: 英文运镜指令（使用 H3 规范词：Arc Shot / Push In / Pull Out / Zoom In / Pan / Tilt / Static Shot 等，加 amplitude + speed）
- emotion_beat: 叙事节拍名，必须与用户给定节拍表中的名称完全一致
- image_gen_prompt: 英文生图提示词（给 FLUX / gpt-image 用。B-Roll 必须有极致的产品细节、光线、质感描写。A-Roll 写场景和人物状态）
- ref_needed: Ref2VA 镜头填所用真实产品图的 id（必须是 product_refs 中的 id，如 "Ref1"）；T2VA 镜头填 null。
- characters: 本镜头出场角色 id 列表（如 ["C1"]），无人物填 []
- scene: 本镜头所在场景 id（如 "S1"）
- image_path: 留空字符串（由层 2 回填）
image_gen_prompt 中涉及角色或场景时，直接嵌入对应的 appearance_en / description_en 原文。

product_refs 字段说明（本剧本需要用户提供的真实产品图清单）：
- id: "Ref1"、"Ref2" ... 从 1 开始连续编号
- desc: 中文拍摄要求，写清拍什么、什么角度、什么背景，让用户照着拍或挑图。
  例如 "瓶身正面平视，白底，品牌名和文字清晰"、"滴管挤出精华的质地特写，微距"

输出格式：纯 JSON，不加 markdown 代码块，不加多余注释。

JSON 结构：
{
  "product": { "name": "...", "usp": [...], "brand_color": "..." },
  "script": { "title": "...", "logline": "...", "characters": [ {...} ], "scenes": [ {...} ], "full_text": "..." },
  "product_refs": [ { "id": "Ref1", "desc": "..." } ],
  "shots": [ {...}, {...}, {...}, {...} ]
}
"""


def generate_storyboard(
    product_name: str,
    usp: list[str],
    brand_color: str = "#000000",
    story_template: str = DEFAULT_TEMPLATE,
    target_platform: str = DEFAULT_PLATFORM,
    agent_skill_content: str = "",
    max_refs: int = MAX_PRODUCT_REFS,
    product_context: str = "",
) -> dict:
    """
    调用 right.codes LLM，生成带货分镜 JSON。

    Args:
        product_name: 商品名称
        usp: 核心卖点列表
        brand_color: 品牌主色
        story_template: STORY_TEMPLATES 的键；不在表中时按自由文本节拍描述使用
        target_platform: PLATFORMS 的键；决定画幅与剧本节奏
        agent_skill_content: 选择的编剧技能（SKILL.md 内容）
        max_refs: 最多向用户索要几张真实产品图
        product_context: 产品分析（痛点/人群/场景/背书等，见 product_brief.brief_context），可为空

    Returns:
        分镜 JSON dict，含 meta / product_refs / shots
    """
    # 剧本模板 → 明确的镜头数与逐镜节拍
    tpl = STORY_TEMPLATES.get(story_template)
    if tpl:
        beats_text = "\n".join(
            f"{i}. 【{name}】({roll}) {task}" for i, (name, roll, task) in enumerate(tpl["beats"], 1)
        )
        structure = (
            f"剧本模板：{story_template}（{tpl['desc']}）\n"
            f"请严格输出 {len(tpl['beats'])} 个镜头，按顺序一一对应以下节拍，emotion_beat 填节拍名：\n{beats_text}"
        )
    else:
        structure = f"剧情叙事节拍：{story_template}"

    # 目标平台 → 画幅 + 平台风格
    plat = PLATFORMS.get(target_platform, PLATFORMS[DEFAULT_PLATFORM])
    platform_text = (
        f"目标平台：{target_platform}\n"
        f"画幅：{plat['aspect_ratio']}（所有构图描述和 image_gen_prompt 都按此画幅设计）\n"
        f"平台要求：{plat['guide']}"
    )

    # 产品图由剧本决定：LLM 列出需要哪些图；之后由 AI 以用户上传的实拍图为参考生成（或用户手动上传）
    ref_info = f"""
【真实产品图需求】
系统会按你的描述，以用户上传的产品实拍图为参考生成这些产品图（外观与实物一致），用于锁定产品外观。请：
1. 在 product_refs 中列出本剧本需要的产品图，最多 {max_refs} 张，能复用同一张就复用；
2. 每张图的 desc 写清拍摄要求（内容、角度、背景），让用户能照着拍或从现有图里挑；
3. 凡是真实产品入画的镜头，mode 用 "Ref2VA"，ref_needed 填对应的 id；
4. 这些镜头的 image_gen_prompt 中要指引锁定参考图特征（例如：matching the reference product exactly, the packaging text, colors, and textures strictly adhere to the reference image）。
"""

    skill_instruction = f"""
【注入好莱坞编剧大师级技能】
请仔细阅读并内化以下编剧流派或技能的技法，并将其灵魂完美融入到本次剧本创作中：
---
{agent_skill_content}
---
""" if agent_skill_content else ""

    user_prompt = f"""
商品名称：{product_name}
核心卖点：{chr(10).join(f'- {u}' for u in usp)}
品牌主色：{brand_color}
{product_context}
{platform_text}

{structure}
{ref_info}
{skill_instruction}
我的商品是一款【{product_name}】，请严格按照上面的剧本模板节拍，帮我策划剧情短剧广告脚本。
明确指出每个镜头的角色、剧情、场景，并且 B-Roll 怎么切入产品特写。
注意：无需产品入画的镜头标记为 "T2VA" 并在 ref_needed 填 null。凡是产品入画的镜头（B-Roll 特写，或人物手持/使用产品的 A-Roll），请明确标记为 "Ref2VA" 并指定 ref_needed（例如 "Ref1"）！千万不要用 I2VA，因为 I2VA 无法锁定真实产品特征！
"""

    headers = {
        "Authorization": f"Bearer {RIGHT_CODES_LLM_KEY}",
        "Content-Type": "application/json",
        "Connection": "close"
    }
    
    payload = {
        "model": PLANNING_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 8192,  # 现在包含完整剧本 + 设定集，4096 容易被截断导致 JSON 解析失败
        "response_format": {"type": "json_object"}
    }
    
    # 完整剧本 + 设定集输出很长，大模型常要 2 分钟以上；超时 / 断网 / 5xx 重试，共 3 次
    resp = None
    for attempt in range(LLM_RETRIES):
        # 临时移除 NO_PROXY 以避免 requests 忽略系统代理
        old_no_proxy = os.environ.pop("NO_PROXY", None)
        old_no_proxy_lower = os.environ.pop("no_proxy", None)
        try:
            resp = requests.post(
                f"{RIGHT_CODES_BASE_URL.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
                timeout=LLM_TIMEOUT,
            )
            if resp.status_code < 500 or attempt == LLM_RETRIES - 1:
                break
            print(f"[层1] [{attempt + 1}/{LLM_RETRIES}] 接口返回 {resp.status_code}，5 秒后重试...")
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt == LLM_RETRIES - 1:
                raise Exception(f"LLM 请求 {LLM_RETRIES} 次都失败（{type(e).__name__}，"
                                f"单次超时 {LLM_TIMEOUT:.0f} 秒）。可在 .env 设 LLM_TIMEOUT 调大，"
                                "或稍后重试") from e
            print(f"[层1] [{attempt + 1}/{LLM_RETRIES}] {type(e).__name__}，5 秒后重试...")
        finally:
            if old_no_proxy is not None:
                os.environ["NO_PROXY"] = old_no_proxy
            if old_no_proxy_lower is not None:
                os.environ["no_proxy"] = old_no_proxy_lower
        time.sleep(5)

    if resp.status_code != 200:
        raise Exception(f"API请求失败 ({resp.status_code}): {resp.text}")
        
    choice = resp.json()["choices"][0]
    raw = _strip_code_fence(choice["message"]["content"] or "")
    try:
        storyboard = json.loads(raw)
    except json.JSONDecodeError as e:
        hint = "（输出被 max_tokens 截断，可减少镜头数后重试）" if choice.get("finish_reason") == "length" else ""
        raise Exception(f"LLM 返回的分镜不是合法 JSON{hint}：{e}") from e

    shots = [s for s in storyboard.get("shots", []) if isinstance(s, dict)]
    storyboard["shots"] = shots
    for i, shot in enumerate(shots, 1):
        # 镜头 id 统一按顺序重排为 1..N：下游文件名 shot_XX 用 int(id)，LLM 给 "S1"/"1A" 会串片
        shot["id"] = i
        # 校验：duration_sec 必须 ≥ 5.0（H3 实测要求 ≥ 120 帧 @24fps）；LLM 可能返回字符串
        try:
            duration = float(shot.get("duration_sec", 0))
        except (TypeError, ValueError):
            duration = 0.0
        # 按口播字数兜底时长（约 4 字/秒 + 起止留白），避免台词被截断；H3 上限约 14 秒
        speech_sec = len(re.sub(r"\s", "", str(shot.get("voiceover") or ""))) / 4 + 0.8
        shot["duration_sec"] = round(min(max(duration, 5.0, speech_sec), 14.0), 1)
        # 统一 mode 写法，下游各层按规范值判断
        shot["mode"] = normalize_mode(shot.get("mode"))
        # 确保 image_path 字段存在
        shot.setdefault("image_path", "")

    storyboard["script"] = _normalize_script(storyboard)
    # 先按 LLM 原始 mode 整理产品图，再把有角色出场的镜头升级为 Ref2VA（顺序不能反，
    # 否则人物镜头会被误判为需要产品图）
    storyboard["product_refs"] = _normalize_product_refs(storyboard)
    _apply_character_refs(storyboard)
    storyboard["meta"] = {
        "story_template": story_template,
        "platform": target_platform,
        "aspect_ratio": plat["aspect_ratio"],
        "image_size": plat["image_size"],
        # 全片共用同一个随机种子，进一步统一画风
        "seed": random.randint(0, 2**31 - 1),
    }
    return storyboard


def _strip_code_fence(text: str) -> str:
    """去掉 LLM 偶尔包在外面的 ```json ... ``` """
    text = text.strip()
    m = re.match(r"^```[a-zA-Z]*\s*\n?(.*?)\n?```$", text, re.S)
    return m.group(1).strip() if m else text


def _apply_character_refs(storyboard: dict):
    """有角色出场的镜头一律走 Ref2VA，用角色定妆照锁定长相；定妆照由层 2 生成或用户上传"""
    for c in storyboard.get("script", {}).get("characters", []):
        c.setdefault("image_path", "")
        c.setdefault("image_source", "")  # "generated" / "upload"
    for shot in storyboard.get("shots", []):
        if shot.get("characters"):
            shot["mode"] = "Ref2VA"


def shot_references(storyboard: dict, shot: dict) -> list[dict]:
    """
    返回某镜头需要的全部参考图，顺序即 H3 中的 <Picture N> 编号：先角色定妆照，后产品图。
    每项：{"label": "Picture 1", "kind": "character"|"product", "id": "C1"|"Ref1", "name": ..., "path": ...}
    path 为空表示该参考图尚未就绪。
    """
    script = storyboard.get("script") or {}
    chars = {c["id"]: c for c in script.get("characters", []) if isinstance(c, dict) and c.get("id")}
    prods = {r["id"]: r for r in storyboard.get("product_refs", []) if isinstance(r, dict) and r.get("id")}

    refs = []
    for cid in shot.get("characters", []):
        c = chars.get(cid)
        if c:
            refs.append({"kind": "character", "id": cid, "name": c.get("name", cid), "path": c.get("image_path", "")})
    rid = shot.get("ref_needed")
    if rid and rid in prods:
        refs.append({"kind": "product", "id": rid, "name": "产品", "path": prods[rid].get("image_path", "")})
    for i, r in enumerate(refs, 1):
        r["label"] = f"Picture {i}"
    return refs


def normalize_ref_id(value):
    """'Ref1' / 'ref 1' / 'R1' / 1 → 'Ref1'；无法解析返回 None"""
    if value is None:
        return None
    m = re.search(r"\d+", str(value))
    return f"Ref{int(m.group())}" if m else None


def _normalize_script(storyboard: dict) -> dict:
    """补齐剧本结构，并让镜头只引用已定义的角色/场景 id"""
    script = storyboard.get("script")
    if not isinstance(script, dict):
        script = {}

    def clean(items, prefix):
        out, seen = [], set()
        for i, item in enumerate(items if isinstance(items, list) else [], 1):
            if not isinstance(item, dict):
                continue
            m = re.search(r"\d+", str(item.get("id", "")))
            item["id"] = f"{prefix}{int(m.group()) if m else i}"
            if item["id"] not in seen:
                seen.add(item["id"])
                out.append(item)
        return out

    script["characters"] = clean(script.get("characters"), "C")
    script["scenes"] = clean(script.get("scenes"), "S")
    for key in ("title", "logline", "full_text"):
        script[key] = str(script.get(key) or "")

    char_ids = {c["id"] for c in script["characters"]}
    scene_ids = {s["id"] for s in script["scenes"]}
    for shot in storyboard.get("shots", []):
        chars = shot.get("characters")
        chars = chars if isinstance(chars, list) else []
        shot["characters"] = [f"C{int(m.group())}" for c in chars if (m := re.search(r"\d+", str(c)))
                              and f"C{int(m.group())}" in char_ids]
        m = re.search(r"\d+", str(shot.get("scene") or ""))
        sid = f"S{int(m.group())}" if m else None
        shot["scene"] = sid if sid in scene_ids else None
    return script


def _normalize_product_refs(storyboard: dict) -> list[dict]:
    """
    整理产品图清单，保证与镜头引用一致：
    - Ref2VA 镜头缺 ref_needed 时默认绑定 Ref1；
    - 镜头引用了但清单里没有的 id，补一条默认描述；
    - 清单里没有任何镜头使用的图，删掉，不让用户白拍。
    """
    descs = {}
    for r in storyboard.get("product_refs") or []:
        rid = normalize_ref_id(r.get("id")) if isinstance(r, dict) else None
        if rid and rid not in descs:
            descs[rid] = str(r.get("desc") or "").strip()

    used_in: dict[str, list] = {}
    for shot in storyboard.get("shots", []):
        # LLM 填了 ref_needed 说明产品入画（如人物手持产品），即使 mode 写成 T2VA 也要绑定产品图
        if shot.get("mode") == "Ref2VA" or normalize_ref_id(shot.get("ref_needed")):
            shot["mode"] = "Ref2VA"
            rid = normalize_ref_id(shot.get("ref_needed")) or "Ref1"
            shot["ref_needed"] = rid
            used_in.setdefault(rid, []).append(shot.get("id"))
        else:
            shot["ref_needed"] = None

    return [
        {"id": rid, "desc": descs.get(rid) or DEFAULT_REF_DESC, "used_in": used_in[rid], "image_path": ""}
        for rid in sorted(used_in, key=lambda x: int(x[3:]))
    ]


# LLM 输出的 mode 写法不统一（REF2VA / R2V / t2v ...），归一化为规范值
_MODE_ALIASES = {
    "T2V": "T2VA", "T2VA": "T2VA",
    "I2V": "I2VA", "I2VA": "I2VA",
    "FL2V": "FL2VA", "FL2VA": "FL2VA",
    "L2V": "L2VA", "L2VA": "L2VA",
    "R2V": "Ref2VA", "R2VA": "Ref2VA", "REF2V": "Ref2VA", "REF2VA": "Ref2VA",
}


def normalize_mode(mode) -> str:
    key = str(mode or "T2VA").strip().upper().replace("-", "").replace("_", "")
    return _MODE_ALIASES.get(key, str(mode).strip())


RENDER_FPS = 24               # 与 h3_diffusers_service.FPS 保持一致
# H3 服务端实测（2026-10-10）：帧数按 17n+5 向上取整后须在 120–360 之间，即 124–345 帧（约 5.2–14.4 秒）
MIN_SEC, MAX_SEC = 5.0, 14.0


def duration_to_frames(duration_sec) -> int:
    """
    按分镜时长换算渲染帧数。H3 的视频 VAE 要求帧数为 17n+5（107, 124, 141...），
    这里取最接近的合法值，且不短于 MIN_SEC。
    """
    try:
        sec = float(duration_sec)
    except (TypeError, ValueError):
        sec = 5.0
    sec = min(max(sec, MIN_SEC), MAX_SEC)
    target = sec * RENDER_FPS
    n = max(round((target - 5) / 17), 1)
    # 向下取整最多允许短 6 帧（0.25s），再短台词可能被截断，改为向上取整
    if 17 * n + 5 < target - 6:
        n += 1
    while (17 * n + 5) < MIN_SEC * RENDER_FPS:
        n += 1
    return 17 * n + 5


def save_storyboard(storyboard: dict, output_path: str = "./pipeline/output/storyboard.json") -> str:
    """保存分镜 JSON 到文件"""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(storyboard, f, ensure_ascii=False, indent=2)
    print(f"[层1] 分镜 JSON 已保存：{output_path}")
    return output_path


# ── 示例入口 ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    storyboard = generate_storyboard(
        product_name="玻尿酸原液精华",
        usp=[
            "1000万分子玻尿酸，三重补水",
            "3秒渗透，不油腻不黏腻",
            "皮肤科医生认证，敏感肌可用",
            "买一赠一，限时特惠",
        ],
        brand_color="#C8A882",
    )

    save_storyboard(storyboard)

    print("\n[层1] 分镜预览：")
    for shot in storyboard["shots"]:
        print(f"  镜头{shot['id']} | {shot['duration_sec']}s | {shot['mode']} | {shot['emotion_beat']}")
        print(f"         口播：{shot['voiceover']}")
