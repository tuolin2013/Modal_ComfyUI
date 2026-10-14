"""
代用茶（张家界莓茶 / 藤茶 / 花草茶）：类目手册 + 主图 / 详情页生成策略

法规依据（2026 年核对，只是初筛参考，上线前请法务确认）：
  代用茶属普通食品：GB 7718-2025《预包装食品标签通则》，2025-03-16 发布，2027-03-16 实施（过渡期 2 年），
  新标准要求标示生产日期和保质期到期日，不得使用「不添加」「不使用」类声称

照片类型的 key 见 asset_library.PHOTO_TYPES；拍法的 key 见 asset_library.SHOTS。
屏的作用文字会按 role_rules（本类目）→ asset_library.ROLE_RULES（通用）的顺序匹配来分配素材，改写屏序时注意用词
（如首屏不要写「产地」，否则会被当成产地屏去找原料照片）。
"""

from .base import Category

PHOTOS = [
    ("package_front", "包装正面（锁定产品外观）", True),
    ("package_back", "包装背面（配料、许可证编号、执行标准）", True),
    ("product_closeup", "干茶特写（白霜等外观特征看得清）", True),
    ("in_use", "冲泡中 / 茶汤", True),
    ("texture_detail", "叶底（泡开后的叶片）", False),
    ("raw_material", "原料植株 / 产地环境", False),
    ("certificate", "检测报告 / 许可证", False),
]

PLANS = {
    "value": [
        "首屏海报：产品 + 份量 + 一句核心利益（价格用【券后价】占位）",
        "价值量感：份量、包数，讲清为什么划算（价格用【券后价】占位，不编造价格和活动）",
        "核心卖点 1：最直观的外观特征（如莓茶的白霜），干茶特写",
        "冲泡方法：①②③ 开水直接冲，一次几克、几度水",
        "茶汤展示：汤色和透亮度",
        "对比：与普通做法对比（不点名竞品，只写可证明的事实）",
        "核心卖点 2：口感（回甘等用户能感知的描述）",
        "原料与产地：原料、产地（只写有据的）",
        "叶底特写：泡开后的叶片",
        "适用人群与场景：办公室、居家、待客",
        "规格参数：净含量、配料、保质期、贮存条件、生产许可证、执行标准",
        "品质证据：检测报告 / 许可证（只用用户提供的编号，否则【占位】）",
        "包装细节：罐 / 袋的密封与便携",
        "储存方法：开封后怎么存",
        "售后保障与收口：发货、退换、客服承诺，推动下单",
    ],
    "daily": [
        "首屏海报：品牌 + 一句核心利益，3 秒内让人想继续往下看",
        "痛点共鸣：喝茶的具体困扰（如要洗茶、苦涩、麻烦），用用户口吻",
        "产品亮相：一眼看清是什么茶",
        "原料与产地：原料、产地（只写有据的）",
        "核心卖点 1：最直观的外观特征（如莓茶的白霜），干茶特写",
        "冲泡方法：①②③ 用量、水温、时间",
        "茶汤展示：汤色与口感",
        "叶底特写：泡开后的叶片",
        "对比：与普通做法对比（不点名竞品，只写可证明的事实）",
        "制作工艺：杀青、揉捻、干燥等步骤（只写有据的）",
        "饮用场景：办公室、居家、待客",
        "品质证据：检测报告 / 许可证（只用用户提供的编号，否则【占位】）",
        "规格参数：净含量、配料、保质期、贮存条件、生产许可证、执行标准",
        "储存方法：开封后怎么存",
        "售后保障与收口：发货、退换、客服承诺，推动下单",
    ],
    "premium": [
        "首屏海报：品牌 + 一句有温度的核心表达，意境大片",
        "品牌故事：品牌由来、传承与理念（只写用户提供的事实）",
        "原料与产地：山林、云雾、采摘的真实环境（只写有据的）",
        "制作工艺：杀青、揉捻、干燥等步骤（只写有据的）",
        "产品亮相：一罐好茶的样子",
        "核心卖点 1：最直观的外观特征（如莓茶的白霜），干茶特写",
        "茶汤展示：汤色与口感",
        "叶底特写：泡开后的叶片",
        "冲泡仪式：①②③ 用量、水温、器具",
        "仪式感场景：安静的饮茶时刻",
        "送礼场景：礼盒开箱与送礼场合",
        "品质证据：检测报告 / 许可证（只用用户提供的编号，否则【占位】）",
        "规格参数：净含量、配料、保质期、贮存条件、生产许可证、执行标准",
        "储存方法：开封后怎么存",
        "承诺收口：品质与售后保障、邀请收藏 / 关注（不编造服务承诺）",
    ],
}

PLAYBOOK = {
    "label": "代用茶（莓茶 / 藤茶 / 花草茶）",
    "standard": True,
    "keywords": r"代用茶|莓茶|藤茶|显齿蛇葡萄|花草茶|花茶|苦荞茶|大麦茶|菊花茶|玫瑰花茶|袋泡茶|调味茶",
    "focus": "原料与产地（有据）、外观特征（如白霜）、冲泡是否方便、汤色与口感、配料是否单一、规格与储存",
    "required": [
        ("冲泡方式", r"冲泡|泡|水温|℃|开水", "写清一次用量、水温和冲泡时间"),
        ("原料 / 产地", r"原料|产地|产自|采自|山区|种植", "原料名称和产地（只写有依据的）"),
    ],
    "video": "让人「看得见、想喝」：干茶外观（白霜等）、开水冲下去的变化、汤色、叶底的特写和声音；"
             "口播讲产地、工艺、口感和冲泡方式，不讲任何保健功效",
    "ecom": "主图突出外观特征和冲泡场景；详情页讲清原料产地、冲泡方法、汤色叶底、规格配料和储存，证据屏用实拍",
    "visual": "natural tea still life, soft daylight, ceramic or glass teaware, wood or linen surface, "
              "steam over a cup, true-to-life leaf color",
    "detail_overrides": {},
    "compliance": [
        r"降压茶|降糖茶|降脂茶|减肥茶|排毒茶|养生茶|保健茶|养肝茶|护肝茶",
        # GB 7718-2025：不得使用「不添加」「不使用」及其同义语声称（「不使用时请密封」这类说明不算）
        r"不添加|无添加|零添加|不使用(?:任何)?(?:防腐剂|添加剂|香精|色素|农药|化肥|糖精)",
        r"代替药物|替代药物|可以停药|喝出健康|调理身体|调理肠胃|调节血糖|调节血压|平衡血糖",
    ],
    "compliance_hint": "代用茶是普通食品：不得宣称保健或疾病相关功效；不得用「不添加」「不使用」类声称；"
                       "黄酮、二氢杨梅素等含量只有检测报告支撑时才能写",
    "label_basis": "GB 7718-2025《预包装食品标签通则》（2027-03-16 起实施，此前可提前执行；"
                   "过渡期内 GB 7718-2011 仍有效）",
    "label_items": [
        ("食品名称", r"代用茶|莓茶|藤茶|花茶|[\u4e00-\u9fff]+茶", "反映真实属性的名称（如「代用茶」）"),
        ("配料表", r"配料", "配料表（单一配料也要标）"),
        ("净含量 / 规格", r"净含量|\d+\s*(?:g|克|千克|kg)", "净含量（含多件时写规格，如 5g×20 袋）"),
        ("生产者", r"生产者|生产商|制造商|公司|厂", "生产者名称、地址和联系方式"),
        ("日期", r"生产日期|保质期|到期日", "生产日期和保质期（新标准要求标保质期到期日）"),
        ("贮存条件", r"贮存|储存|保存|阴凉|干燥处", "贮存条件"),
        ("食品生产许可证", r"SC\s*\d{6,}|生产许可证", "食品生产许可证编号（SC 开头）"),
        ("产品标准代号", r"执行标准|产品标准|标准号|GB/?T?\s*\d+|DB\s*\d+|Q/", "产品标准代号（执行标准）"),
    ],
    "photo_checklist": PHOTOS,
    # 每条只查一件事：同一条里混几种说法时，产品信息里命中任何一种都会被当成「有依据」
    "evidence": [
        (r"黄酮|二氢杨梅素|多酚", "黄酮 / 二氢杨梅素等成分要有检测报告，含量数据也一样"),
        (r"\d+(?:\.\d+)?\s*%", "百分比数据要有检测报告或其它来源"),
        (r"有机", "「有机」要有有机产品认证"),
        (r"野生", "「野生」要有原料来源依据"),
        (r"特级|一级|二级|特等|[一二三]等品", "质量等级只有执行标准里规定了才能标（GB 7718-2025）"),
        (r"不含|无(?!需|须|论|法|限|数)", "「无 / 不含 XX」要求该成分含量为 0，且不能用于食品添加剂"),
        (r"国家地理标志|地理标志|原产地保护", "地理标志要有对应登记或授权"),
    ],
    "positioning_plans": PLANS,
}


CATEGORY = Category(
    key="tea",
    parent="food",
    playbook=PLAYBOOK,
    # 平台决定主图有哪几张，这里决定每张拍什么：茶的主图要让人「看得见、想喝」，画面照实拍
    main_overrides={
        "*": {
            "卖点图": "干茶特写：叶片外观特征（如白霜）看得清，照实拍画",
            "场景图": "冲泡场景：开水冲下去、茶汤透亮，生活化的饮茶时刻",
            "细节/证据图": "茶汤或叶底特写（照实拍画，不编造汤色和叶形）",
            "细节图": "茶汤或叶底特写（照实拍画，不编造汤色和叶形）",
            "第二卖点图": "口感：回甘等用户能感知的描述，配茶汤画面",
            "第三卖点图": "原料与产地（只写有据的），配原料或产地实拍",
            "痛点图": "喝茶的具体麻烦（要洗茶、苦涩、冲泡麻烦），不演疗效",
            "人群图": "办公室、居家、待客的饮茶时刻",
            "使用方法图": "冲泡方法：①②③ 用量、水温、时间",
            "价值图": "份量、包数，讲清能喝多久（价格用【券后价】占位，不编造活动）",
            "规格图": "净含量、包数、罐 / 袋",
            "保障图": "检测报告 / 许可证（编号用【占位】）",
            "信任图": "品质保障：检测报告 / 许可证（只用商家提供的编号，否则【占位】）",
        },
    },
    # 茶汤、叶底、干茶、冲泡、原料产地都是真实的产品内容：照片照着画，没有对口照片的屏先不出图
    role_rules=(
        (r"茶汤|汤色", dict(types=["in_use", "texture_detail"], product=True, photo=True, real=True,
                         shots=["macro", "tabletop", "side"])),
        (r"叶底", dict(types=["texture_detail", "in_use"], product=True, photo=True, real=True,
                     shots=["macro", "side"])),
        (r"白霜|干茶|外观特征", dict(types=["product_closeup", "texture_detail"], product=True, photo=True,
                               real=True, shots=["macro", "flatlay", "side"])),
        (r"冲泡|泡法", dict(types=["in_use", "product_closeup"], product=True, photo=True, real=True,
                         shots=["pov", "tabletop", "flatlay"])),
        (r"产地|原料|山林|云雾|采摘|鲜叶|茶园|种植", dict(types=["raw_material", "product_closeup"], product=False,
                                              photo=True, real=True, shots=["landscape", "macro", "lifestyle"])),
        # 工艺屏少有过程实拍：干茶照片锁住叶片的样子即可，不拦
        (r"工艺|杀青|揉捻", dict(types=["product_closeup", "raw_material"], product=True, photo=False,
                            shots=["flatlay", "macro", "tabletop"])),
        (r"包装细节|密封|便携", dict(types=["package_other", "package_front"], product=True, photo=False,
                               shots=["tabletop", "hero"])),
        (r"口感|回甘", dict(types=["in_use", "product_closeup"], product=True, photo=False,
                         shots=["tabletop", "pov"])),
    ),
    photo_descs={
        "product_closeup": "the real dry tea out of the package (its leaf shape, color and surface frost)",
        "in_use": "the real brewed tea (its liquor color and clarity)",
        "texture_detail": "the real infused leaves after brewing",
        "raw_material": "the real tea plants or growing area",
    },
    image_rules=(
        "If tea leaves, brewed tea or tea plants appear, they must look exactly like those in the reference photos "
        "(leaf shape, size, color, liquor color and clarity); if no reference photo shows them, keep them out of "
        "frame or out of focus instead of inventing them.",
        "Keep teaware and props simple and everyday; no medicinal herbs, pills, capsules or health-supplement imagery.",
    ),
    icon_rules=((r"杀青|揉捻", "flame"), (r"茶|嫩芽|芽叶", "leaf"), (r"回甘|甘甜|汤色", "cup")),
    step_pattern=r"冲泡",
    scene_example="a home office desk at 3 pm, overcast window light, a laptop and a glass mug of amber tea",
    action_example="低头吹热气、单手拿杯、往杯里投一小撮茶",
    art_example=("soft morning window light from the left; pale oak tabletop and warm off-white linen; "
                 "recurring props: a clear glass mug, a small bamboo tray; 50mm lens, shallow depth of field, "
                 "gentle film grain"),
    note_example="「玻璃杯里的琥珀色茶汤」「三袋装礼盒」",
)
