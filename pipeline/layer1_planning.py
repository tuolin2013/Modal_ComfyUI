"""
层 1 · 策划层
使用 right.codes LLM API 将商品卖点 → 带货分镜 JSON
right.codes 兼容 OpenAI 协议，这里直接用 requests 调用 /chat/completions

Base URL：https://www.rightapi.ai/v1（OpenAI 兼容的 /chat/completions）
令牌分工：
  - LLM key  → 策划层 LLM 调用（RIGHT_CODES_LLM_KEY）
  - draw key → 视觉资产层生图调用（RIGHT_CODES_DRAW_KEY）
"""

import json
import os
import random
import re
import time
import requests
from pathlib import Path

import category_playbook as cp
import quality_check as qc

# 加载 pipeline/.env
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

# ── right.codes 接入配置 ─────────────────────────────────────────────────────
# 在 .env 文件中设置（参见 pipeline/.env.example）：
RIGHT_CODES_BASE_URL = "https://www.rightapi.ai/v1"
RIGHT_CODES_LLM_KEY = os.environ.get("RIGHT_CODES_LLM_KEY", "")

# 策划层模型：必须是平台上可用的模型名，可在 .env 的 PLANNING_MODEL 中修改
PLANNING_MODEL = os.environ.get("PLANNING_MODEL", "claude-opus-5-5")

# LLM 单次请求的读取超时（秒）和重试次数。长输出（完整剧本 / 25 张图文策划）常超过 2 分钟
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "300"))
LLM_RETRIES = 3


# ── 视频类型 / 剧本模板：定义镜头数量与每个镜头的叙事节拍 ─────────────────────────
# type：视频类型（剧情带货、口播种草……），同一类型可以有多个模板
# role：这类视频的编导角色，写进提示词，替代原来写死的「短剧编剧」
# rules：这类视频特有的创作规则
# beats: (节拍名, A-Roll/B-Roll, 这一镜要完成的叙事任务)
# 节拍名会作为 emotion_beat 写入分镜，层 2 按它匹配生图风格（EMOTION_BEAT_STYLE）
STORY_TEMPLATES: dict[str, dict] = {
    "经典反转 · 4镜": {
        "type": "剧情带货",
        "role": "短剧编剧：用一个有冲突的小故事带出产品，产品是解决冲突的关键道具",
        "rules": ["遵循“麦格芬法则”：产品推动剧情、解决人际冲突或安抚人物情绪，而不是被介绍",
                  "开场和冲突镜头不出现产品，反转镜头才让人物手持或使用产品"],
        "desc": "先用人物冲突抓住注意力，产品作为解决冲突的关键道具出场，结尾反转。适合有明确痛点的产品。",
        "beats": [
            ("开场钩子", "A-Roll", "前 2 秒抛出悬念或冲突，人物处于尴尬/焦虑/意外的处境，不出现产品"),
            ("使用场景", "A-Roll", "冲突升级，把痛点放大到观众感同身受，仍不出现产品"),
            ("卖点展示", "B-Roll", "产品登场特写，用画面直观呈现核心卖点（质地、细节、包装）"),
            ("行动召唤", "A-Roll", "反转：人物因产品摆脱困境，情绪释放，口播自然带出购买理由"),
        ],
    },
    "痛点-解决 · 5镜": {
        "type": "剧情带货",
        "role": "带货编导：按 PAS（痛点-加剧-方案）结构讲清产品为什么值得买",
        "rules": ["痛点要具体到一个画面，不说「很多人都有这个烦恼」这类空话",
                  "证据镜头用看得见的细节证明卖点，不靠口播宣称"],
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
        "type": "口播种草",
        "role": "生活方式博主：第一人称分享真实体验，像朋友推荐",
        "rules": ["口吻是分享不是推销，用「我」的感受说话",
                  "体验要具体（什么时候用、用完什么感觉），不堆形容词"],
        "desc": "第一人称真实体验口吻，像朋友推荐。适合小红书、视频号等重信任感的平台。",
        "beats": [
            ("开场钩子", "A-Roll", "博主口吻的生活化开场，说出\"最近发现一个好东西\"类的引子"),
            ("卖点展示", "B-Roll", "开箱/手持产品，展示包装与外观"),
            ("使用场景", "A-Roll", "日常场景中自然地使用产品"),
            ("信任背书", "B-Roll", "质地、细节特写，配合真实感受描述"),
            ("行动召唤", "A-Roll", "总结推荐理由，自然地引导收藏或下单"),
        ],
    },
    "商品展示 · 4镜": {
        "type": "产品展示",
        "role": "电商视觉导演：产品是主角，用光影和细节呈现卖点，剧情弱化",
        "rules": ["产品第 1 秒清晰入画，不铺垫剧情", "每个 B-Roll 镜头只呈现一个卖点"],
        "desc": "产品开场、卖点直给、弱剧情。适合淘宝/京东主图视频等用户已有购买意向的搜索场景。",
        "beats": [
            ("卖点展示", "B-Roll", "第 1 秒产品主体清晰入画，配最核心的一个利益点，不铺垫剧情"),
            ("使用场景", "A-Roll", "真人或生活场景中使用产品，直接呈现使用后的结果"),
            ("信任背书", "B-Roll", "细节、材质、成分或工艺特写，用画面证明上面的卖点"),
            ("行动召唤", "B-Roll", "产品全貌 + 规格/包装内含，口播给出购买理由"),
        ],
    },
    "口播种草 · 4镜": {
        "type": "口播种草",
        "role": "带货主播：真人正对镜头，一句话一个信息点，节奏快、结论先行",
        "rules": ["第一句就给结论或抛问题，不寒暄、不自我介绍", "主播镜头里人物正对镜头说话；每镜只讲一个点"],
        "desc": "真人对镜头推荐，结论先行。适合抖音/快手的达人口播和直播切片风格。",
        "beats": [
            ("开场钩子", "A-Roll", "主播正对镜头，第一句直接说出结论或一个扎心的问题"),
            ("卖点展示", "B-Roll", "手持产品特写，展示最核心的卖点"),
            ("使用场景", "A-Roll", "主播边用边说真实感受"),
            ("行动召唤", "A-Roll", "主播正对镜头给出推荐理由和适合谁买"),
        ],
    },
    "开箱测评 · 5镜": {
        "type": "开箱测评",
        "role": "测评博主：带着一个要验证的问题开箱、实测，给出有条件的结论",
        "rules": ["先提出要验证的问题，结尾回答它", "结论要有条件（适合谁、不适合谁），不做绝对化评价",
                  "不点名、不贬低其它品牌"],
        "desc": "提出问题 → 开箱 → 细节 → 实测 → 结论。适合 3C、家居、新品等需要建立信任的品类。",
        "beats": [
            ("开场钩子", "A-Roll", "博主拿着快递，说出这次要验证的一个具体问题"),
            ("开箱", "B-Roll", "拆开包装，展示包装内含和第一眼质感"),
            ("卖点展示", "B-Roll", "关键细节特写（做工、材质、接口、质地）"),
            ("使用场景", "A-Roll", "真实场景中实测，呈现可见的结果"),
            ("行动召唤", "A-Roll", "回答开头的问题，给出适合谁的结论"),
        ],
    },
    "使用教程 · 4镜": {
        "type": "使用教程",
        "role": "产品讲师：用分步演示讲清怎么用、用对了是什么效果",
        "rules": ["每个步骤镜头只演示一到两个动作，动作清楚可见", "开场指出一个常见的错误用法，制造看下去的理由"],
        "desc": "错误用法 → 步骤 → 步骤 → 效果。适合需要教育用户的产品，也适合详情页视频。",
        "beats": [
            ("开场钩子", "A-Roll", "人物演示一个常见的错误用法，口播点出问题"),
            ("使用步骤", "B-Roll", "正确用法的前一半步骤，手部动作清晰"),
            ("使用步骤", "B-Roll", "正确用法的后一半步骤，呈现产品状态的变化"),
            ("行动召唤", "A-Roll", "用对之后的效果，口播总结要点"),
        ],
    },
    "产地故事 · 5镜": {
        "type": "产地 / 品牌故事",
        "role": "纪录片导演：用产地、原料和工艺的真实画面建立信任，节奏舒缓",
        "rules": ["只写用户提供的产地、原料、工艺信息，不编造历史、荣誉和年份",
                  "产地空镜不出现人物和产品，可以只有环境声、没有口播"],
        "desc": "产地 → 原料 → 工艺 → 成品 → 饮用/使用。适合农产品、茶饮、手工制品和地方特产。",
        "beats": [
            ("开场钩子", "A-Roll", "产地环境空镜，建立地域感和季节感，不出现产品"),
            ("产地溯源", "A-Roll", "原料采摘或收获的真实动作"),
            ("工艺细节", "B-Roll", "加工、晾晒或制作工序的细节"),
            ("卖点展示", "B-Roll", "成品包装与质地特写"),
            ("行动召唤", "A-Roll", "人物在生活场景中饮用或使用，口播带出购买理由"),
        ],
    },
    "对比实验 · 4镜": {
        "type": "对比实验",
        "role": "实验型博主：用同一个可见指标做前后或同框对比，让结果自己说话",
        "rules": ["对比对象写「普通做法」或「使用前」，不点名、不贬低竞品",
                  "对比的指标必须在画面里看得见（颜色、形态、残留、时间）", "不夸大结果，不写医疗或功效类结论"],
        "desc": "抛出问题 → 同框对比 → 结果特写 → 结论。适合清洁、收纳、防护类等效果可视化的产品。",
        "beats": [
            ("开场钩子", "A-Roll", "人物抛出一个可以用实验回答的问题"),
            ("对比实验", "B-Roll", "普通做法与本品同框或前后对比，过程清晰可见"),
            ("卖点展示", "B-Roll", "对比结果特写，突出一个可见指标"),
            ("行动召唤", "A-Roll", "人物说出结论和适用场景"),
        ],
    },
    "节日营销 · 4镜": {
        "type": "节日营销",
        "role": "品牌营销导演：把产品放进节日场景，用情感和仪式感带出购买理由",
        "rules": ["节日氛围靠场景和道具，不在画面里写字", "不编造优惠和库存；优惠信息后期用字幕加",
                  "情感要落到具体的人和动作（送给谁、怎么送）"],
        "desc": "节日场景 → 送礼/团聚 → 礼盒特写 → 祝福 + 购买理由。适合礼盒、节日限定和送礼场景。",
        "beats": [
            ("开场钩子", "A-Roll", "节日场景里人物的一个小困扰（不知道送什么、来不及准备）"),
            ("使用场景", "A-Roll", "送出或一起享用产品的温暖瞬间"),
            ("卖点展示", "B-Roll", "礼盒或产品特写，呈现质感和仪式感"),
            ("行动召唤", "A-Roll", "人物送上祝福，口播带出购买理由"),
        ],
    },
}
DEFAULT_TEMPLATE = "经典反转 · 4镜"
# 自定义节拍：覆盖不到的场景由用户逐行填写节拍
CUSTOM_TEMPLATE = "custom"
MIN_BEATS, MAX_BEATS = 2, 6
ROLL_TYPES = ("A-Roll", "B-Roll")


def parse_custom_beats(text: str) -> list[tuple[str, str, str]]:
    """
    解析自定义节拍：每行「节拍名 | A-Roll 或 B-Roll | 这一镜的任务」，roll 可省略（默认 A-Roll）。
    镜头数不在 MIN_BEATS–MAX_BEATS 时报错（H3 每镜单独渲染，镜头太多成本高、一致性也难保证）。
    """
    beats = []
    for line in str(text or "").splitlines():
        parts = [p.strip() for p in re.split(r"[|｜]", line) if p.strip()]
        if not parts:
            continue
        name = re.sub(r"^(?:\d+[.、)）]|[-*·•])\s*", "", parts[0]).strip()[:8]
        roll = next((r for r in ROLL_TYPES if len(parts) > 1 and parts[1].upper() == r.upper()), None)
        rest = parts[2:] if roll else parts[1:]
        if name:
            beats.append((name, roll or "A-Roll", "；".join(rest) or name))
    if not MIN_BEATS <= len(beats) <= MAX_BEATS:
        raise ValueError(f"自定义节拍需要 {MIN_BEATS}–{MAX_BEATS} 行，每行「节拍名 | A-Roll/B-Roll | 任务」，"
                         f"现在是 {len(beats)} 行")
    return beats


def resolve_template(story_template: str, custom_beats: str = "") -> tuple[str, dict | None]:
    """返回 (显示名, 模板)。自定义节拍解析成与内置模板同结构；模板名不在表里时返回 None（按自由文本使用）"""
    if story_template == CUSTOM_TEMPLATE:
        return "自定义节拍", {"type": "自定义", "role": "带货编导：严格按用户给定的节拍创作", "rules": [],
                           "desc": "用户自定义的节拍", "beats": parse_custom_beats(custom_beats)}
    return story_template, STORY_TEMPLATES.get(story_template)

# ── 目标平台：决定画幅（影响层 2 生图尺寸）、剧本节奏、镜头时长和推荐模板 ──────────
# duration：单镜时长范围（秒）。H3 下限 5 秒，上限见 MAX_SEC
# image_size：gpt-image 2 系列接受 16 的倍数的自定义尺寸，这里取精确的 9:16 / 16:9；
#             平台不接受时层 2 自动换成同方向的标准尺寸（1024x1536 / 1536x1024）
# templates：适合该平台的剧本模板，第一个是推荐；所选模板不在其中时界面会提示
PLATFORMS: dict[str, dict] = {
    "抖音/快手（竖版 9:16）": {
        "aspect_ratio": "9:16", "image_size": "1008x1792", "duration": (5.0, 6.0),
        "templates": ["经典反转 · 4镜", "痛点-解决 · 5镜", "口播种草 · 4镜", "对比实验 · 4镜", "开箱测评 · 5镜",
                      "种草分享 · 5镜", "节日营销 · 4镜"],
        "guide": "竖屏全屏观看，前 2 秒必须有强钩子；节奏快、镜头短；口播口语化、有网感；主体居中，上下留出字幕区。",
    },
    "视频号（竖版 9:16）": {
        "aspect_ratio": "9:16", "image_size": "1008x1792", "duration": (6.0, 8.0),
        "templates": ["种草分享 · 5镜", "产地故事 · 5镜", "口播种草 · 4镜", "痛点-解决 · 5镜", "经典反转 · 4镜",
                      "节日营销 · 4镜"],
        "guide": "用户偏熟龄、重信任；节奏可稍缓，突出真实感、生活化和口碑；避免过度夸张的表演。",
    },
    "淘宝/京东 主图视频（方形 1:1）": {
        "aspect_ratio": "1:1", "image_size": "1024x1024", "duration": (5.0, 7.0),
        "templates": ["商品展示 · 4镜", "使用教程 · 4镜", "对比实验 · 4镜", "开箱测评 · 5镜", "痛点-解决 · 5镜"],
        "guide": "用户已有购买意向，重点讲清卖点和细节；产品占画面主体，剧情可弱化，B-Roll 比重提高。",
    },
    "淘宝/京东 详情页（横版 16:9）": {
        "aspect_ratio": "16:9", "image_size": "1792x1008", "duration": (6.0, 9.0),
        "templates": ["使用教程 · 4镜", "痛点-解决 · 5镜", "产地故事 · 5镜", "商品展示 · 4镜", "开箱测评 · 5镜"],
        "guide": "横屏构图，信息完整；适合展示使用场景全景和多卖点并列，口播偏讲解型。",
    },
}
DEFAULT_PLATFORM = "抖音/快手（竖版 9:16）"
# 每秒口播字数（约 4 字/秒），用于按平台时长换算口播字数上限
SPEECH_CHARS_PER_SEC = 4


def platform_duration(platform: str) -> tuple[float, float]:
    return tuple(PLATFORMS.get(platform, PLATFORMS[DEFAULT_PLATFORM]).get("duration", (5.0, 6.0)))


def voiceover_limit(platform: str) -> int:
    """每镜口播字数上限：按平台时长上限换算，留 0.8 秒起止留白"""
    return int((platform_duration(platform)[1] - 0.8) * SPEECH_CHARS_PER_SEC)


def template_conflict(platform: str, template: str) -> str:
    """所选模板不适合该平台时返回提示，否则空字符串（自定义节拍不提示）"""
    plat = PLATFORMS.get(platform)
    if not plat or template not in STORY_TEMPLATES or template in plat["templates"]:
        return ""
    return f"「{template}」不太适合{platform}，推荐「{plat['templates'][0]}」"


# ── H3 能力边界：写进每次剧本提示词，避免写出渲染不出来的剧本 ────────────────────
H3_LIMITS = """\
【视频模型能力边界（必须遵守，否则渲染不出来）】
1. 每个镜头单独渲染，镜头之间没有记忆：人物、服装、场景靠 script 里的设定文字和定妆照对齐，
   不要写依赖上一镜画面细节的动作（如「接着上一镜没喝完的那杯」）。
2. 一个镜头 = 一个连续画面，镜头内不切镜、不分屏、不画中画。
3. 画面里不出现字幕、花字、价格、Logo 动画等后期文字；需要文字的信息放在 voiceover，或留给后期剪辑加。
4. 每镜最多 2 个人物同时出现，动作简单清楚；不写复杂的手部精细操作（如拧细小的螺丝、写字）。
5. 产品是刚体，不要写产品变形、液体从包装里大量倾泻、包装上的文字变化之类的画面。"""

# 一个剧本最多向用户索要的真实产品图数量
MAX_PRODUCT_REFS = 3
DEFAULT_REF_DESC = "产品主图：包装正面平视，背景干净，品牌名与包装文字清晰可辨"


# ── 系统提示词 ────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """\
你是资深短视频带货编导，熟悉抖音、快手、视频号、淘宝和京东的内容规则，也熟悉 MiniMax H3 视频生成模型的能力边界。
根据商品信息和用户指定的视频类型，输出一个结构化的带货分镜 JSON。本次的具体编导角色和类型规则见用户消息里的【视频类型】。

【创作原则】
1. 不要写直白的电视购物/硬广，不要罗列功能参数；产品开场的类型（如「商品展示」）用画面和一句利益点开场，而不是念参数。
2. 结构必须严格遵循用户给定的【剧情叙事节拍】：镜头数、顺序和 emotion_beat 与节拍表一一对应。
   - A-Roll（人戏）：以人物为主，承担钩子、共鸣和情绪；
   - B-Roll（物戏）：产品高光特写，必须是刚体静止、电影级扫光、微距运镜（防变形核心）。
3. 人物与场景一致性：先在 script 中定好角色和场景设定，所有镜头只引用这些设定；
   同一角色全片服装、发型、妆容不变（除非剧情需要，并在 full_text 中写明）。
4. 只用用户提供的信息：不编造参数、数据、认证、销量、产地故事和用户评价。用户提供的买家评价只用来提炼痛点和用语，
   不要在台词里当作“用户评价”引用。遵守《广告法》，不用极限词，不做医疗功效宣称；
   不换个说法暗示功效（如「守护血管」「喝出好睡眠」），画面也不暗示疗效（如捂着胸口/喉咙转为舒展、医院和药品场景）。
5. 每条核心卖点至少在一个镜头里用画面或口播呈现出来；有【类目手册】时按手册的要点组织。
6. 有【编剧技能】时，用技能里的方法提升钩子、冲突和台词质量，但节拍、时长、字数和能力边界以本提示为准。
7. 代入感：人物镜头要让目标观众觉得「这就是我」。按目标人群画像选具体的地点、时间和道具
   （如「下午三点的工位，键盘旁的玻璃杯，窗外阴天」），人物是普通人的长相、穿着和日常动作，
   不是模特摆拍；场景里留一点真实的生活痕迹（桌上的文件、没叠的毯子），不要样板间和影棚。

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
- duration_sec: 时长（按用户给定的平台单镜时长范围；H3 模型要求 ≥ 5.0）
- mode: 只能取 "T2VA" 或 "Ref2VA"。没有产品入画的镜头用 "T2VA"；凡是产品入画（包括 B-Roll 特写、人物手持/使用产品）必须用 "Ref2VA" 并填写 ref_needed（锁定真实产品特征，保证刚体无变形）；不要使用 I2VA / R2V
- voiceover: 中文台词或画外音（每镜不超过用户给定的字数上限，口语、短句；字数多会被截断）
- visual_intent: 视觉构图描述（中文，给生图 AI 和人类导演看的方向）
- camera: 英文运镜指令（使用 H3 规范词：Arc Shot / Push In / Pull Out / Zoom In / Pan / Tilt / Static Shot 等，加 amplitude + speed）
- emotion_beat: 叙事节拍名，必须与用户给定节拍表中的名称完全一致
- image_gen_prompt: 英文生图提示词（给 gpt-image 用）。B-Roll 写极致的产品细节、光线、质感。
  A-Roll 写成一个具体的生活瞬间：地点 + 时间 + 光线来源 + 人物在做的具体动作 + 2-3 个真实道具
  （如 "a cluttered office desk at 3 pm, overcast window light, a woman in a grey cardigan blowing on a glass mug"），
  不写 cinematic、beautiful、perfect 这类空泛词，不写影棚和纯色背景
- ref_needed: Ref2VA 镜头填所用真实产品图的 id（必须是 product_refs 中的 id，如 "Ref1"）；T2VA 镜头填 null。
- characters: 本镜头出场角色 id 列表（如 ["C1"]），无人物填 []
- scene: 本镜头所在场景 id（如 "S1"）
- image_path: 留空字符串（由层 2 回填）
系统会按 scene / characters 字段把场景的 description_en 和角色的 appearance_en 原文拼进生图提示词，
所以 image_gen_prompt 只写这一镜特有的动作、构图、景别和道具，不要重复抄写设定，更不要改写设定里的外貌和陈设。

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

# ── 第一步：先定创意，再展开分镜 ────────────────────────────────────────────────
# 一次调用同时写创意和十几个字段的 JSON，模型的注意力大多花在格式上，钩子和台词容易平庸。
# 先只想「这条视频凭什么让人停下来」：给 3 个方向，自评后选一个，再按节拍展开
CONCEPT_COUNT = 3
CONCEPT_SYSTEM = """\
你是资深短视频带货创意总监。先不写分镜，只想清楚这条视频凭什么让目标观众停下来看完、愿意下单。

给出 3 个差异明显的创意方向（切入角度不同，不只是换句台词），每个包含：
- angle：切入角度，一句话（如「办公室下午犯困的那一刻」「一层白霜是什么」）
- hook_visual：第一镜的画面，具体到人物、动作、地点和道具，前 2 秒就能看懂
- hook_line：第一镜的口播，口语、短句，不超过用户给定的单镜字数上限
- conflict：贯穿全片的冲突或悬念，一句话
- payoff：结尾的回报 / 购买理由，必须来自用户给的卖点，不编造
- score：按「停留率、可信度、与卖点的关联、能否被视频模型渲染」综合打 1-10 分
- risk：这个方向最可能翻车的地方（如太像硬广、需要复杂动作、有功效暗示风险）

规则：
1. 只用用户提供的信息，不编造数据、认证、评价和产地故事；不用极限词，不做、也不暗示医疗功效（画面也不暗示）。
2. 3 个方向都要能放进用户给定的节拍结构，并符合目标平台和视频类型的要求。
3. chosen 填你推荐的方向序号（从 0 开始），reason 一句话说明为什么选它。

只输出 JSON：{"concepts": [{...}, {...}, {...}], "chosen": 0, "reason": "..."}"""
CONCEPT_FIELDS = ("angle", "hook_visual", "hook_line", "conflict", "payoff", "risk")


def normalize_concepts(raw: dict, playbook: str | None = None) -> dict:
    """
    清洗创意方向：字段转成字符串、score 限定 1-10。选定的方向口播或画面有合规风险时，
    改选分数最高的无风险方向（模型自评常忽略合规）。全部无效时返回 {"concepts": []}
    """
    items = raw.get("concepts") if isinstance(raw, dict) else None
    concepts = []
    for c in items if isinstance(items, list) else []:
        if not isinstance(c, dict):
            continue
        item = {k: str(c.get(k) or "").strip()[:200] for k in CONCEPT_FIELDS}
        if not (item["angle"] and item["hook_line"]):
            continue
        try:
            item["score"] = min(max(int(float(c.get("score"))), 1), 10)
        except (TypeError, ValueError):
            item["score"] = 5
        text = "\n".join(item[k] for k in CONCEPT_FIELDS if k != "risk")
        item["compliance"] = (qc.check_compliance(text, playbook, placeholders=False)
                              + qc.check_visual(item["hook_visual"]))
        concepts.append(item)
    concepts = concepts[:CONCEPT_COUNT]
    if not concepts:
        return {"concepts": []}
    try:
        chosen = int(raw.get("chosen"))
    except (TypeError, ValueError):
        chosen = -1
    reason = str(raw.get("reason") or "").strip()[:200]
    if not 0 <= chosen < len(concepts):
        chosen = max(range(len(concepts)), key=lambda i: concepts[i]["score"])
    if concepts[chosen]["compliance"]:
        clean = [i for i, c in enumerate(concepts) if not c["compliance"]]
        if clean:
            chosen = max(clean, key=lambda i: concepts[i]["score"])
            reason = "模型推荐的方向有合规风险，改选无风险方向里分数最高的"
    return {"concepts": concepts, "chosen": chosen, "reason": reason}


def concept_block(concepts: dict) -> str:
    """把选定的创意写进分镜提示词；其余方向只作参考，不混用"""
    if not concepts.get("concepts"):
        return ""
    c = concepts["concepts"][concepts["chosen"]]
    return f"""
【创意方向（已选定，分镜必须按这个方向展开）】
- 切入角度：{c['angle']}
- 第一镜画面：{c['hook_visual']}
- 第一镜口播：{c['hook_line']}（可以微调字数，不要换掉这个钩子）
- 全片冲突 / 悬念：{c['conflict']}
- 结尾回报：{c['payoff']}
- 需要避开的风险：{c['risk'] or '无'}
"""


def generate_concepts(product_name: str, usp: list[str], context: str, playbook: str | None = None) -> dict:
    """第一步：3 个创意方向 + 选定一个。失败时返回 {"concepts": []}，分镜照常生成"""
    user = f"商品名称：{product_name}\n核心卖点：\n" + "\n".join(f"- {u}" for u in usp) + f"\n{context}"
    try:
        content, finish = _chat([{"role": "system", "content": CONCEPT_SYSTEM},
                                 {"role": "user", "content": user}], temperature=0.9)
        return normalize_concepts(json.loads(_strip_code_fence(content)), playbook)
    except Exception as e:      # 创意这一步失败不影响出分镜
        print(f"[层1] 创意方向生成失败，直接生成分镜：{e}")
        return {"concepts": []}


def generate_storyboard(
    product_name: str,
    usp: list[str],
    brand_color: str = "#000000",
    story_template: str = DEFAULT_TEMPLATE,
    target_platform: str = DEFAULT_PLATFORM,
    agent_skill_content: str = "",
    max_refs: int = MAX_PRODUCT_REFS,
    product_context: str = "",
    playbook: str = cp.GENERAL,
    self_review: bool = False,
    custom_beats: str = "",
    concept_first: bool = False,
) -> dict:
    """
    调用 right.codes LLM，生成带货分镜 JSON。

    Args:
        product_name: 商品名称
        usp: 核心卖点列表
        brand_color: 品牌主色
        story_template: STORY_TEMPLATES 的键；CUSTOM_TEMPLATE 时用 custom_beats；不在表中时按自由文本节拍描述使用
        target_platform: PLATFORMS 的键；决定画幅、单镜时长、口播字数与剧本节奏
        agent_skill_content: 选择的编剧技能（SKILL.md 内容）
        max_refs: 最多向用户索要几张真实产品图
        product_context: 产品分析（痛点/人群/场景/背书等，见 product_brief.brief_context），可为空
        playbook: 类目手册 key（category_playbook），注入类目要点和类目合规
        self_review: 生成后按评分标准 + 程序检查结果让模型再改一轮（多一次 LLM 调用）
        custom_beats: 自定义节拍文本（每行「节拍名 | A-Roll/B-Roll | 任务」），story_template 为 CUSTOM_TEMPLATE 时使用
        concept_first: 先让模型出 3 个创意方向并选定一个，再按它展开分镜（多一次 LLM 调用）

    Returns:
        分镜 JSON dict，含 meta（concept_first 时含 meta.concepts）/ product_refs / shots / quality_issues
    """
    # 剧本模板 → 视频类型、编导角色、明确的镜头数与逐镜节拍
    template_name, tpl = resolve_template(story_template, custom_beats)
    if tpl:
        beats_text = "\n".join(
            f"{i}. 【{name}】({roll}) {task}" for i, (name, roll, task) in enumerate(tpl["beats"], 1)
        )
        rules = "".join(f"\n- {r}" for r in tpl.get("rules") or [])
        structure = (
            f"【视频类型】{tpl.get('type', '剧情带货')}\n编导角色：{tpl.get('role', '')}"
            + (f"\n类型规则：{rules}" if rules else "") + "\n\n"
            f"剧本模板：{template_name}（{tpl['desc']}）\n"
            f"请严格输出 {len(tpl['beats'])} 个镜头，按顺序一一对应以下节拍，emotion_beat 填节拍名：\n{beats_text}"
        )
    else:
        structure = f"剧情叙事节拍：{story_template}"

    # 目标平台 → 画幅 + 时长 + 平台风格
    plat = PLATFORMS.get(target_platform, PLATFORMS[DEFAULT_PLATFORM])
    lo, hi = platform_duration(target_platform)
    platform_text = (
        f"目标平台：{target_platform}\n"
        f"画幅：{plat['aspect_ratio']}（所有构图描述和 image_gen_prompt 都按此画幅设计）\n"
        f"单镜时长：{lo:g}–{hi:g} 秒；每镜口播不超过 {voiceover_limit(target_platform)} 字\n"
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
【编剧技能】
以下是用户选择的编剧技能。用其中的方法提升钩子、冲突、人物和台词，但不要改变节拍、时长、字数和能力边界：
---
{agent_skill_content}
---
""" if agent_skill_content else ""

    user_prompt = f"""
商品名称：{product_name}
核心卖点：{chr(10).join(f'- {u}' for u in usp)}
品牌主色：{brand_color}
{product_context}
{cp.prompt_block(playbook, "video")}
{platform_text}

{structure}

{H3_LIMITS}
{ref_info}
{skill_instruction}
我的商品是一款【{product_name}】，请严格按照上面的视频类型和节拍，策划这条带货视频的剧本和分镜。
明确指出每个镜头的角色、动作、场景，以及 B-Roll 怎么切入产品特写。
注意：无需产品入画的镜头标记为 "T2VA" 并在 ref_needed 填 null。凡是产品入画的镜头（B-Roll 特写，或人物手持/使用产品的 A-Roll），请明确标记为 "Ref2VA" 并指定 ref_needed（例如 "Ref1"）！千万不要用 I2VA，因为 I2VA 无法锁定真实产品特征！
"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}]
    concepts = {"concepts": []}
    if concept_first:
        # 第一步只想创意（和分镜同样的产品、平台、节拍和技能信息），选定的方向写进分镜提示词
        concepts = generate_concepts(product_name, usp, "\n".join(
            [product_context, cp.prompt_block(playbook, "video"), platform_text, structure, skill_instruction]),
            playbook)
        if concepts["concepts"]:
            messages[1]["content"] += concept_block(concepts)
    raw = _chat(messages)
    storyboard = _finalize(_parse_storyboard(*raw), lo, hi)
    beats = [b[0] for b in tpl["beats"]] if tpl else None
    check_kw = dict(beats=beats, vo_limit=voiceover_limit(target_platform), duration_range=(lo, hi),
                    playbook=playbook)
    issues = qc.check_storyboard(storyboard, usp, **check_kw)

    reviewed = False
    if self_review:
        try:
            revised_raw = _chat(messages + [
                {"role": "assistant", "content": raw[0]},
                {"role": "user", "content": qc.revision_request(qc.VIDEO_RUBRIC, issues)},
            ], temperature=0.4)
            revised = _finalize(_parse_storyboard(*revised_raw), lo, hi)
            new_issues = qc.check_storyboard(revised, usp, **check_kw)
            # 按严重程度比较：改稿引入新的 ❌ 或总分变差（模型偶尔删镜头）就保留初稿
            if qc.accept_revision(issues, new_issues):
                storyboard, issues, reviewed = revised, new_issues, True
        except Exception as e:      # 自检失败不影响初稿
            print(f"[层1] 模型自检失败，保留初稿：{e}")

    storyboard["meta"] = {
        "story_template": template_name,
        "video_type": (tpl or {}).get("type", ""),
        "platform": target_platform,
        "aspect_ratio": plat["aspect_ratio"],
        "image_size": plat["image_size"],
        "playbook": playbook,
        "self_reviewed": reviewed,
        # 全片共用同一个随机种子，进一步统一画风
        "seed": random.randint(0, 2**31 - 1),
    }
    if concepts["concepts"]:
        storyboard["meta"]["concepts"] = concepts
    if story_template == CUSTOM_TEMPLATE and tpl:
        storyboard["meta"]["custom_beats"] = [list(b) for b in tpl["beats"]]
    storyboard["quality_issues"] = issues
    return storyboard


def _chat(messages: list[dict], temperature: float = 0.7) -> tuple[str, str]:
    """调用 LLM，返回 (内容, finish_reason)。完整剧本 + 设定集输出很长，大模型常要 2 分钟以上；
    超时 / 断网 / 5xx 重试，共 LLM_RETRIES 次"""
    headers = {
        "Authorization": f"Bearer {RIGHT_CODES_LLM_KEY}",
        "Content-Type": "application/json",
        "Connection": "close"
    }
    payload = {
        "model": PLANNING_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 8192,  # 现在包含完整剧本 + 设定集，4096 容易被截断导致 JSON 解析失败
        "response_format": {"type": "json_object"}
    }

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
    return choice["message"]["content"] or "", choice.get("finish_reason") or ""


def _parse_storyboard(content: str, finish_reason: str = "") -> dict:
    raw = _strip_code_fence(content)
    try:
        storyboard = json.loads(raw)
    except json.JSONDecodeError as e:
        hint = "（输出被 max_tokens 截断，可减少镜头数后重试）" if finish_reason == "length" else ""
        raise Exception(f"LLM 返回的分镜不是合法 JSON{hint}：{e}") from e
    if not isinstance(storyboard, dict):
        raise Exception("LLM 返回的分镜不是 JSON 对象")
    return storyboard


def _finalize(storyboard: dict, min_sec: float = 5.0, max_sec: float = 6.0) -> dict:
    """整理 LLM 输出：镜头编号、时长、mode、角色/场景/产品图引用"""
    shots = [s for s in storyboard.get("shots", []) if isinstance(s, dict)]
    storyboard["shots"] = shots
    for i, shot in enumerate(shots, 1):
        # 镜头 id 统一按顺序重排为 1..N：下游文件名 shot_XX 用 int(id)，LLM 给 "S1"/"1A" 会串片
        shot["id"] = i
        # LLM 可能返回字符串或超出平台范围的时长
        try:
            duration = float(shot.get("duration_sec", 0))
        except (TypeError, ValueError):
            duration = 0.0
        duration = min(max(duration, min_sec), max_sec)
        # 按口播字数兜底时长（约 4 字/秒 + 起止留白），避免台词被截断；可超出平台上限，但不超过 H3 上限
        speech_sec = len(re.sub(r"\s", "", str(shot.get("voiceover") or ""))) / SPEECH_CHARS_PER_SEC + 0.8
        shot["duration_sec"] = round(min(max(duration, MIN_SEC, speech_sec), MAX_SEC), 1)
        # 统一 mode 写法，下游各层按规范值判断
        shot["mode"] = normalize_mode(shot.get("mode"))
        # 确保 image_path 字段存在
        shot.setdefault("image_path", "")

    storyboard["script"] = _normalize_script(storyboard)
    # 先按 LLM 原始 mode 整理产品图，再把有角色出场的镜头升级为 Ref2VA（顺序不能反，
    # 否则人物镜头会被误判为需要产品图）
    storyboard["product_refs"] = _normalize_product_refs(storyboard)
    _apply_character_refs(storyboard)
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
