"""Web 界面：主题能加载、卡片 HTML 会转义；主图 / 详情页卡片的文案框在策划后可编辑"""

import html

import gradio as gr
import pytest

import ui_theme as ui


def test_theme_and_css_load():
    assert isinstance(ui.THEME, gr.themes.Base)
    assert ".asset-card" in ui.CSS and ".nav-tabs" in ui.CSS
    assert "fonts.googleapis" not in ui.CSS            # 只用本机字体，离线也能打开


def test_card_head_escapes_llm_text():
    evil = '<img src=x onerror="alert(1)">'
    out = ui.card_head("M1", evil, [ui.chip(evil, "warn")], warnings=[evil], notes=[evil])
    assert "<img" not in out
    assert html.escape(evil) in out
    assert 'class="chip chip-warn"' in out


def test_header_html_numbers_steps():
    out = ui.header_html("标题", "副标题", ["产品信息", "带货视频"])
    assert "<b>01</b>产品信息" in out and "<b>02</b>带货视频" in out


@pytest.fixture
def app():
    return pytest.importorskip("app_gradio")


@pytest.mark.parametrize("kind", ["main", "detail"])
def test_ecom_card_text_fields_are_interactive(app, monkeypatch, kind):
    """回归：gr.render 里有默认值的 Textbox 前端会当只读显示，策划后主标题/副标题/卖点改不了"""
    item = {"id": "M1" if kind == "main" else "D1", "role": "卖点图", "headline": "安稳过渡", "subline": "鸡肝风味",
            "badges": ["植物配伍", "无谷"], "title": "安稳过渡", "body": "正文", "bullets": ["要点一"],
            "scene_prompt_en": "a cat"}
    with gr.Blocks() as blocks:
        # 卡片的按钮事件要用到页面上的这几个组件，换成本 Blocks 里的替身
        monkeypatch.setattr(app, "ecom_text_mode", gr.Radio(choices=["overlay"], value="overlay"))
        monkeypatch.setattr(app, "status_text", gr.Textbox())
        monkeypatch.setattr(app, "ecom_state", gr.State({}))
        app.render_ecom_card(kind, item)
    fields = [b for b in blocks.blocks.values() if isinstance(b, gr.Textbox) and b.label and b.visible]
    assert len(fields) >= 4, "卡片里应有可见的文案输入框"
    assert all(b.interactive is True for b in fields), [(b.label, b.interactive) for b in fields]
