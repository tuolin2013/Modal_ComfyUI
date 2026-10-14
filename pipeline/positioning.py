"""
产品定位：同一个产品在不同渠道的打法不同（拼多多拼性价比，微信 / 视频号私域讲品牌和故事）

定位是产品的属性，平台只给默认值：用户在「📦 产品信息」选「跟随平台」时按平台默认，也可以手动指定。
每个定位决定：
  - detail_order：详情页 15 屏的顺序。元素是通用屏序号（1-15，内容先经类目手册替换），
    或字符串（这个定位专属的屏，替换掉没列出的通用屏）
  - tone / video：图文和剧本的语气、节奏
  - visual：画面方向（英文），由程序拼进每一张非白底图的提示词
  - price：价格展示策略。show = 点击图、价值图用【券后价】占位做钩子；subtle = 只在需要时出现；
           hide = 主图不放价格（程序清空，不靠 LLM 自觉）
本文件只放数据和纯函数。
"""

FOLLOW_PLATFORM = "auto"

POSITIONINGS: dict[str, dict] = {
    "value": {
        "label": "性价比",
        "summary": "价格与份量先行，信息直给，画面明亮饱满",
        "tone": "直接、实在、算得清账：先讲份量、规格、到手价（【券后价】占位）和省心，再讲品质；"
                "短句、数字化（数字只用用户给的），不玩虚的；不编造价格、销量和活动",
        "video": "前 2 秒给出「划算」的理由（份量、规格、到手价占位），节奏快，口播像实在的推荐，不铺垫情绪",
        "visual": "bright high-key lighting, saturated but natural colors, generous product quantity in frame, "
                  "clean and direct composition",
        "price": "show",
        # 去掉「产品亮相」，第 2 屏换成价值量感；对比、用法提前
        "detail_order": [1, "价值量感：份量、包数、到手价（价格用【券后价】占位），讲清为什么划算（不编造价格和活动）",
                         4, 2, 5, 7, 12, 6, 13, 9, 10, 8, 11, 14, 15],
    },
    "daily": {
        "label": "品质日常",
        "summary": "通用打法：痛点 → 卖点 → 证据 → 场景 → 细节 → 规格售后",
        "tone": "可信、清楚、有生活感：讲用户能感知的利益，配证据和细节",
        "video": "按剧本模板正常展开，钩子 + 卖点 + 证据 + 行动",
        "visual": "",
        "price": "subtle",
        "detail_order": list(range(1, 16)),
    },
    "premium": {
        "label": "高端礼赠",
        "summary": "品牌、产地和故事先行，留白多，适合私域和送礼",
        "tone": "克制、有温度、讲来历：多写产地、工艺、传承和使用时的情绪，少用促销词和感叹号；"
                "不做比价，不出现「便宜」「划算」「抢」；故事只写用户提供的事实",
        "video": "节奏放缓，用产地、工艺、人物的真实细节建立信任和情绪；像朋友分享或纪录片，不像硬广；"
                 "结尾邀请了解 / 收藏，不催单",
        "visual": "restrained premium look, generous negative space, soft natural light, muted refined palette, "
                  "documentary texture, nothing cluttered",
        "price": "hide",
        # 去掉痛点和对比（高端不靠比较）；品牌故事、原料工艺提前；加仪式感 / 送礼屏，收口换成承诺
        "detail_order": [1, "品牌故事：品牌由来、传承与理念（只写用户提供的事实）", 8, 3, 4, 13, 5, 9, 10,
                         "仪式感场景：精致的饮用 / 使用时刻或送礼场合，礼盒开箱", 6, 12, 11, 14,
                         "承诺收口：品质与售后保障、邀请收藏 / 关注（不编造服务承诺）"],
    },
}
DEFAULT_POSITIONING = "daily"

# 渠道默认定位（电商平台 + 视频平台）；没列出的平台用 DEFAULT_POSITIONING
PLATFORM_DEFAULTS = {
    "拼多多": "value",
    "微信小店": "premium",
    "视频号（竖版 9:16）": "premium",
}


def choices() -> list[tuple[str, str]]:
    return [("跟随平台（拼多多 = 性价比，微信 / 视频号 = 高端礼赠，其余 = 品质日常）", FOLLOW_PLATFORM)] + \
        [(f"{p['label']}：{p['summary']}", k) for k, p in POSITIONINGS.items()]


def resolve(choice: str | None, platform: str = "") -> str:
    """用户选的定位；「跟随平台」或无效值时按平台默认"""
    if choice in POSITIONINGS:
        return choice
    return PLATFORM_DEFAULTS.get(platform, DEFAULT_POSITIONING)


def get(key: str | None) -> dict:
    return POSITIONINGS.get(key or "", POSITIONINGS[DEFAULT_POSITIONING])


def label(key: str | None) -> str:
    return get(key)["label"]


def detail_roles(category_roles: list[str], key: str | None, plans: dict | None = None) -> list[str]:
    """
    详情页屏序。category_roles：通用 15 屏经类目手册替换后的内容（cp.detail_roles）。
    plans：类目标准包为各定位给的完整屏序（{定位: [15 个屏作用]}），有就直接用，类目比通用定位更专门。
    """
    if plans and isinstance(plans.get(key), list) and plans[key]:
        return list(plans[key])
    out = []
    for slot in get(key)["detail_order"]:
        if isinstance(slot, int) and 1 <= slot <= len(category_roles):
            out.append(category_roles[slot - 1])
        elif isinstance(slot, str):
            out.append(slot)
    return out


def prompt_block(key: str | None, purpose: str) -> str:
    """注入 LLM 的定位要点。purpose：ecom（主图详情页）/ video（剧本）"""
    p = get(key)
    lines = [f"【产品定位：{p['label']}】{p['summary']}",
             f"{'剧本' if purpose == 'video' else '文案'}要求：{p['video'] if purpose == 'video' else p['tone']}"]
    if purpose == "ecom":
        lines.append({"show": "价格：点击图和价值图用 price_label + price 做钩子，price 一律写【券后价】占位。",
                      "subtle": "价格：只在确实需要促销钩子的图上用【券后价】占位，其余不放价格。",
                      "hide": "价格：主图和详情页都不放价格、不写促销词（系统也会清掉主图价格）。"}[p["price"]])
    if p["visual"]:
        lines.append(f"画面方向（系统会拼进每张图的提示词，scene_prompt_en 不要和它矛盾）：{p['visual']}")
    return "\n".join(lines)


def apply_price_policy(mains: list[dict], key: str | None) -> list[dict]:
    """hide 策略的定位：程序清掉主图上的价格（LLM 偶尔还是会写）"""
    if get(key)["price"] == "hide":
        for m in mains:
            m["price"], m["price_label"] = "", ""
    return mains
