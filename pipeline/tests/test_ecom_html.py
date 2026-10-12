"""主图 / 详情页 HTML 模板排版：文案清洗、模板转义、Chromium 渲染与 Pillow 兜底"""

import asyncio
import sys
import warnings

import pytest
from PIL import Image, ImageChops

import ecom_images as ecom
import html_render


def _base(size, color=(240, 236, 228)) -> Image.Image:
    return Image.new("RGBA", size, (*color, 255))


def _data(kind: str, item: dict, img: Image.Image) -> dict:
    return ecom._template_data(kind, item, img, (200, 168, 130))


def _chromium_ok() -> bool:
    img = _base((200, 200))
    try:
        html_render.render("main.html", _data("main", {"headline": "测试"}, img), img)
        return True
    except html_render.RendererUnavailable:
        return False


needs_chromium = pytest.mark.skipif("not _chromium_ok()", reason="Chromium 不可用（python -m playwright install chromium）")


# ── 文案清洗 ──────────────────────────────────────────────────────────────────
def test_clean_text_strips_invisible_and_limits_length():
    assert ecom.clean_text("  安稳\u200b过渡\n\t好 \u202e", 10) == "安稳 过渡 好"
    assert ecom.clean_text("一二三四五六", 4) == "一二三四"
    assert ecom.clean_text(None, 5) == ""
    assert ecom.clean_text({"x": 1}, 5) == ""


@pytest.mark.parametrize("raw, expected", [("¥19.9", "19.9"), ("￥ 29", "29"), ("39元", "39"),
                                           ("【券后价】", "【券后价】"), ("", "")])
def test_clean_price(raw, expected):
    assert ecom.clean_price(raw) == expected


def test_normalize_specs_accepts_dicts_strings_and_text():
    assert ecom.normalize_specs([{"name": "规格", "value": "0.4克/片×30片"}, "适用：犬、猫", "保质期:24个月",
                                 "整行无名称", {"name": "空值", "value": ""}, 42]) == [
        {"name": "规格", "value": "0.4克/片×30片"}, {"name": "适用", "value": "犬、猫"},
        {"name": "保质期", "value": "24个月"}, {"name": "", "value": "整行无名称"},
        {"name": "", "value": "空值"}, {"name": "", "value": "42"},
    ]
    assert ecom.normalize_specs("规格：30片\n\n产地：【产地】") == [
        {"name": "规格", "value": "30片"}, {"name": "产地", "value": "【产地】"}]
    assert len(ecom.normalize_specs([f"第{i}项：值" for i in range(10)])) == 6
    assert ecom.normalize_specs(None) == []
    assert ecom.specs_text([{"name": "规格", "value": "30片"}, {"name": "", "value": "备注"}]) == "规格：30片\n备注"


@pytest.mark.parametrize("value, expected", [("#d94f2b", "#D94F2B"), ("D94F2B", "#D94F2B"),
                                             ("#FFFFFF", "#C8A882"), ("#000000", "#C8A882"),
                                             ("red", "#C8A882"), (None, "#C8A882")])
def test_theme_color_falls_back_to_brand(value, expected):
    assert ecom._theme_color(value, "#C8A882") == expected


def test_normalize_plan_reads_template_fields():
    raw = {"main_images": [
        {"tag": "新品", "headline": "发情期陪它安稳过渡", "subline": "鸡肝风味", "badges": "植物配伍、猫狗通用",
         "price_label": "券后价", "price": "¥【券后价】", "text_position": "bottom"},
        {"headline": "白底也写了字", "price": "9.9", "tag": "热卖"},
    ], "detail_sections": [{"tag": "规格参数", "title": "规格", "specs": ["规格：30片", {"name": "适用", "value": "猫"}]}]}
    plan = ecom._normalize_plan(raw, ["点击图", "白底图：纯白背景"], "1:1", set())
    m1, m2 = plan["main_images"]
    assert (m1["tag"], m1["price_label"], m1["price"]) == ("新品", "券后价", "【券后价】")
    assert m1["badges"] == ["植物配伍", "猫狗通用"] and m1["text_position"] == "bottom"
    assert (m2["headline"], m2["price"], m2["tag"]) == ("", "", "")   # 白底图不写字
    d1 = plan["detail_sections"][0]
    assert d1["tag"] == "规格参数"
    assert d1["specs"] == [{"name": "规格", "value": "30片"}, {"name": "适用", "value": "猫"}]
    assert plan["detail_sections"][1]["specs"] == []


def test_item_texts_covers_new_fields_for_compliance():
    item = {"tag": "第一", "headline": "", "price_label": "券后价", "price": "【券后价】",
            "specs": [{"name": "认证", "value": "医生推荐"}]}
    issues = ecom.item_issues(item)
    assert "禁用词「第一」" in issues
    assert "风险宣称「医生推荐」" in issues
    assert "未填写占位【券后价】" in issues


# ── 模板 ──────────────────────────────────────────────────────────────────────
def _page(kind: str, item: dict, size) -> str:
    img = _base(size)
    return html_render.render_html(f"{kind}.html", {**_data(kind, item, img), "width": size[0],
                                                    "height": size[1], "base_src": "base.png"})


def test_template_escapes_llm_text():
    evil = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
    page = _page("main", {"headline": evil, "badges": [evil], "tag": evil, "price": evil}, (1440, 1440))
    assert "<script>alert" not in page and "<img src=x" not in page
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in page


def test_detail_template_marks_steps_and_specs():
    item = {"title": "四步喂起来", "bullets": ["① 确认用量", "普通要点"], "specs": [{"name": "规格", "value": "30片"}]}
    page = _page("detail", item, (1440, 1920))
    assert '<span class="num">①</span>确认用量' in page
    assert "<th>规格</th><td>30片</td>" in page


def test_price_symbol_only_for_numbers():
    assert '<span class="price-cur">¥</span><span class="price-num">19.9</span>' in \
        _page("main", {"price": "19.9"}, (1440, 1440))
    page = _page("main", {"price": "【券后价】"}, (1440, 1440))
    assert "price-cur" not in page.split("<body>")[1] and '<span class="price-num">【券后价】</span>' in page


def test_palette_picks_tone_from_text_area():
    light, _ = ecom._palette(_base((100, 100), (245, 245, 245)), (0, 0, 100, 100), (200, 168, 130))
    dark, colors = ecom._palette(_base((100, 100), (20, 20, 20)), (0, 0, 100, 100), (200, 168, 130))
    assert (light, dark) == ("light", "dark")
    assert colors["head"] == "#FFFFFF"


# ── 渲染（需要 Chromium）──────────────────────────────────────────────────────
@needs_chromium
def test_render_main_draws_text_in_bottom_zone():
    img = _base((1440, 1440))
    item = {"headline": "发情期陪它安稳过渡", "subline": "鸡肝风味片", "badges": ["植物配伍", "猫狗通用"],
            "price_label": "券后价", "price": "19.9", "tag": "新品", "text_position": "bottom"}
    out, warnings = html_render.render("main.html", _data("main", item, img), img)
    assert out.size == (1440, 1440) and out.mode == "RGB"
    assert warnings == []
    diff = ImageChops.difference(out, img.convert("RGB")).convert("L").point(lambda v: 255 if v > 8 else 0)
    assert diff.crop((0, 960, 1440, 1440)).getbbox() is not None      # 底部有文字
    assert diff.crop((0, 0, 200, 120)).getbbox() is not None          # 左上角角标
    assert diff.crop((300, 200, 1140, 600)).getbbox() is None         # 中间产品区不动


@needs_chromium
def test_render_detail_with_specs_table():
    img = _base((1440, 1920))
    item = {"tag": "规格参数", "title": "规格参数", "body": "密封置于凉暗干燥处保存。",
            "specs": [{"name": "规格", "value": "0.4克/片×30片"}, {"name": "适用", "value": "犬、猫"},
                      {"name": "保质期", "value": "24个月"}]}
    out, warnings = html_render.render("detail.html", _data("detail", item, img), img)
    assert out.size == (1440, 1920)
    assert warnings == []


@needs_chromium
def test_render_shrinks_long_text_and_reports_overflow():
    img = _base((1440, 1440))
    item = {"headline": "这是一条明显超出长度限制的主标题" * 3, "text_position": "top"}
    _, warnings = html_render.render("main.html", _data("main", item, img), img)
    assert any("主标题超过 2 行" in w for w in warnings)


@needs_chromium
def test_render_blocks_network_requests(monkeypatch):
    img = _base((200, 200))
    seen = []
    original = html_render._allow_local_only

    async def spy(route):
        seen.append(route.request.url)
        await original(route)

    monkeypatch.setattr(html_render, "_allow_local_only", spy)
    data = _data("main", {"headline": "测试"}, img)
    data["font_bold"] = "https://example.invalid/font.ttf"     # 模拟模板里出现外链
    _, warnings = html_render.render("main.html", data, img)
    assert any(u.startswith("https://example.invalid") for u in seen)
    assert "字体文件加载失败，已用系统字体" in warnings


@needs_chromium
@pytest.mark.skipif(sys.platform != "win32", reason="只有 Windows 的 Selector 事件循环不能开子进程")
def test_render_survives_selector_policy(monkeypatch):
    """app_gradio 会 import modal，modal 把全局策略设成 WindowsSelectorEventLoopPolicy，Chromium 仍要能启动"""
    worker = html_render._BrowserWorker()          # 新 worker，确保在 Selector 策略下重新启动浏览器
    monkeypatch.setattr(html_render, "_worker", worker)
    with warnings.catch_warnings():                # 3.14 起事件循环策略 API 已弃用
        warnings.simplefilter("ignore", DeprecationWarning)
        old = asyncio.get_event_loop_policy()
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        img = _base((200, 200))
        out, _ = html_render.render("main.html", _data("main", {"headline": "测试"}, img), img)
        assert out.size == (200, 200)
    finally:
        worker.close()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            asyncio.set_event_loop_policy(old)


# ── compose_item：HTML 排版与 Pillow 兜底 ─────────────────────────────────────
@pytest.fixture
def plan_item(tmp_path, monkeypatch):
    base = tmp_path / "M1_base.png"
    Image.new("RGB", (1024, 1024), (240, 236, 228)).save(base)
    monkeypatch.setattr(ecom, "FINAL_DIR", tmp_path / "final")
    item = {"id": "M1", "seq": 1, "ratio": "1:1", "headline": "安稳过渡", "subline": "鸡肝风味",
            "badges": ["植物配伍"], "price": "19.9", "price_label": "券后价", "text_position": "top",
            "base_path": str(base), "base_mode": "overlay"}
    return {"meta": {"brand_color": "#C8A882", "theme_color": "#B5651D"}}, item


@needs_chromium
def test_compose_item_uses_html(plan_item):
    plan, item = plan_item
    out = ecom.compose_item(plan, "main", item)
    assert item["renderer"] == "html"
    assert Image.open(out).size == (1440, 1440)


def test_compose_item_falls_back_to_pil(plan_item, monkeypatch):
    plan, item = plan_item

    def unavailable(*_a, **_k):
        raise html_render.RendererUnavailable("Chromium 无法启动（测试）")

    monkeypatch.setattr(html_render, "render", unavailable)
    out = ecom.compose_item(plan, "main", item)
    assert item["renderer"] == "pil"
    assert item["layout_warnings"] == ["已退回简版排版：Chromium 无法启动（测试）"]
    assert Image.open(out).size == (1440, 1440)


def test_compose_item_pil_mode_skips_browser(plan_item, monkeypatch):
    plan, item = plan_item
    monkeypatch.setattr(ecom, "RENDERER", "pil")
    monkeypatch.setattr(html_render, "render", lambda *a, **k: pytest.fail("pil 模式不应调用浏览器"))
    ecom.compose_item(plan, "main", item)
    assert item["renderer"] == "pil" and item["layout_warnings"] == []


def test_ai_mode_only_resizes(plan_item, monkeypatch):
    plan, item = plan_item
    item["base_mode"] = "ai"
    monkeypatch.setattr(html_render, "render", lambda *a, **k: pytest.fail("AI 写字的图不应再叠字"))
    ecom.compose_item(plan, "main", item)
    assert item["renderer"] == ""
