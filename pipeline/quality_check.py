"""
生成质量的硬校验（规则判断，不靠模型自觉）+ 广告法用词初筛

- check_brief：输入体检，生成前提示还缺哪些信息
- check_storyboard：剧本/分镜（镜头数、节拍、口播字数、卖点覆盖、重复、合规）
- check_ecom_plan：主图/详情页（字数、卖点覆盖、重复、合规）
- check_compliance：通用极限词/功效词 + 类目专属风险词 + 未填写的【占位】
- check_visual：画面描述 / 生图提示词里暗示医疗场景或疗效的词（中英文）
- accept_revision：模型自检改稿是否采纳（按严重程度计分，不许引入新的 ❌）
- revision_request：模型自检时发给 LLM 的修改要求（评分标准 + 程序发现的问题）

全部是关键词和规则判断，只能拦住明显问题，不能替代人工和法务审核。
本模块只依赖 category_playbook，不导入其它层，避免循环导入。
"""

import re

import category_playbook as cp

# ── 广告法用词检查（关键词初筛）─────────────────────────────────────────────────
BANNED_PATTERNS = [
    r"最[佳好高低优强大新先美全牛]", r"第一(?![泡次步口眼杯天时])", r"唯一", r"首[个家选款]", r"顶级", r"顶尖", r"极致", r"极品",
    r"国家级", r"世界级", r"全球[首领]", r"全网[最第]", r"销量冠军", r"100\s*%", r"百分之百", r"绝对",
    r"永久", r"万能", r"无敌", r"史无前例", r"独家", r"王牌", r"至尊", r"NO\.?\s*1",
    r"[\u4e00-\u9fff]{1,4}之王",
]
# 医疗 / 疾病类宣称：任何非药品都不该出现（含换个说法的暗示，如「血管清道夫」「祛病」）
RISKY_PATTERNS = [
    r"治疗|治愈|根治|疗效|药到病除|治百病|防治|祛病|疗伤", r"消炎|抗炎|杀菌|抑菌|抗菌|杀灭细菌|抗病毒",
    r"无副作用|零副作用|无毒|毒副作用", r"抑制肿瘤|抗肿瘤|抗癌|防癌",
    r"药理|药用|药物|药食同源|处方|送服|辅助吸收|辅助治疗",
    r"咽炎|支气管炎|呼吸道感染|心脑血管疾病|高血压|糖尿病|痛风|血管清道夫",
    r"神草|神药|神茶|神仙草|长寿|延年益寿",
    r"祛斑|美白|防脱|生发|抗敏", r"减肥|瘦身|燃脂", r"医生推荐|医院|临床证明|医生认证|专家推荐",
    r"零添加|纯天然", r"立即见效|立刻见效|\d+\s*[天秒分钟]+见效",
]
PLACEHOLDER = re.compile(r"【[^】]*】")

# 画面暗示医疗 / 疗效：查生图提示词和画面描述（中英文）。产品本身是片剂、胶囊时不算，所以不查 pill / tablet
VISUAL_RISK_PATTERNS = [
    r"\b(?:hospitals?|clinics?|doctors?|nurses?|physicians?|surgeons?|pharmac(?:y|ies|ist)|patients?)\b",
    r"\b(?:stethoscopes?|syringes?|injections?|IV drips?|prescriptions?|x-rays?|blood tests?|hospital beds?|lab coats?)\b",
    r"\b(?:clutch(?:es|ing)?|grabb?(?:ing|s)?|hold(?:s|ing)?)\s+(?:his|her|their|the)\s+(?:chest|throat|stomach|belly|head)\b",
    r"\b(?:cough(?:s|ing)?|sore throat|in pain|feverish|fever|sick(?:ness)?|nause(?:a|ous)|ill(?:ness)?|symptoms?)\b",
    r"\b(?:cur(?:e|es|ed|ing)|heal(?:s|ed|ing)?|recover(?:s|ed|y|ing)?|treatment)\b",
    r"\b(?:medical|medicines?|medications?)\b",
    r"医院|诊所|医生|护士|白大褂|听诊器|输液|打针|针管|病床|药瓶|药片|处方|捂着?(?:胸口|喉咙|肚子|头)|咳嗽|生病|发烧|病痛|痊愈",
]


def check_visual(text: str) -> list[str]:
    """画面描述里暗示医疗场景或疗效的词，如 ['画面风险「hospital」']（关键词初筛）"""
    found = []
    for p in VISUAL_RISK_PATTERNS:
        found += [m if isinstance(m, str) else m[0] for m in re.findall(p, str(text or ""), flags=re.I)]
    return [f"画面风险「{w}」" for w in dict.fromkeys(w.lower() for w in found)]


def check_compliance(text: str, playbook: str | None = None, placeholders: bool = True) -> list[str]:
    """
    返回问题列表，如 ['禁用词「第一」', '风险宣称「医生认证」', '类目风险「预防」', '未填写占位【券后价】']
    playbook：类目手册 key，传了就同时查类目专属风险词；placeholders=False 时不报【占位】
    """
    text = str(text or "")
    issues, seen = [], set()

    def add(kind, words):
        for w in words:
            if w not in seen:
                seen.add(w)
                issues.append(f"{kind}「{w}」")

    for p in BANNED_PATTERNS:
        add("禁用词", re.findall(p, text, flags=re.I))
    for p in RISKY_PATTERNS:
        add("风险宣称", re.findall(p, text))
    for p in cp.compliance_patterns(playbook):
        add("类目风险", re.findall(p, text))
    if placeholders:
        issues += [f"未填写占位{m}" for m in PLACEHOLDER.findall(text)]
    return list(dict.fromkeys(issues))


def item_texts(item: dict) -> str:
    """主图/详情页一张图上会出现的全部文字"""
    parts = [item.get(k, "") for k in ("tag", "headline", "subline", "title", "body", "price_label", "price")]
    parts += list(item.get("badges") or []) + list(item.get("bullets") or [])
    parts += [f"{s.get('name', '')} {s.get('value', '')}" for s in item.get("specs") or []]
    cmp = item.get("compare") or {}
    if cmp.get("rows"):
        parts += list(cmp.get("headers") or [])
        parts += [f"{r.get('name', '')} {r.get('left', '')} {r.get('right', '')}" for r in cmp["rows"]]
    return "\n".join(str(p).strip() for p in parts if str(p).strip())


def item_issues(item: dict, playbook: str | None = None) -> list[str]:
    return check_compliance(item_texts(item), playbook)


# ── 卖点覆盖：按双字片段的重合度粗判 ─────────────────────────────────────────
_NON_WORD = re.compile(r"[\W_]+")


def _core(text: str) -> str:
    """去掉括号里的补充说明和标点，只留卖点主干"""
    return _NON_WORD.sub("", re.sub(r"[（(][^）)]*[）)]", "", str(text or "")))


def covers(usp: str, haystack: str, threshold: float = 0.5) -> bool:
    """卖点主干的双字片段有 threshold 以上出现在 haystack 里，就算覆盖"""
    core = _core(usp)
    if len(core) < 2:
        return True
    grams = {core[i:i + 2] for i in range(len(core) - 1)}
    hay = _NON_WORD.sub("", str(haystack or ""))
    return sum(g in hay for g in grams) / len(grams) >= threshold


def uncovered_usps(usps: list[str], haystack: str) -> list[str]:
    return [u for u in usps if not covers(u, haystack)]


def _chars(text) -> int:
    return len(re.sub(r"\s", "", str(text or "")))


def _duplicates(values: list[tuple[str, str]]) -> list[str]:
    """[(id, 文案)] 里文案相同的组，返回 ['M1/M3「xxx」']"""
    groups: dict[str, list[str]] = {}
    for iid, text in values:
        key = _NON_WORD.sub("", str(text or ""))
        if key:
            groups.setdefault(key, []).append(iid)
    return [f"{'/'.join(ids)}「{key}」" for key, ids in groups.items() if len(ids) > 1]


# 问题分三级：❌ 必须处理 / ⚠️ 建议处理 / 💡 提示
ERR, WARN, INFO = "❌", "⚠️", "💡"


def format_report(title: str, issues: list[str]) -> str:
    if not issues:
        return f"\n✅ {title}：未发现问题"
    return f"\n🔎 {title}（{len(issues)} 项）：\n" + "\n".join(f"  {i}" for i in issues)


# ── 输入体检 ──────────────────────────────────────────────────────────────────
MIN_DESC_CHARS = 30


def check_brief(brief: dict, playbook: str) -> list[str]:
    """生成前的输入体检：信息越全，后面的卖点、剧本和图文越不需要靠模型猜"""
    issues = []
    if not str(brief.get("name") or "").strip():
        issues.append(f"{ERR} 没有填写商品名称")
    if _chars(brief.get("description")) < MIN_DESC_CHARS:
        issues.append(f"{WARN} 产品描述只有 {_chars(brief.get('description'))} 字，"
                      "建议补充成分/材质、规格、功能、用法等（模型缺信息时只能泛泛而谈）")
    for name, tip in cp.missing_required(brief, playbook):
        issues.append(f"{WARN} {cp.label(playbook)}类目建议写明「{name}」：{tip}")
    # 输入里的功效宣称会被模型当成素材照搬或换个说法暗示，生成前就提醒
    source = "\n".join(str(brief.get(k) or "") for k in ("description", "endorsements", "usp"))
    claims = [i for i in check_compliance(source, playbook, placeholders=False) if not i.startswith("禁用词")]
    if claims:
        shown = "、".join(re.sub(r"^.*?「|」$", "", c) for c in claims[:6])
        issues.append(f"{WARN} 产品描述 / 背书 / 卖点里有 {len(claims)} 处功效或医疗宣称（{shown}"
                      f"{'…' if len(claims) > 6 else ''}）。模型会参考这些内容写文案，建议删掉，"
                      "或改写成可证明的事实（产地、原料、工艺、口感、冲泡方式）")
    if not str(brief.get("reviews") or "").strip():
        issues.append(f"{INFO} 可粘贴买家评价、问大家或竞品卖点，痛点和用语会更贴近真实用户")
    if not str(brief.get("audience") or brief.get("persona") or "").strip():
        issues.append(f"{INFO} 没有目标人群，卖点分析会按类目常见人群推断")
    if playbook == cp.GENERAL:
        issues.append(f"{INFO} 没有识别出类目，按通用规则生成；可在「类目手册」里手动选择")
    return issues


# ── 剧本 / 分镜 ───────────────────────────────────────────────────────────────
def check_storyboard(sb: dict, usps: list[str], beats: list[str] | None = None, vo_limit: int | None = None,
                     duration_range: tuple[float, float] | None = None, playbook: str | None = None) -> list[str]:
    shots = sb.get("shots") or []
    if not shots:
        return [f"{ERR} 没有生成任何镜头"]
    issues = []

    if beats:
        if len(shots) != len(beats):
            issues.append(f"{ERR} 模板要求 {len(beats)} 个镜头，实际 {len(shots)} 个")
        for shot, beat in zip(shots, beats):
            if shot.get("emotion_beat") != beat:
                issues.append(f"{WARN} 镜头 {shot.get('id')} 的节拍是「{shot.get('emotion_beat')}」，模板要求「{beat}」")

    for shot in shots:
        sid, vo = shot.get("id"), shot.get("voiceover")
        if vo_limit and _chars(vo) > vo_limit:
            issues.append(f"{WARN} 镜头 {sid} 口播 {_chars(vo)} 字，超过平台建议的 {vo_limit} 字")
        if duration_range and float(shot.get("duration_sec") or 0) > duration_range[1] + 0.05:
            issues.append(f"{WARN} 镜头 {sid} 因口播较长被拉到 {shot.get('duration_sec')} 秒"
                          f"（平台建议 {duration_range[0]:g}–{duration_range[1]:g} 秒）")
    if all(not _chars(s.get("voiceover")) for s in shots):
        issues.append(f"{WARN} 所有镜头都没有口播")
    for dup in _duplicates([(f"镜头{s.get('id')}", s.get("voiceover")) for s in shots]):
        issues.append(f"{WARN} 口播重复：{dup}")

    script = sb.get("script") or {}
    hay = "\n".join([str(script.get("full_text") or "")] +
                    [f"{s.get('voiceover') or ''} {s.get('visual_intent') or ''}" for s in shots])
    for u in uncovered_usps(usps, hay):
        issues.append(f"{WARN} 卖点没有在剧本里体现：{u}")

    # 口播会被直接念出来，算硬问题；剧本正文只给人看，算建议
    spoken = check_compliance("\n".join(str(s.get("voiceover") or "") for s in shots), playbook,
                              placeholders=False)
    issues += [f"{ERR} 口播{i}" for i in spoken]
    issues += [f"{WARN} 剧本正文{i}" for i in check_compliance(script.get("full_text"), playbook, placeholders=False)
               if i not in spoken]

    # 画面也不能暗示疗效（医院、捂胸口转舒展……）：查每镜的画面描述、生图提示词和场景设定
    for shot in shots:
        visual = check_visual(f"{shot.get('visual_intent') or ''}\n{shot.get('image_gen_prompt') or ''}")
        if visual:
            issues.append(f"{WARN} 镜头 {shot.get('id')} {'、'.join(visual)}，画面不要暗示医疗场景或疗效")
    for scene in script.get("scenes") or []:
        if isinstance(scene, dict) and (visual := check_visual(scene.get("description_en"))):
            issues.append(f"{WARN} 场景 {scene.get('id')} {'、'.join(visual)}，画面不要暗示医疗场景或疗效")
    return issues


# ── 自检改稿是否采纳：按严重程度计分，而不是数条数 ─────────────────────────────
SEVERITY_WEIGHT = {ERR: 5, WARN: 1}


def issue_score(issues: list[str]) -> int:
    """❌ 记 5 分、⚠️ 记 1 分、💡 不计分；分越低越好"""
    return sum(next((w for mark, w in SEVERITY_WEIGHT.items() if i.startswith(mark)), 0) for i in issues)


def accept_revision(old: list[str], new: list[str]) -> bool:
    """改稿不能引入原稿没有的 ❌（如新增违规口播），总分也不能变差"""
    introduced = {i for i in new if i.startswith(ERR)} - set(old)
    return not introduced and issue_score(new) <= issue_score(old)


# ── 主图 / 详情页 ─────────────────────────────────────────────────────────────
# 与 ecom_images.PLAN_SYSTEM 的字数要求一致
MAIN_LIMITS = (("headline", 10, "主标题"), ("subline", 16, "副标题"), ("tag", 4, "角标"))
DETAIL_LIMITS = (("title", 12, "标题"), ("body", 40, "正文"), ("tag", 4, "小标签"))
BADGE_LIMIT, BULLET_LIMIT = 6, 14


def check_ecom_plan(plan: dict, usps: list[str]) -> list[str]:
    """字数、缺标题、重复、卖点覆盖。广告法和【占位】由 item_issues 逐张检查，这里不重复报"""
    mains, details = plan.get("main_images") or [], plan.get("detail_sections") or []
    issues = []

    def check_item(it, limits, list_field, list_limit, list_label, title_field):
        for field, limit, label in limits:
            n = _chars(it.get(field))
            if n > limit:
                issues.append(f"{WARN} {it.get('id')} {label} {n} 字，建议 ≤ {limit} 字")
        for b in it.get(list_field) or []:
            if _chars(b) > list_limit:
                issues.append(f"{WARN} {it.get('id')} {list_label}「{b}」超过 {list_limit} 字")
        if not _chars(it.get(title_field)):
            issues.append(f"{WARN} {it.get('id')} 没有{limits[0][2]}")

    for it in mains:
        if not it.get("white_bg"):
            check_item(it, MAIN_LIMITS, "badges", BADGE_LIMIT, "卖点标签", "headline")
    for it in details:
        check_item(it, DETAIL_LIMITS, "bullets", BULLET_LIMIT, "要点", "title")

    for dup in _duplicates([(it.get("id"), it.get("headline")) for it in mains if not it.get("white_bg")]):
        issues.append(f"{WARN} 主图标题重复：{dup}")
    for dup in _duplicates([(it.get("id"), it.get("title")) for it in details]):
        issues.append(f"{WARN} 详情页标题重复：{dup}")

    hay = "\n".join(item_texts(it) for it in mains + details)
    for u in uncovered_usps(usps, hay):
        issues.append(f"{WARN} 卖点没有出现在任何一张图上：{u}")
    return issues


# ── 模型自检 ──────────────────────────────────────────────────────────────────
VIDEO_RUBRIC = """\
1. 开场 2 秒内有钩子：冲突、悬念或一个强利益点，观众不划走
2. 每条核心卖点至少呈现一次，并且尽量用画面证明，而不只是口播念出来
3. 口播口语化、短句，不超过字数上限，不同镜头不重复
4. 不编造参数、数据、认证和评价；不用极限词，不做医疗功效宣称，遵守类目合规
5. 符合目标平台的节奏、视频类型的规则和类目手册的要点
6. 结尾给出明确、可信的购买理由
7. 每个镜头都能被视频模型单独渲染：一镜一个连续画面，不要求画面里出现文字，不依赖上一镜的画面细节"""

ECOM_RUBRIC = """\
1. 每张主图只讲一个利益点，主图之间不重复；首图在搜索结果里一眼看懂「是什么、为什么买」
2. 详情页按屏序讲完整的说服链条，屏与屏之间不重复同一句话
3. 遵守字数限制（主标题 ≤ 10 字、副标题 ≤ 16 字、详情页标题 ≤ 12 字、正文 ≤ 40 字等）
4. 每条核心卖点至少出现在一张图上
5. 不编造价格、数据、认证和评价，需要的地方用【占位】；不用极限词，不做医疗功效宣称，遵守类目合规
6. 符合电商平台特点和类目手册的要点"""


def revision_request(rubric: str, issues: list[str]) -> str:
    """模型自检的指令：按评分标准自查，结合程序发现的问题，输出修改后的完整 JSON"""
    found = "\n".join(f"- {i}" for i in issues) or "- （程序未发现硬性问题，请按评分标准自查）"
    return f"""\
请以资深电商操盘手的标准审核你上面输出的 JSON，并直接给出修改后的版本。

【评分标准】
{rubric}

【程序检查发现的问题（必须修正）】
{found}

要求：
1. 逐条对照评分标准，优先修改最影响转化的问题；没问题的部分保持不变。
2. 输出修改后的完整 JSON，字段结构、数组长度和顺序与原来完全一致，不要增删镜头或图片。
3. 只输出 JSON，不加解释。"""
