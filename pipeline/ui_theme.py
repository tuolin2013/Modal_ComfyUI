"""Web 界面的主题、样式和卡片小部件（AI SaaS / 仪表盘风格，配合 app_gradio.py）

- THEME：Gradio 主题变量（配色、圆角、字体、按钮、输入框），浅色和深色两套都定义
- CSS：ui_theme.css 里的版式细节（页头、分段导航、素材卡片、状态标签、运行日志栏）
- FORCE_DARK_JS：默认深色；地址栏带 ?__theme=light 时按用户选择
- 只用本机字体，不拉 Google Fonts，离线和国内网络都能正常打开
- 卡片里的文字来自 LLM / 用户输入，拼 HTML 前一律转义
"""

import html
from pathlib import Path

import gradio as gr

CSS = Path(__file__).with_name("ui_theme.css").read_text(encoding="utf-8")

FORCE_DARK_JS = """
() => {
  if (!new URLSearchParams(window.location.search).has('__theme')) {
    document.body.classList.add('dark');
  }
}
"""

FONT_SANS = ["Inter", "PingFang SC", "HarmonyOS Sans SC", "Microsoft YaHei UI", "Microsoft YaHei",
             "system-ui", "sans-serif"]
FONT_MONO = ["JetBrains Mono", "Cascadia Code", "Consolas", "ui-monospace", "monospace"]

_GRAD = "linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%)"
_GRAD_HOVER = "linear-gradient(135deg, #7477f6 0%, #9b70f8 100%)"
# 页面顶部两团很淡的靛蓝 / 紫色光晕
_BODY_DARK = ("radial-gradient(1100px 520px at 0% -8%, rgba(99,102,241,.16), transparent 62%), "
              "radial-gradient(900px 480px at 100% -6%, rgba(168,85,247,.10), transparent 60%), #08090b")
_BODY_LIGHT = ("radial-gradient(1100px 520px at 0% -8%, rgba(99,102,241,.08), transparent 62%), "
               "radial-gradient(900px 480px at 100% -6%, rgba(168,85,247,.06), transparent 60%), #fafafa")

_SURFACES = dict(
    body_background_fill=_BODY_LIGHT, body_background_fill_dark=_BODY_DARK,
    body_text_color="#18181b", body_text_color_dark="#e7e7ea",
    body_text_color_subdued="#71717a", body_text_color_subdued_dark="#8a8d98",
    background_fill_primary="#ffffff", background_fill_primary_dark="#0d0e11",
    background_fill_secondary="#f4f4f5", background_fill_secondary_dark="#111216",
    border_color_primary="#e4e4e7", border_color_primary_dark="#23252b",
    border_color_accent="#6366f1", border_color_accent_dark="#6366f1",
    color_accent="#6366f1", color_accent_soft="rgba(99,102,241,.10)", color_accent_soft_dark="rgba(99,102,241,.16)",
    link_text_color="#4f46e5", link_text_color_dark="#a5b4fc",
    link_text_color_hover="#4338ca", link_text_color_hover_dark="#c7d2fe",
    code_background_fill="#f4f4f5", code_background_fill_dark="#17181d",
    panel_background_fill="#fafafa", panel_background_fill_dark="#0b0c0f",
    panel_border_color="#e4e4e7", panel_border_color_dark="#1f2126",
    block_background_fill="#ffffff", block_background_fill_dark="#0e0f12",
    block_border_color="#e7e7ea", block_border_color_dark="#1f2126", block_border_width="1px",
    block_radius="14px", block_padding="14px",
    block_shadow="0 1px 2px rgba(16,24,40,.04)", block_shadow_dark="none",
    block_label_background_fill="transparent", block_label_background_fill_dark="transparent",
    block_label_border_width="0px", block_label_text_color="#71717a", block_label_text_color_dark="#8a8d98",
    block_title_text_color="#3f3f46", block_title_text_color_dark="#a1a3ad",
    block_title_text_size="13px", block_title_text_weight="500",
    block_info_text_color="#71717a", block_info_text_color_dark="#7c7f8a",
    accordion_text_color="#18181b", accordion_text_color_dark="#e7e7ea",
    loader_color="#6366f1", loader_color_dark="#818cf8", slider_color="#6366f1", slider_color_dark="#818cf8",
    shadow_drop="0 1px 2px rgba(0,0,0,.06)", shadow_drop_lg="0 8px 24px -12px rgba(0,0,0,.25)",
    shadow_spread="3px", shadow_spread_dark="3px",
)

_INPUTS = dict(
    input_background_fill="#ffffff", input_background_fill_dark="#0a0b0d",
    input_background_fill_focus="#ffffff", input_background_fill_focus_dark="#0b0c10",
    input_border_color="#e4e4e7", input_border_color_dark="#25272d",
    input_border_color_hover="#d4d4d8", input_border_color_hover_dark="#34363d",
    input_border_color_focus="#6366f1", input_border_color_focus_dark="#6366f1",
    input_shadow="none", input_shadow_dark="none",
    input_shadow_focus="0 0 0 3px rgba(99,102,241,.16)", input_shadow_focus_dark="0 0 0 3px rgba(99,102,241,.26)",
    input_placeholder_color="#a1a1aa", input_placeholder_color_dark="#55585f",
    input_radius="10px",
    checkbox_background_color_selected="#6366f1", checkbox_background_color_selected_dark="#6366f1",
    checkbox_border_color_selected="#6366f1", checkbox_border_color_selected_dark="#6366f1",
    checkbox_border_color_dark="#34363d", checkbox_background_color_dark="#0a0b0d",
    checkbox_label_background_fill="#ffffff", checkbox_label_background_fill_dark="#101115",
    checkbox_label_background_fill_hover="#f4f4f5", checkbox_label_background_fill_hover_dark="#16171c",
    checkbox_label_background_fill_selected="rgba(99,102,241,.08)",
    checkbox_label_background_fill_selected_dark="rgba(99,102,241,.16)",
    checkbox_label_border_color="#e4e4e7", checkbox_label_border_color_dark="#25272d",
    checkbox_label_border_color_selected="#6366f1", checkbox_label_border_color_selected_dark="#6366f1",
    checkbox_label_text_color_selected="#3730a3", checkbox_label_text_color_selected_dark="#e0e7ff",
    checkbox_label_border_width="1px",
)


_BUTTONS = dict(
    button_large_radius="10px", button_medium_radius="10px", button_small_radius="8px",
    button_large_text_weight="600", button_medium_text_weight="600", button_small_text_weight="500",
    button_transition="all .15s ease", button_transform_hover="translateY(-1px)",
    button_primary_background_fill=_GRAD, button_primary_background_fill_dark=_GRAD,
    button_primary_background_fill_hover=_GRAD_HOVER, button_primary_background_fill_hover_dark=_GRAD_HOVER,
    button_primary_border_color="transparent", button_primary_border_color_dark="transparent",
    button_primary_text_color="#ffffff", button_primary_text_color_dark="#ffffff",
    button_primary_shadow="inset 0 1px 0 rgba(255,255,255,.18), 0 6px 20px -8px rgba(99,102,241,.55)",
    button_primary_shadow_hover="inset 0 1px 0 rgba(255,255,255,.22), 0 10px 28px -8px rgba(139,92,246,.65)",
    button_primary_shadow_dark="inset 0 1px 0 rgba(255,255,255,.18), 0 6px 22px -8px rgba(99,102,241,.7)",
    button_primary_shadow_hover_dark="inset 0 1px 0 rgba(255,255,255,.22), 0 12px 32px -8px rgba(139,92,246,.8)",
    button_secondary_background_fill="#ffffff", button_secondary_background_fill_dark="#15161a",
    button_secondary_background_fill_hover="#f4f4f5", button_secondary_background_fill_hover_dark="#1c1d22",
    button_secondary_border_color="#e4e4e7", button_secondary_border_color_dark="#2a2c33",
    button_secondary_text_color="#18181b", button_secondary_text_color_dark="#e4e4e7",
    button_cancel_background_fill="transparent", button_cancel_background_fill_dark="transparent",
    button_cancel_background_fill_hover="rgba(239,68,68,.08)",
    button_cancel_background_fill_hover_dark="rgba(239,68,68,.12)",
    button_cancel_border_color="rgba(220,38,38,.35)", button_cancel_border_color_dark="rgba(248,113,113,.35)",
    button_cancel_text_color="#dc2626", button_cancel_text_color_dark="#f87171",
    button_cancel_text_color_hover="#b91c1c", button_cancel_text_color_hover_dark="#fca5a5",
)

THEME = gr.themes.Base(
    primary_hue="indigo", secondary_hue="violet", neutral_hue="zinc",
    radius_size="lg", font=FONT_SANS, font_mono=FONT_MONO,
).set(**_SURFACES, **_INPUTS, **_BUTTONS)

_LOGO = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" '
         'stroke-linejoin="round" aria-hidden="true"><path d="M12 3l1.9 4.6 4.6 1.9-4.6 1.9L12 16l-1.9-4.6'
         '-4.6-1.9 4.6-1.9z"/><path d="M18.5 15l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z"/></svg>')


def header_html(title: str, subtitle: str, steps: list[str]) -> str:
    """页头：Logo + 标题 + 流程节点（产品信息 → 视频 → 图文）"""
    nodes = '<span class="arrow" aria-hidden="true"></span>'.join(
        f'<span class="node"><b>{i:02d}</b>{html.escape(s)}</span>' for i, s in enumerate(steps, 1))
    return (f'<div class="app-header"><div class="brand"><div class="logo">{_LOGO}</div><div>'
            f'<h1 class="title">{html.escape(title)}</h1><div class="sub">{html.escape(subtitle)}</div>'
            f'</div></div><nav class="flow" aria-label="流程">{nodes}</nav></div>')


def console_head_html() -> str:
    return ('<div class="console-head"><div class="name"><span class="dot" aria-hidden="true"></span>运行日志</div>'
            '<span class="meta">任务排队串行执行</span></div>')


def chip(text: str, tone: str = "muted") -> str:
    """状态小标签。tone：muted / ok / warn / accent"""
    return f'<span class="chip chip-{tone}">{html.escape(str(text))}</span>'


def card_head(badge: str, title: str, chips: list[str] = (), warnings: list[str] = (),
              notes: list[str] = ()) -> str:
    """卡片标题行：编号徽标 + 标题 + 标签（chips 为 chip() 的结果）+ 警告 / 提示条"""
    out = [f'<div class="card-head"><div class="card-title"><span class="card-id">{html.escape(str(badge))}</span>'
           f'<span class="card-name">{html.escape(str(title))}</span></div>']
    if chips:
        out.append(f'<div class="card-chips">{"".join(chips)}</div>')
    out += [f'<div class="note note-warn">{html.escape(w)}</div>' for w in warnings if w]
    out += [f'<div class="note note-info">{html.escape(n)}</div>' for n in notes if n]
    return "".join(out) + "</div>"
