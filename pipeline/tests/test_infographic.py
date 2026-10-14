"""详情页信息图版式（流程 / 图标网格 / 对比表）与场景图代入感（生活纪实 vs 棚拍）"""

import pytest
from PIL import Image

import ecom_images as ecom
import html_render
import layer2_visual_assets as l2
import quality_check as qc

ACCENT = (107, 142, 78)


def _base(size=(720, 960)) -> Image.Image:
    return Image.new("RGB", size, (236, 230, 220))


def _chromium_ok() -> bool:
    img = _base((200, 200))
    try:
        html_render.render("main.html", ecom._template_data("main", {"headline": "测试"}, img, ACCENT), img)
        return True
    except html_render.RendererUnavailable:
        return False


needs_chromium = pytest.mark.skipif("not _chromium_ok()", reason="Chromium 不可用（python -m playwright install chromium）")


# ── 对比表 / 图标 / 版式的数据清洗 ────────────────────────────────────────────
def test_parse_compare_text_and_roundtrip():
    cmp = ecom.parse_compare("普通茶叶｜本品\n洗茶|需要先洗|不用洗\n\n口感｜偏苦｜回甘\n只有两格｜右")
    assert cmp["headers"] == ["普通茶叶", "本品"]
    assert cmp["rows"] == [{"name": "洗茶", "left": "需要先洗", "right": "不用洗"},
                           {"name": "口感", "left": "偏苦", "right": "回甘"},
                           {"name": "", "left": "只有两格", "right": "右"}]
    assert ecom.parse_compare(ecom.compare_text(cmp)) == cmp


def test_normalize_compare_defaults_and_limits():
    cmp = ecom.normalize_compare({"rows": [[f"项{i}", "左", "右"] for i in range(8)]})
    assert cmp["headers"] == ["普通做法", "本品"]                 # 没给列名时用中性列名，不点名竞品
    assert len(cmp["rows"]) == ecom.MAX_COMPARE_ROWS
    assert ecom.normalize_compare({"rows": [["项", "", ""]]}) == {"headers": [], "rows": []}
    assert ecom.normalize_compare(None) == {"headers": [], "rows": []}
    assert ecom.compare_text({}) == ""


def test_icons_whitelist_and_guess():
    assert ecom.fill_icons(["leaf", "<script>", None], ["原料", "95℃ 开水", "杀青工艺"]) == ["leaf", "thermo", "flame"]
    assert ecom.guess_icon("海拔 800 米山地") == "mountain"
    assert ecom.guess_icon("说不清的东西") == "check"
    assert all(name in ecom.ICONS for _, name in ecom.ICON_KEYWORDS)


@pytest.mark.parametrize("layout, item, expected", [
    ("steps", {"bullets": ["一", "二"]}, "steps"),
    ("steps", {"bullets": ["只有一步"]}, "standard"),           # 1 步撑不起流程图
    ("grid", {"bullets": []}, "standard"),
    ("compare", {"compare": {"rows": []}}, "standard"),          # 选了对比表却没有行
    ("compare", {"compare": {"rows": [{"name": "a", "left": "b", "right": "c"}]}}, "compare"),
    ("bogus", {"role": "冲泡方法（①②③）", "bullets": ["一", "二"]}, "steps"),   # 没给就按屏的角色猜
    (None, {"role": "对比：与普通做法对比", "compare": {"rows": [{"name": "", "left": "a", "right": "b"}]}}, "compare"),
    (None, {"role": "品牌故事"}, "standard"),
])
def test_normalize_layout(layout, item, expected):
    assert ecom.normalize_layout(layout, item) == expected


def test_normalize_plan_reads_layout_fields():
    raw = {"detail_sections": [
        {"layout": "steps", "title": "冲泡", "bullets": ["取茶", "冲水", "出汤"], "icons": ["leaf", "nope"]},
        {"layout": "compare", "title": "对比", "compare": {"headers": ["普通", "本品"], "rows": [["洗茶", "要", "不用"]]}},
        {"layout": "grid", "title": "只有一条", "bullets": ["一条"]},
    ]}
    plan = ecom._normalize_plan(raw, ["卖点图"], "1:1", set(), ["冲泡方法", "对比", "卖点"])
    d1, d2, d3 = plan["detail_sections"]
    assert d1["layout"] == "steps" and d1["icons"] == ["leaf", "drop", "check"]
    assert d2["layout"] == "compare" and d2["compare"]["rows"][0]["right"] == "不用"
    assert d3["layout"] == "standard"


def test_detail_share_reserves_more_room_for_infographics():
    assert ecom.detail_share({"layout": "grid"}) == 0.55
    assert ecom.detail_share({"specs": [{"name": "a", "value": "b"}]}) == 0.50
    assert ecom.detail_share({}) == 0.35
    _, _, zone_max = ecom._text_zone("detail", {"layout": "compare"}, 1000, 1000)
    assert zone_max == 600


def test_compare_text_is_compliance_checked():
    it = {"title": "对比", "layout": "compare", "compare": ecom.parse_compare("普通｜本品\n喝完｜一般｜降血糖")}
    assert "降血糖" in qc.item_texts(it)
    assert qc.check_compliance(qc.item_texts(it), "food", placeholders=False)


def test_plan_prompt_lists_icons():
    assert "{icon_names}" not in ecom.PLAN_SYSTEM
    assert "mountain" in ecom.PLAN_SYSTEM and "compare" in ecom.PLAN_SYSTEM


# ── 模板 ──────────────────────────────────────────────────────────────────────
def _html(item: dict) -> str:
    img = _base()
    return html_render.render_html("detail.html", {**ecom._template_data("detail", item, img, ACCENT),
                                                   "width": 720, "height": 960, "base_src": "base.png"})


def _body(html: str) -> str:
    return html.split("<body>")[1].split("<script>")[0]


def test_template_renders_each_layout():
    steps = _body(_html({"layout": "steps", "title": "冲泡", "bullets": ["取茶", "冲水", "出汤"]}))
    assert steps.count('class="flow-step"') == 3 and steps.count("flow-arrow") == 2
    assert "<path" in steps                                       # 图标 SVG 原样嵌入
    grid = _body(_html({"layout": "grid", "title": "卖点", "bullets": ["云雾山地", "白霜", "回甘"]}))
    assert 'class="grid cols-3"' in grid and grid.count('class="cell"') == 3
    cmp = _body(_html({"layout": "compare", "title": "对比",
                       "compare": ecom.parse_compare("普通｜本品\n洗茶｜要｜不用")}))
    assert 'class="compare"' in cmp and "不用" in cmp
    std = _body(_html({"layout": "standard", "title": "标准", "bullets": ["要点"]}))
    assert 'class="bullets' in std and "flow" not in std and "<svg" not in std


def test_template_escapes_infographic_text():
    evil = '<img src=x onerror="alert(1)">'
    for item in ({"layout": "steps", "title": "t", "bullets": [evil, "二"]},
                 {"layout": "grid", "title": "t", "bullets": [evil, "二"]},
                 {"layout": "compare", "title": "t", "compare": ecom.parse_compare(f"{evil}｜本品\n项｜{evil}｜右")}):
        out = _html(item)
        assert "<img src=x" not in out
        assert "&lt;img" in out


@needs_chromium
@pytest.mark.parametrize("item", [
    {"layout": "steps", "title": "三步泡好一杯", "bullets": ["取 3-5 克茶叶", "95℃ 以上开水", "不用洗茶直接喝"]},
    {"layout": "grid", "title": "山里来的一杯茶", "bullets": ["云雾山地采摘", "叶片自带白霜", "开水直接冲泡", "回味甘甜"]},
    {"layout": "compare", "title": "泡起来更省事",
     "compare": ecom.parse_compare("普通茶叶｜本品\n洗茶｜需要先洗｜不用洗\n第一泡｜倒掉｜直接喝")},
])
def test_infographics_fit_without_warnings(item):
    img = _base((1440, 1920))
    final, warnings = html_render.render("detail.html", ecom._template_data("detail", item, img, ACCENT), img)
    assert final.size == (1440, 1920)
    assert warnings == []


def test_pil_fallback_keeps_compare_rows(monkeypatch, tmp_path):
    seen = []
    real_wrap = ecom._wrap
    monkeypatch.setattr(ecom, "_wrap", lambda text, font, w: seen.append(text) or real_wrap(text, font, w))
    item = {"layout": "compare", "title": "对比", "compare": ecom.parse_compare("普通｜本品\n洗茶｜要｜不用洗")}
    try:
        ecom._pil_compose_detail(item, ACCENT, tmp_path / "x.jpg", _base().convert("RGBA"))
    except FileNotFoundError:
        pytest.skip("没有中文字体")
    assert any("洗茶：不用洗" in s for s in seen)


# ── 场景代入感 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("shot, expected", [
    ({"emotion_beat": "卖点展示", "characters": []}, l2.STYLE_PREFIX),
    ({"emotion_beat": "信任背书"}, l2.STYLE_PREFIX),
    ({"emotion_beat": "卖点展示", "characters": ["C1"]}, l2.LIFESTYLE_PREFIX),   # 有人出镜就是生活场景
    ({"emotion_beat": "开场钩子", "characters": []}, l2.LIFESTYLE_PREFIX),
    ({"emotion_beat": "使用场景"}, l2.LIFESTYLE_PREFIX),
])
def test_style_prefix_by_shot(shot, expected):
    assert l2.style_prefix(shot) == expected


def test_lifestyle_prefix_avoids_studio_look():
    assert "studio" not in l2.LIFESTYLE_PREFIX.replace("no studio", "")
    assert "clean background" not in l2.LIFESTYLE_PREFIX
    assert "medical" not in "".join(l2.EMOTION_BEAT_STYLE.values()).lower()