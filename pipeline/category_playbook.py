"""
类目手册：让「类目」真正影响卖点分析、剧本、主图/详情页和合规检查

每个类目一份配置：
  - keywords：从「类目 / 商品名称 / 描述」自动识别用的关键词（正则）
  - focus：买家在这个类目做决定时看什么，卖点和文案按这些角度组织
  - required：这个类目必须交代清楚的信息，输入体检按它提示缺什么
  - video / ecom：给剧本和图文策划的类目要点
  - visual：画面方向（英文，写进生图提示词）
  - detail_overrides：详情页 15 屏里换成类目专属内容的屏（序号从 1 起）
  - compliance：类目专属的风险用语（关键词初筛，不能替代法务审核）

识别不到时用「通用」，与没有手册时的行为一致。本文件只放数据和纯函数，不依赖其它层。

自定义类目手册：内置类目穷举不完（如代用茶），用户可以新建手册存成 pipeline/playbooks/*.json，
key 为「custom:类目名」。自定义手册指定一个内置手册作 parent，类目风险词在 parent 的基础上只增不减，
空着的字段沿用 parent。自动识别时先查自定义手册（更专门），再查内置手册。
"""

import json
import re
from pathlib import Path

import categories as cats

AUTO = "auto"
GENERAL = "general"
CUSTOM_PREFIX = "custom:"
CUSTOM_DIR = Path(__file__).resolve().parent / "playbooks"

PLAYBOOKS: dict[str, dict] = {
    "pet": {
        "label": "宠物用品 / 宠物食品",
        "keywords": r"宠物|猫粮|狗粮|猫砂|猫咪|狗狗|犬|猫|狗|驱虫|鸟食|仓鼠|鱼缸",
        "focus": "适口性（宠物爱不爱吃/用）、成分与安全、适用对象（种类/年龄/体重）、喂养是否省事、宠主的具体困扰",
        "required": [
            ("适用对象", r"犬|猫|狗|幼[猫犬]|成[猫犬]|月龄|体重|公斤|kg", "写清适用的宠物种类、年龄或体重"),
            ("用法用量", r"用量|每[天日]|饲喂|喂食|片/|克/|g/", "写清每天喂多少、怎么喂"),
            ("成分 / 原料", r"成分|原料|配料|组成", "列出主要原料"),
            ("资质", r"许可证|饲预|饲添|执行标准|登记证|备案", "生产许可证或执行标准，证据类画面要用"),
        ],
        "video": "主角是宠主和宠物的关系；用宠物的真实行为（进食、玩耍、安静下来）证明卖点，不拟人化夸大效果",
        "ecom": "主图突出宠物进食/使用的瞬间和适用对象；详情页讲清成分、喂法和资质",
        "visual": "warm home lifestyle with a real-looking pet, natural window light, soft fur detail",
        "detail_overrides": {
            2: "痛点共鸣：宠主的具体困扰（行为、喂养难题），用宠主口吻",
            8: "成分与配方：原料组成和作用方向（不写治疗、预防疾病）",
            9: "资质证据：生产许可证、执行标准（只用用户提供的编号，否则【占位】）",
            11: "适用对象：适合的种类、年龄、体重；哪些情况不适用",
            12: "喂养方法：按体重/天的用量和喂法（①②③）",
            13: "适口性与细节：形态、气味、宠物进食特写",
        },
        "compliance": [r"预防|治疗|治愈|疾病|药效|兽药|处方|疗程", r"中药配方|中药成分|药用|药材",
                       r"安神|镇静|助眠", r"替代绝育|抑制发情|停止发情"],
        "compliance_hint": "宠物饲料/营养补充剂不得宣称预防、治疗疾病或药用功效",
    },
    "baby": {
        "label": "母婴",
        "keywords": r"母婴|婴儿|婴幼儿|宝宝|新生儿|儿童|奶粉|纸尿裤|尿不湿|奶瓶|辅食|孕妇|童装|玩具",
        "focus": "安全与材质、适用月龄/年龄、家长是否省心省力、宝宝是否配合",
        "required": [
            ("适用年龄", r"月龄|岁|个月|新生儿|适用年龄|阶段", "写清适用的月龄/年龄段"),
            ("安全 / 标准", r"标准|GB|3C|CCC|认证|检测|食品级", "执行标准或检测（编号可留空，文案会用【占位】）"),
        ],
        "video": "从家长视角讲真实育儿场景；安全感靠细节画面（材质、结构）证明，不制造焦虑",
        "ecom": "主图首先让家长放心（材质、安全）；详情页讲清适用阶段和使用细节",
        "visual": "soft bright nursery, gentle pastel palette, clean and safe-looking surfaces",
        "detail_overrides": {
            2: "痛点共鸣：家长的担心（安全、省心、宝宝不配合）",
            8: "材质与安全：材质、接触安全（标准编号用【占位】）",
            9: "安全证据：检测、执行标准、认证（编号用【占位】）",
            11: "适用月龄 / 年龄：哪些阶段适合",
            12: "使用方法：①②③，突出家长单手可操作等细节",
        },
        "compliance": [r"替代母乳|代替母乳|母乳化|接近母乳", r"益智|提高智力|增强抵抗力|提高免疫力|防过敏",
                       r"无刺激|零刺激"],
        "compliance_hint": "婴幼儿产品不得宣称替代母乳、益智或增强免疫力",
    },
    "health": {
        "label": "保健食品 / 营养补充",
        "keywords": r"保健|营养补充|膳食补充|维生素|益生菌|钙片|蛋白粉|鱼油|胶原蛋白|叶黄素|蓝帽",
        "focus": "成分与每份含量、适宜人群、食用方法、资质（批准文号/备案）",
        "required": [
            ("成分含量", r"成分|含量|每[份粒片]|mg|毫克|IU", "主要成分及每份含量"),
            ("资质", r"蓝帽|国食健|卫食健|批准文号|备案|食健备", "保健食品批准文号或备案号"),
            ("适宜人群", r"适宜人群|不适宜|适用人群|人群", "适宜与不适宜人群"),
        ],
        "video": "讲生活场景里的补充习惯，不演「吃了就好」的前后对比，不涉及疾病",
        "ecom": "主图突出成分与适宜人群；详情页必须有资质和「本品不能代替药物」提示",
        "visual": "clean bright kitchen or desk, morning light, fresh and trustworthy mood",
        "detail_overrides": {
            8: "成分与含量：主要原料与每份含量（只写有据的数字）",
            9: "资质证据：批准文号/备案号（用【占位】）与生产资质",
            11: "适宜人群与不适宜人群",
            12: "食用方法：用量、时间（①②③）",
            15: "注意事项与收口：「本品不能代替药物」等提示 + 售后",
        },
        "compliance": [r"治疗|预防|疾病|药效|处方", r"降血压|降血糖|降血脂|抗癌|防癌|抗肿瘤",
                       r"增强免疫力|提高免疫力|排毒"],
        "compliance_hint": "保健食品广告不得涉及疾病预防、治疗功能，需声明「本品不能代替药物」",
    },
    "beauty": {
        "label": "美妆护肤",
        "keywords": r"美妆|护肤|彩妆|精华|面霜|乳液|爽肤水|化妆水|口红|唇|粉底|面膜|洁面|洗面|防晒|香水|眼霜|卸妆",
        "focus": "适合什么肤质、质地与肤感、核心成分（有据）、使用前后感受（不承诺功效）",
        "required": [
            ("成分", r"成分|含量|%|浓度|配方|玻尿酸|烟酰胺|视黄醇", "核心成分（浓度、含量只写有据的）"),
            ("规格", r"ml|毫升|g|克|规格|容量", "容量/规格"),
            ("适用肤质", r"肤质|干皮|油皮|敏感|混合|中性|所有肤", "适合哪些肤质"),
        ],
        "video": "用质地、上脸、妆效特写证明卖点；前后对比只呈现即时肤感，不承诺长期功效",
        "ecom": "主图突出质地和肤感；详情页讲清成分、肤质适配和用法",
        "visual": "soft diffused beauty lighting, glossy texture close-ups, clean vanity setting",
        "detail_overrides": {
            8: "成分与配方：核心成分及作用（浓度、含量只写有据的数字）",
            10: "质地与肤感：上脸的质地、吸收、肤感特写",
            11: "适用肤质：哪些肤质/季节/场景适合，哪些不建议",
            12: "使用方法：用量、步骤、搭配顺序（①②③）",
        },
        "compliance": [r"药妆|医学护肤|医美级|械字号", r"祛痘|淡斑|抗皱|消除皱纹|逆龄|冻龄|修复屏障"],
        "compliance_hint": "化妆品不得宣称医疗作用，美白/祛斑/防晒/防脱等特殊功效需有对应注册证",
    },
    "food": {
        "label": "食品饮料",
        # 「茶」单独出现也算食品（代用茶、莓茶、花草茶），但排除茶几、茶具这类家居用品
        "keywords": r"食品|零食|饮料|饮品|茶叶|代用茶|花草茶|养生茶|冲饮|茶(?![几具杯盘])|咖啡|白酒|红酒|啤酒|坚果|糕点|面包|调味|酱|大米|面条|方便面|肉脯|水果|蜂蜜|巧克力",
        "focus": "口味口感、原料与产地（有据）、配料表是否干净、食用场景、保质期与储存",
        "required": [
            ("配料", r"配料|原料|成分", "配料表"),
            ("规格", r"g|克|ml|毫升|袋|盒|瓶|罐|净含量", "净含量/规格"),
            ("保质期 / 储存", r"保质期|储存|贮藏|保存", "保质期和储存方式"),
        ],
        "video": "让人「看饿」：开袋、切面、冲泡、入口的特写和声音；口播讲口感不讲功效",
        "ecom": "主图突出食欲感和口味；详情页讲清原料、吃法、规格和储存",
        "visual": "appetizing food photography, warm light, steam or texture detail, shallow depth of field",
        "detail_overrides": {
            3: "产品亮相：一眼看清是什么、什么口味",
            8: "原料与产地：原料、产地、工艺（只写有据的）",
            10: "食用场景：什么时候吃/喝、怎么搭配",
            12: "食用方法 / 冲泡方法（①②③）",
            13: "口感与细节：切面、质地、冲泡后状态特写",
            14: "规格参数：净含量、配料表、保质期、储存方式",
        },
        "compliance": [r"降血压|降血糖|降血脂|降三高|抗癌|防癌|增强免疫力|提高免疫力|排毒|养颜",
                       r"改善睡眠|助眠|安神|保健功能|保健作用", r"治疗|预防疾病",
                       r"清热解毒|护肝|养肝|保肝|解酒|醒酒|降火|养胃|护胃",
                       r"降胆固醇|降尿酸|清除自由基|抗氧化|抗衰老|预防",
                       r"清咽|利咽|利嗓|止咳|祛痰|化痰|消肿|解热|退烧|润肺",
                       r"(?:改善|提高|提升).{0,4}睡眠|安眠"],
        "compliance_hint": "普通食品不得宣称保健或疾病相关功效",
    },
    "digital": {
        "label": "3C 数码 / 家电",
        "keywords": r"手机|耳机|充电|数码|电脑|平板|键盘|鼠标|相机|音箱|手表|手环|家电|剃须刀|吹风机|空调|冰箱|洗衣机|扫地机|投影|电动",
        "focus": "关键参数换成看得见的体验（续航、速度、噪音、画质）、兼容性、使用场景、保修售后",
        "required": [
            ("关键参数", r"型号|参数|mAh|毫安|\dW|瓦|Hz|GB|功率|电池|续航|分辨率|尺寸", "关键参数（续航、功率、尺寸等）"),
            ("保修 / 售后", r"保修|质保|售后|包换", "保修期限或售后政策"),
        ],
        "video": "用演示代替讲解：真实使用场景里展示参数带来的结果（如充电 10 分钟后的电量）",
        "ecom": "主图一图一个体验点；详情页把参数翻译成用户能感知的结果，讲清兼容和保修",
        "visual": "sleek tech product photography, dark gradient or clean desk setup, crisp rim light",
        "detail_overrides": {
            4: "核心卖点 1：最重要的体验提升（参数换成看得见的结果）",
            7: "对比：与普通做法/旧款的体验对比（不点名竞品）",
            8: "核心参数与技术：关键参数及其带来的体验",
            11: "兼容与适配：适用机型、接口、场景",
            12: "上手使用：开箱到使用的 2-4 步（①②③）",
            15: "保修与售后：质保期限、退换、客服（未提供的用【占位】）",
        },
        "compliance": [r"零辐射|无辐射|军工级|航天级|医疗级", r"永不卡顿|永不掉线|永不损坏"],
        "compliance_hint": "不得使用无依据的「军工级」「零辐射」等宣称",
    },
    "apparel": {
        "label": "服饰鞋包",
        "keywords": r"服装|服饰|女装|男装|童鞋|裤|裙|鞋|靴|背包|手提包|帽|袜|外套|T恤|卫衣|衬衫|毛衣|内衣|饰品|首饰|围巾",
        "focus": "上身效果与版型、面料材质（成分有据）、尺码是否准、搭配场景、洗护",
        "required": [
            ("面料 / 材质", r"面料|材质|棉|涤|羊毛|羊绒|真皮|皮革|聚酯|成分|含量", "面料成分"),
            ("尺码", r"尺码|码数|\b(?:XS|S|M|L|XL|XXL)\b|尺寸|身高|体重", "尺码范围或尺码表"),
        ],
        "video": "用上身、走动、细节特写展示版型和面料；场景切换体现百搭",
        "ecom": "主图以模特上身为主；详情页讲面料、尺码建议和搭配",
        "visual": "editorial fashion photography, natural daylight, full-body and fabric texture details",
        "detail_overrides": {
            3: "上身效果：模特整体穿搭，一眼看到版型",
            8: "面料与做工：面料成分（只写有据的）、手感、走线",
            10: "搭配场景 1：通勤/日常穿搭",
            11: "搭配场景 2：休闲/出游穿搭",
            12: "尺码建议：尺码表（用 specs 或【占位】）+ 选码建议",
            14: "规格与洗护：颜色、尺码、洗涤保养",
        },
        "compliance": [r"防螨|除螨|远红外|负离子|磁疗|理疗|矫正"],
        "compliance_hint": "服饰不得宣称理疗、矫正等功效",
    },
    "home": {
        "label": "家居日用",
        "keywords": r"家居|日用|收纳|清洁|厨具|锅|碗|水杯|床品|枕|被子|毛巾|洗衣液|纸巾|香薰|台灯|拖把|垃圾",
        "focus": "解决了哪个具体麻烦、尺寸是否合适、材质是否安全耐用、清洁维护是否省事",
        "required": [
            ("尺寸 / 规格", r"尺寸|cm|厘米|mm|规格|容量|ml|升", "尺寸或容量"),
            ("材质", r"材质|材料|不锈钢|硅胶|棉|塑料|PP|玻璃|陶瓷|木", "材质"),
        ],
        "video": "用前后对比和真实家务场景展示省事；尺寸用参照物表现",
        "ecom": "主图突出解决的麻烦和使用后的效果；详情页讲清尺寸适配和清洁维护",
        "visual": "bright tidy home interior, natural light, real-life scale reference",
        "detail_overrides": {
            4: "核心卖点 1：解决的具体麻烦，用前后对比画面",
            8: "材质与工艺：材质安全、耐用细节",
            11: "尺寸适配：放在哪里、多大空间、适配什么",
            12: "使用与清洁维护（①②③）",
        },
        "compliance": [r"除甲醛|净化空气|除菌率|杀菌率|防霉"],
        "compliance_hint": "除醛、除菌等功效需有检测依据",
    },
    GENERAL: {
        "label": "通用",
        "keywords": "",
        "focus": "",
        "required": [],
        "video": "", "ecom": "", "visual": "",
        "detail_overrides": {},
        "compliance": [],
        "compliance_hint": "",
    },
}

# 自动识别的优先级：越专门的类目越靠前（「宠物营养补充剂」应归宠物而不是保健）
BASE_DETECT_ORDER = ("pet", "baby", "health", "beauty", "food", "digital", "apparel", "home")
# 自定义手册的字段（与内置手册一致）；字符串字段空着时沿用 parent
TEXT_FIELDS = ("focus", "video", "ecom", "visual", "compliance_hint")
# 商用标准包多出来的字段（见 category_packs）；自定义手册也可以写，空着时沿用 parent
PACK_FIELDS = ("label_items", "label_basis", "photo_checklist", "evidence", "positioning_plans")


def _inherit(parent: dict, child: dict) -> dict:
    """子手册在 parent 之上：空着的文字字段沿用 parent；必填项、屏序覆盖、合规风险词只增不减。
    标准包字段（标签必备项、照片清单等）子手册没写就沿用 parent；子手册自己改了屏序（detail_overrides）时，
    不再沿用 parent 的整套定位屏序，免得改的屏被覆盖掉"""
    pb = {**child, **{f: child.get(f) or parent.get(f, "") for f in TEXT_FIELDS}}
    pb["required"] = list(parent["required"]) + [r for r in child.get("required") or []
                                                 if r[0] not in {p[0] for p in parent["required"]}]
    pb["detail_overrides"] = {**parent["detail_overrides"], **(child.get("detail_overrides") or {})}
    # 只增不减：parent 的风险词是底线，子手册不能把它们删掉
    pb["compliance"] = list(dict.fromkeys(list(parent["compliance"]) + list(child.get("compliance") or [])))
    pb["label_basis"] = child.get("label_basis") or parent.get("label_basis") or ""
    for f in ("label_items", "photo_checklist", "evidence"):
        pb[f] = list(child.get(f) or parent.get(f) or [])
    own_order = bool(child.get("detail_overrides"))
    pb["positioning_plans"] = dict(child.get("positioning_plans") or
                                   ({} if own_order else parent.get("positioning_plans") or {}))
    return pb


# 商用标准包（categories/ 下的类目）注册为内置手册，排在各自 parent 前面识别
for _cat in cats.registered():
    if _cat.playbook:
        PLAYBOOKS[_cat.key] = {**_inherit(PLAYBOOKS[_cat.parent], _cat.playbook), "parent": _cat.parent}
STANDARD_KEYS = tuple(c.key for c in cats.registered() if c.playbook)
DETECT_ORDER = tuple(k for base in BASE_DETECT_ORDER
                     for k in [c.key for c in cats.registered() if c.playbook and c.parent == base] + [base])
BUILTIN_KEYS = DETECT_ORDER + (GENERAL,)


# ── 自定义手册：读写 pipeline/playbooks/*.json ─────────────────────────────────
def _slug(name: str) -> str:
    """文件名：保留中英文和数字，其它字符换成 _"""
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "_", str(name or "")).strip("_")
    return s[:40] or "custom"


def _valid_regex(p: str) -> bool:
    try:
        re.compile(p)
        return True
    except re.error:
        return False


def _keyword_list(value) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[\s,，、|]+", value)
    return [str(k).strip() for k in (value or []) if str(k).strip()]


def normalize_custom(data: dict) -> dict:
    """
    清洗用户/LLM 给的自定义手册；非法正则直接丢掉，避免识别和合规检查时报错。
    keywords 保存为字面关键词列表（用户写的「莓茶」里不该有正则语法），识别时再拼成正则。
    """
    name = str(data.get("label") or data.get("name") or "").strip()[:20]
    if not name:
        raise ValueError("自定义类目手册需要一个名称")
    parent = data.get("parent") if data.get("parent") in BUILTIN_KEYS else GENERAL
    keywords = list(dict.fromkeys(_keyword_list(data.get("keywords")) + [name]))
    required = []
    for r in data.get("required") or []:
        if isinstance(r, dict):
            r = (r.get("name"), r.get("pattern"), r.get("tip"))
        if isinstance(r, (list, tuple)) and len(r) == 3 and all(r) and _valid_regex(str(r[1])):
            required.append((str(r[0])[:12], str(r[1]), str(r[2])[:60]))
    overrides = {}
    raw_overrides = data.get("detail_overrides")
    for k, v in (raw_overrides.items() if isinstance(raw_overrides, dict) else []):
        if str(k).isdigit() and 1 <= int(k) <= 15 and str(v).strip():
            overrides[int(k)] = str(v).strip()[:60]
    compliance = [str(p) for p in (data.get("compliance") or []) if str(p).strip() and _valid_regex(str(p))]
    out = {"label": name, "parent": parent, "keywords": keywords, "required": required,
           "detail_overrides": overrides, "compliance": compliance}
    for f in TEXT_FIELDS:
        out[f] = str(data.get(f) or "").strip()
    out.update(_normalize_pack_fields(data))
    return out


def _triples(value, n: int, regex_at: int | None = None) -> list[tuple]:
    """[[a, b, c], ...] 或 [{...}] → 元组列表；长度不对、正则非法的项丢掉"""
    out = []
    for r in value if isinstance(value, list) else []:
        if isinstance(r, dict):
            r = tuple(r.values())
        if isinstance(r, (list, tuple)) and len(r) == n and all(str(x).strip() for x in r[:2]):
            if regex_at is not None and not _valid_regex(str(r[regex_at])):
                continue
            out.append(tuple(r))
    return out


def _normalize_pack_fields(data: dict) -> dict:
    """自定义手册里的标准包字段（可选）：标签必备项、照片清单、需要依据的说法、各定位的完整屏序"""
    label_items = [(str(a)[:12], str(b), str(c)[:60]) for a, b, c in _triples(data.get("label_items"), 3, 1)]
    photos = [(str(a), str(b)[:40], bool(c)) for a, b, c in _triples(data.get("photo_checklist"), 3)]
    evidence = [(str(a), str(b)[:60]) for a, b in _triples(data.get("evidence"), 2, 0)]
    plans = {}
    raw = data.get("positioning_plans")
    for k, roles in (raw.items() if isinstance(raw, dict) else []):
        roles = [str(r).strip()[:60] for r in roles if str(r).strip()] if isinstance(roles, list) else []
        if len(roles) == 15:          # 详情页固定 15 屏，不够或多了的整套丢掉，退回通用定位屏序
            plans[str(k)] = roles
    return {"label_items": label_items, "photo_checklist": photos, "evidence": evidence,
            "label_basis": str(data.get("label_basis") or "").strip()[:120], "positioning_plans": plans}


def save_custom(data: dict) -> str:
    """保存自定义手册，返回它的 key（custom:名称）"""
    pb = normalize_custom(data)
    CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
    payload = {**pb, "required": [list(r) for r in pb["required"]],
               "detail_overrides": {str(k): v for k, v in pb["detail_overrides"].items()}}
    (CUSTOM_DIR / f"{_slug(pb['label'])}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return CUSTOM_PREFIX + pb["label"]


def delete_custom(key: str) -> bool:
    if not str(key).startswith(CUSTOM_PREFIX):
        return False
    path = CUSTOM_DIR / f"{_slug(key[len(CUSTOM_PREFIX):])}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def load_custom() -> dict[str, dict]:
    """读取全部自定义手册；格式不对的文件跳过（不影响内置手册）"""
    out = {}
    for path in sorted(CUSTOM_DIR.glob("*.json")) if CUSTOM_DIR.exists() else []:
        try:
            pb = normalize_custom(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, json.JSONDecodeError, AttributeError, TypeError):
            continue
        out[CUSTOM_PREFIX + pb["label"]] = pb
    return out


def _merged(custom: dict) -> dict:
    """自定义手册 + parent：空着的文字字段沿用 parent；合规词、必填项、屏序都在 parent 基础上叠加"""
    pb = _inherit(PLAYBOOKS[custom["parent"]], custom)
    pb.update(label=custom["label"], parent=custom["parent"],
              keywords="|".join(re.escape(k) for k in custom["keywords"]))
    return pb


def all_playbooks() -> dict[str, dict]:
    return {**{k: PLAYBOOKS[k] for k in BUILTIN_KEYS}, **{k: _merged(v) for k, v in load_custom().items()}}


def choices() -> list[tuple[str, str]]:
    """界面下拉框：(显示名, 值)"""
    custom = [(f"⭐ {pb['label']}（自定义）", k) for k, pb in load_custom().items()]
    return [("自动识别", AUTO)] + custom + [(PLAYBOOKS[k]["label"], k) for k in DETECT_ORDER] + \
           [(PLAYBOOKS[GENERAL]["label"], GENERAL)]


def detect(brief: dict) -> str:
    """按 类目 → 商品名称 → 描述 的顺序找关键词；先查自定义手册（更专门），再查内置手册"""
    custom = load_custom()
    for field in ("category", "name", "description"):
        text = str(brief.get(field) or "")
        if not text.strip():
            continue
        for key, pb in custom.items():
            if any(k.lower() in text.lower() for k in pb["keywords"]):
                return key
        for key in DETECT_ORDER:
            if re.search(PLAYBOOKS[key]["keywords"], text, flags=re.I):
                return key
    return GENERAL


def resolve(brief: dict) -> str:
    """用户手动选了类目手册就用它，否则自动识别；选中的自定义手册被删了就退回自动识别"""
    chosen = str(brief.get("playbook") or AUTO)
    if chosen in PLAYBOOKS or (chosen.startswith(CUSTOM_PREFIX) and chosen in load_custom()):
        return chosen
    return detect(brief)


def category(key: str | None) -> cats.Category:
    """
    这个类目手册怎么出图（主图拍什么、每屏找哪类照片、参考图怎么说明、画面要求）。
    自定义手册沿用 parent 的生成策略；没有专属策略的类目（通用、内置的轻量手册）用 categories.GENERIC，
    行为与重构前一致。
    """
    key = key or GENERAL
    if key.startswith(CUSTOM_PREFIX):
        custom = load_custom().get(key)
        return cats.get(custom["parent"] if custom else GENERAL)
    return cats.get(key)


def get(key: str | None) -> dict:
    key = key or GENERAL
    if key.startswith(CUSTOM_PREFIX):
        custom = load_custom().get(key)
        return _merged(custom) if custom else PLAYBOOKS[GENERAL]
    return PLAYBOOKS.get(key, PLAYBOOKS[GENERAL])


def is_custom(key: str | None) -> bool:
    return str(key or "").startswith(CUSTOM_PREFIX)


def label(key: str | None) -> str:
    return get(key)["label"]


def parent_of(key: str | None) -> str:
    """自定义手册的 parent；内置手册返回自己"""
    return get(key).get("parent") or (key if key in PLAYBOOKS else GENERAL)


# 让 LLM 起草自定义手册时用的字段说明（与内置手册同一套字段）
DRAFT_SYSTEM = """\
你是资深电商类目运营。用户的商品类目不在内置手册里，请参照下面的字段为它起草一份类目手册。

字段：
- label：类目名，≤ 10 字（如「代用茶」）
- parent：最接近的内置类目，只能取 {parents}。合规底线按 parent 继承，选错会漏查风险词，拿不准时选更严的
- keywords：识别这个类目的字面关键词，3-8 个（如 ["代用茶", "莓茶", "藤茶"]）
- focus：买家在这个类目做决定时看什么，一句话，列 4-6 个角度
- required：这个类目必须交代清楚的信息，2-4 项，每项 {{"name": "名称", "pattern": "能在产品描述里匹配到它的正则", "tip": "提示用户补什么"}}
- video：带货视频的类目要点，一句话
- ecom：主图 / 详情页的类目要点，一句话
- visual：画面方向，英文短句
- detail_overrides：详情页 15 屏里要换成类目专属内容的屏，{{"屏号": "新的屏作用"}}，屏号见用户给的通用屏序，2-6 屏
- compliance：这个类目常见的违规宣称（正则，多个词用 | 连接），2-5 条。只写有明确风险的
- compliance_hint：类目合规要点，一句话

只输出 JSON，不加解释。合规相关的内容只是初筛参考，不要写成法律结论。"""


def draft_request(brief: dict, base_detail_roles: list[str]) -> str:
    """起草自定义手册时发给 LLM 的用户消息"""
    roles = "\n".join(f"{i}. {r}" for i, r in enumerate(base_detail_roles, 1))
    return (f"商品名称：{brief.get('name') or ''}\n类目：{brief.get('category') or ''}\n"
            f"产品描述：{str(brief.get('description') or '')[:1500]}\n\n通用详情页屏序：\n{roles}")


def draft_system() -> str:
    return DRAFT_SYSTEM.format(parents=" / ".join(BUILTIN_KEYS))


def custom_to_text(key: str) -> str:
    """把自定义手册转成界面里可编辑的 JSON 文本"""
    custom = load_custom().get(key)
    if not custom:
        return ""
    return json.dumps({**custom, "required": [list(r) for r in custom["required"]],
                       "detail_overrides": {str(k): v for k, v in custom["detail_overrides"].items()}},
                      ensure_ascii=False, indent=2)


def compliance_patterns(key: str | None) -> list[str]:
    return list(get(key)["compliance"])


def detail_roles(base: list[str], key: str | None) -> list[str]:
    """详情页屏序：通用 15 屏，按类目替换部分屏的内容（屏数不变）"""
    overrides = get(key)["detail_overrides"]
    return [overrides.get(i, role) for i, role in enumerate(base, 1)]


def is_standard(key: str | None) -> bool:
    """商用标准包（或以它为 parent 的自定义手册）"""
    return bool(get(key).get("label_items"))


def missing_label_items(text: str, key: str | None) -> list[tuple[str, str]]:
    """包装标签必备项里，在 text（产品描述 + 背书 + 包装背面识别出的标签）中找不到的：[(名称, 提示)]"""
    return [(name, tip) for name, pattern, tip in get(key).get("label_items") or []
            if not re.search(pattern, text or "", flags=re.I)]


def unsupported_claims(copy: str, source: str, key: str | None) -> list[tuple[str, str]]:
    """文案里出现、但产品信息里没有依据的说法：[(命中的词, 提示)]。只是提醒，不判违规"""
    out = []
    for pattern, tip in get(key).get("evidence") or []:
        hit = re.search(pattern, copy or "", flags=re.I)
        if hit and not re.search(pattern, source or "", flags=re.I):
            out.append((hit.group(), tip))
    return out


def missing_required(brief: dict, key: str | None) -> list[tuple[str, str]]:
    """类目必填信息里，用户输入中找不到的项：[(名称, 提示)]"""
    text = " ".join(str(brief.get(k) or "") for k in ("name", "category", "description", "endorsements", "price"))
    return [(name, tip) for name, pattern, tip in get(key)["required"]
            if not re.search(pattern, text, flags=re.I)]


def prompt_block(key: str | None, purpose: str) -> str:
    """
    注入 LLM 的类目要点。purpose：analysis（卖点分析）/ video（剧本）/ ecom（主图详情页）
    通用类目返回空字符串，提示词与没有手册时一致。
    """
    pb = get(key)
    if not pb["focus"]:
        return ""
    lines = [f"【类目手册：{pb['label']}】", f"买家在这个类目主要看：{pb['focus']}"]
    if purpose == "analysis":
        lines.append("卖点按这些角度组织，有信息支撑的才写；缺的信息在 notes 里提示用户补充。")
    elif purpose in ("video", "ecom"):
        lines.append(f"{'剧本' if purpose == 'video' else '图文'}要点：{pb[purpose]}")
        lines.append(f"画面方向（英文提示词可参考）：{pb['visual']}")
    if pb["compliance_hint"]:
        lines.append(f"类目合规：{pb['compliance_hint']}")
    if pb.get("evidence"):
        lines.append("这些说法只有产品信息里写明了依据才能用：" + "；".join(tip for _, tip in pb["evidence"]))
    if purpose == "ecom" and pb.get("label_items"):
        lines.append(f"规格参数屏按标签必备项列（{pb['label_basis']}），产品信息里没有的写【占位】，不要编："
                     + "、".join(n for n, _, _ in pb["label_items"]))
    return "\n".join(lines)
