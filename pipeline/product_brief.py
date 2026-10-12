"""
产品信息 → LLM 卖点分析

用户填写产品信息（名称、类目、描述/参数、目标人群、价格区间、真实背书），
LLM 提炼：用户痛点、核心卖点（按说服力排序）、差异化、目标人群画像、使用场景。
结果在页面上可直接修改，或重新分析；视频剧本和主图/详情页策划都基于这份分析。

原则：只从用户提供的信息里提炼，不编造参数、数据、认证和评价。
"""

import json
import os
import re
import time
from pathlib import Path

import requests

from layer1_planning import RIGHT_CODES_BASE_URL, RIGHT_CODES_LLM_KEY, PLANNING_MODEL, LLM_TIMEOUT
import category_playbook as cp

BRIEF_PATH = Path(__file__).resolve().parent / "output" / "product_brief.json"

# 页面字段：用户输入（reviews：买家评价/问大家/竞品卖点原文；playbook：类目手册，auto 为自动识别）
INPUT_FIELDS = ("name", "category", "description", "audience", "price", "endorsements", "brand_color",
                "reviews", "playbook")
# 页面字段：LLM 分析结果（用户可改）
ANALYSIS_FIELDS = ("usp", "pain_points", "differentiation", "persona", "scenes")

DEFAULT_BRIEF = {
    "name": "某某玻尿酸原液精华",
    "category": "美妆护肤 · 精华",
    "description": "1000万分子玻尿酸，水感质地，3秒渗透不油腻，30ml 滴管瓶",
    "audience": "",
    "price": "",
    "endorsements": "",
    "brand_color": "#C8A882",
    "reviews": "",
    "playbook": cp.AUTO,
    "usp": "1000万分子玻尿酸\n3秒渗透不油腻",
    "pain_points": "",
    "differentiation": "",
    "persona": "",
    "scenes": "",
}

# 买家评价可能很长，注入提示词时截断，避免挤占输出 token
MAX_REVIEW_CHARS = 3000

ANALYZE_SYSTEM = """\
你是资深电商操盘手兼品牌策划，擅长从产品信息中提炼能打动用户的卖点。

【任务】根据用户提供的产品信息，输出：
1. pain_points：目标用户在这个品类上的真实困扰，1-3 条，用用户的口吻（如"涂完黏腻，上妆搓泥"）
2. usp：核心卖点 3-5 条，按说服力从强到弱排序。每条 ≤ 16 字，讲用户能感知的利益和结果，
   最好能对应一个痛点；参数只作为支撑，写在利益后面（如"3 秒吸收不黏腻（水感质地）"）
3. differentiation：与同类产品相比最核心的 1 个差异点，一句话
4. persona：目标人群画像，一句话（年龄段/身份/场景/诉求）
5. scenes：典型使用场景 2-4 个

【硬性规则】
1. 只从用户提供的信息中提炼和推理，不编造参数、成分含量、检测数据、认证、销量和用户评价。
   用户没给的数字不要写数字。
2. 背书只能引用用户在"真实背书"里提供的内容。
3. 遵守《广告法》：不用极限词（最、第一、唯一、顶级、国家级、100%、XX之王 等），不做医疗功效宣称。
   产品描述里出现的保健 / 医疗功效（降血压、消炎、抗肿瘤、改善睡眠、护肝等）一律不写进卖点，
   也不换个说法暗示（如「守护血管」「喝出好睡眠」「告别咽喉不适」）；改用可证明的事实
   （产地、原料、工艺、口感、用法），并在 notes 里提醒用户这些宣称有合规风险。
   含量、比例等数据只有用户在「真实背书」里给出检测来源时才能用。
4. 信息不足以支撑某项时，写出合理推断，并在 notes 里提示用户补充什么信息。
5. 用户提供了"买家评价 / 问大家 / 竞品卖点"时：痛点优先取买家原话里反复出现的困扰，
   用语贴近买家的说法；竞品卖点用来找差异化，不要照抄。评价内容不得改写成"用户说……"之类的背书。
6. 有【类目手册】时，按手册列出的角度组织卖点。

输出纯 JSON：
{"pain_points": ["..."], "usp": ["..."], "differentiation": "...", "persona": "...",
 "scenes": ["..."], "notes": "需要用户补充或核实的信息，没有则留空"}
"""


def load_brief() -> dict:
    try:
        data = json.loads(BRIEF_PATH.read_text(encoding="utf-8"))
        return {**DEFAULT_BRIEF, **{k: v for k, v in data.items() if isinstance(v, str)}}
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_BRIEF)


def save_brief(brief: dict):
    BRIEF_PATH.parent.mkdir(parents=True, exist_ok=True)
    BRIEF_PATH.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")


def lines_of(text: str) -> list[str]:
    """多行文本 → 列表，去掉空行和行首的序号/项目符号"""
    out = []
    for line in str(text or "").splitlines():
        line = re.sub(r"^\s*(?:[-*·•]|\d+[.、)）])\s*", "", line).strip()
        if line:
            out.append(line)
    return out


def _input_text(brief: dict) -> str:
    rows = [
        ("商品名称", brief.get("name")),
        ("类目", brief.get("category")),
        ("产品描述 / 参数 / 特性", brief.get("description")),
        ("目标人群（用户填写）", brief.get("audience")),
        ("价格区间", brief.get("price")),
        ("真实背书（证书/专利/检测/销量等）", brief.get("endorsements")),
        ("买家评价 / 问大家 / 竞品卖点（原文，只用来提炼痛点和用语）",
         str(brief.get("reviews") or "")[:MAX_REVIEW_CHARS]),
        ("用户已有的卖点想法", brief.get("usp")),
    ]
    body = "\n".join(f"{k}：{v.strip()}" for k, v in rows if v and str(v).strip())
    block = cp.prompt_block(cp.resolve(brief), "analysis")
    return f"{body}\n\n{block}" if block else body


def chat_json(system: str, user: str, temperature: float = 0.4, max_tokens: int = 3000) -> dict:
    """通用 JSON 对话：起草类目手册、评测打分等短输出任务共用"""
    old_np = os.environ.pop("NO_PROXY", None)
    old_np_l = os.environ.pop("no_proxy", None)
    try:
        resp = None
        for attempt in range(3):
            try:
                resp = requests.post(
                    f"{RIGHT_CODES_BASE_URL.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {RIGHT_CODES_LLM_KEY}",
                             "Content-Type": "application/json", "Connection": "close"},
                    json={"model": PLANNING_MODEL, "temperature": temperature, "max_tokens": max_tokens,
                          "response_format": {"type": "json_object"},
                          "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
                    timeout=LLM_TIMEOUT,
                )
                if resp.status_code < 500:
                    break
            except requests.exceptions.RequestException:
                if attempt == 2:
                    raise
            time.sleep(5)
    finally:
        if old_np is not None:
            os.environ["NO_PROXY"] = old_np
        if old_np_l is not None:
            os.environ["no_proxy"] = old_np_l

    if resp is None or resp.status_code != 200:
        raise Exception(f"LLM 请求失败 ({getattr(resp, 'status_code', '-')}): {getattr(resp, 'text', '')[:500]}")
    raw = resp.json()["choices"][0]["message"]["content"].strip()
    return json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw))


def draft_playbook(brief: dict, base_detail_roles: list[str]) -> dict:
    """识别不到类目时，让 LLM 按内置手册的字段起草一份自定义手册（返回清洗后的草稿，未保存）"""
    data = chat_json(cp.draft_system(), cp.draft_request(brief, base_detail_roles), max_tokens=2500)
    if not data.get("label"):
        data["label"] = str(brief.get("category") or "").strip()[:10] or "自定义类目"
    return cp.normalize_custom(data)


def analyze_product(brief: dict) -> tuple[dict, str]:
    """调用 LLM 分析卖点，返回 (分析结果字段 dict, 给用户的提示 notes)"""
    if not str(brief.get("name", "")).strip():
        raise ValueError("请先填写商品名称")
    data = chat_json(ANALYZE_SYSTEM, _input_text(brief), temperature=0.6)

    def join(v):
        if isinstance(v, list):
            return "\n".join(str(x).strip() for x in v if str(x).strip())
        return str(v or "").strip()

    result = {
        "usp": join(data.get("usp")),
        "pain_points": join(data.get("pain_points")),
        "differentiation": join(data.get("differentiation")),
        "persona": join(data.get("persona")),
        "scenes": join(data.get("scenes")),
    }
    return result, join(data.get("notes"))


def brief_context(brief: dict) -> str:
    """把产品信息 + 卖点分析整理成一段文字，注入视频剧本和图文策划的提示词"""
    rows = [
        ("类目", brief.get("category")),
        ("产品描述 / 参数", brief.get("description")),
        ("价格区间", brief.get("price")),
        ("目标人群", brief.get("persona") or brief.get("audience")),
        ("用户痛点", brief.get("pain_points")),
        ("差异化", brief.get("differentiation")),
        ("使用场景", brief.get("scenes")),
        ("真实背书（只能引用这里的内容，没有的不要编）", brief.get("endorsements")),
        ("买家原话（只用来贴近买家用语，不得当作评价引用）", str(brief.get("reviews") or "")[:800]),
    ]
    body = "\n".join(f"{k}：{str(v).strip()}" for k, v in rows if v and str(v).strip())
    return f"【产品分析（用户已确认）】\n{body}" if body else ""
