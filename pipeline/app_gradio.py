import os
import re
import json
import inspect
import random
import shutil
import subprocess
import traceback
from datetime import datetime
from pathlib import Path

import gradio as gr
import modal
from dotenv import load_dotenv

# ── 路径与环境 ────────────────────────────────────────────────────────────────
PIPELINE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PIPELINE_DIR.parent

# 各层内部使用 "./pipeline/output/..." 相对路径，统一切到项目根目录，避免从其他目录启动时路径错乱
os.chdir(PROJECT_ROOT)
load_dotenv(PIPELINE_DIR / ".env")

# 导入 Pipeline 层
from layer1_planning import (
    generate_storyboard, save_storyboard,
    STORY_TEMPLATES, DEFAULT_TEMPLATE, PLATFORMS, DEFAULT_PLATFORM, CUSTOM_TEMPLATE, MIN_BEATS, MAX_BEATS,
    parse_custom_beats,
    shot_references, duration_to_frames,
    platform_duration, voiceover_limit, template_conflict,
)
from layer2_visual_assets import generate_all_shots, quick_draft, generate_character_portrait
from layer3_h3_prompt import process_all_prompts
import category_playbook as cp
import asset_library as al
import positioning as pos
import ecom_images as ecom
import product_brief as brief_mod
import quality_check as qc
import ui_theme as ui

OUTPUT_DIR = PROJECT_ROOT / "pipeline" / "output"
STORYBOARD_PATH = str(OUTPUT_DIR / "storyboard.json")
ASSETS_DIR = str(OUTPUT_DIR / "assets" / "shots")
PRODUCT_REF_DIR = OUTPUT_DIR / "user_product_refs"
CHARACTER_DIR = OUTPUT_DIR / "assets" / "characters"
PROMPTS_DIR = OUTPUT_DIR / "prompts"
VIDEO_DIR = OUTPUT_DIR / "videos"
FINAL_VIDEO = VIDEO_DIR / "final_commercial.mp4"

# 模式 A 的 ComfyUI 地址，在 pipeline/.env 中配置；为空时界面不显示跳转按钮
COMFYUI_URL = os.environ.get("COMFYUI_URL", "").strip()
T2V_MODES = {"T2VA", "T2V"}
MODAL_APP = "minimax-h3-diffusers-service"  # 与 h3_diffusers_service.APP_NAME 一致

# ── Gradio 版本兼容 ───────────────────────────────────────────────────────────
# Gradio 6 移除了 show_copy_button（改为 buttons=["copy"]），theme / css / js 也从 Blocks 移到了 launch()
GRADIO_MAJOR = int(gr.__version__.split(".")[0])
COPY_BTN = {"buttons": ["copy"]} if GRADIO_MAJOR >= 6 else {"show_copy_button": True}
THEME = ui.THEME
_UI_KW = {"theme": ui.THEME, "css": ui.CSS, "js": ui.FORCE_DARK_JS}
BLOCKS_KW = {} if GRADIO_MAJOR >= 6 else _UI_KW
LAUNCH_KW = _UI_KW if GRADIO_MAJOR >= 6 else {}
# 运行日志放右侧固定侧栏（滚到哪里都看得到）；老版本 Gradio 没有右侧栏时退回页头下方一栏
HAS_RIGHT_SIDEBAR = hasattr(gr, "Sidebar") and "position" in inspect.signature(gr.Sidebar.__init__).parameters

# 帧数换算 duration_to_frames 在 layer1_planning（layer3 也要用，放这里会循环导入）


# ── 分镜读写 ──────────────────────────────────────────────────────────────────
def load_storyboard() -> dict:
    """读取完整 storyboard（shots + product_refs + meta）；不存在或损坏时返回空 dict"""
    if os.path.exists(STORYBOARD_PATH):
        try:
            with open(STORYBOARD_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def write_storyboard(sb: dict):
    with open(STORYBOARD_PATH, "w", encoding="utf-8") as f:
        json.dump(sb, f, ensure_ascii=False, indent=2)


# ── 工具函数 ──────────────────────────────────────────────────────────────────
def shot_id(shot) -> int:
    """LLM 可能返回字符串 id，统一转 int，便于 :02d 格式化与排序"""
    try:
        return int(shot.get("id"))
    except (TypeError, ValueError):
        return 0


def prompt_file_for(shot) -> Path:
    # 与 layer3 的命名保持一致
    return PROMPTS_DIR / f"shot_{shot_id(shot):02d}_h3_prompt.txt"


def read_h3_prompt(shot) -> str:
    """优先读取 layer3 生成的 H3 提示词，否则回退到 layer1 的 image_gen_prompt"""
    p = prompt_file_for(shot)
    if p.exists():
        return p.read_text(encoding="utf-8")
    return shot.get("image_gen_prompt", "")


def clear_dir(path: Path, pattern: str = "*"):
    """清理派生产物，防止旧商品的提示词/视频/参考图污染新一轮流水线"""
    if not path.exists():
        return
    for f in path.glob(pattern):
        if f.is_file():
            f.unlink()



def _ready(path) -> bool:
    return bool(path and os.path.exists(path))


def characters_of(sb: dict) -> list[dict]:
    return ((sb or {}).get("script") or {}).get("characters", [])


def missing_refs(sb: dict) -> list[str]:
    """剧本需要、但用户还没上传的产品图 id"""
    return [r["id"] for r in sb.get("product_refs", []) if not _ready(r.get("image_path"))]


def missing_portraits(sb: dict) -> list[str]:
    """还没有定妆照的角色 id（层 2 会自动生成，也可手动上传）"""
    return [c["id"] for c in characters_of(sb) if not _ready(c.get("image_path"))]


def video_aspect_ratio(sb: dict) -> str:
    """成片画幅：优先 meta.aspect_ratio，旧分镜没有时按 meta.platform 查平台表，再不行用默认平台"""
    meta = (sb or {}).get("meta") or {}
    plat = PLATFORMS.get(meta.get("platform")) or PLATFORMS[DEFAULT_PLATFORM]
    return str(meta.get("aspect_ratio") or plat["aspect_ratio"])


def delete_shot_videos(shot_ids):
    """参考图变了，用到它的镜头视频已过期"""
    for sid in shot_ids:
        try:
            (VIDEO_DIR / f"shot_{int(sid):02d}.mp4").unlink(missing_ok=True)
        except (TypeError, ValueError):
            pass


def shots_with_character(sb: dict, cid: str) -> list:
    return [s.get("id") for s in sb.get("shots", []) if cid in (s.get("characters") or [])]


def err(e: Exception) -> str:
    return f"❌ 发生错误: {e}\n\n详情:\n{traceback.format_exc()}"


def template_info(name: str) -> str:
    if name == CUSTOM_TEMPLATE:
        return (f"在下方逐行填写 {MIN_BEATS}–{MAX_BEATS} 个节拍，每行「节拍名 | A-Roll 或 B-Roll | 这一镜的任务」。"
                "A-Roll 是人物镜头，B-Roll 是产品特写。")
    tpl = STORY_TEMPLATES.get(name)
    if not tpl:
        return ""
    beats = " → ".join(f"{b[0]}({b[1]})" for b in tpl["beats"])
    return f"**{tpl['type']}** · {tpl['desc']}\n\n**{len(tpl['beats'])} 个镜头：** {beats}"


def template_choices() -> list[tuple[str, str]]:
    """剧本模板下拉框：按视频类型显示，最后是自定义节拍"""
    return [(f"{t['type']} · {name}", name) for name, t in STORY_TEMPLATES.items()] + \
           [("✍️ 自定义节拍（覆盖不到的场景）", CUSTOM_TEMPLATE)]


def platform_info(name: str, template: str = "") -> str:
    p = PLATFORMS.get(name)
    if not p:
        return ""
    lo, hi = platform_duration(name)
    text = (f"**画幅 {p['aspect_ratio']}**（基底图 {p['image_size']}）· 单镜 {lo:g}–{hi:g} 秒 · "
            f"口播 ≤ {voiceover_limit(name)} 字/镜 · 推荐模板「{p['templates'][0]}」\n\n{p['guide']}")
    conflict = template_conflict(name, template)
    return text + (f"\n\n⚠️ {conflict}" if conflict else "")


def on_platform_change(platform: str, template: str):
    """换平台时：当前模板不适合新平台就切到推荐模板（适合的话保留用户的选择）"""
    p = PLATFORMS.get(platform)
    if p and template not in p["templates"]:
        template = p["templates"][0]
    return platform_info(platform, template), gr.update(value=template)


def playbook_info(brief: dict) -> str:
    key = cp.resolve(brief)
    how = "手动选择" if brief.get("playbook") not in (None, "", cp.AUTO) and key == brief.get("playbook") else "自动识别"
    if key == cp.GENERAL:
        return (f"类目手册：**通用**（{how}；未匹配到类目）。可手动选择，"
                "或在下方「新建自定义类目手册」让 AI 起草一份")
    extra = f" · 继承「{cp.label(cp.parent_of(key))}」的合规底线" if cp.is_custom(key) else ""
    return f"类目手册：**{cp.label(key)}**（{how}{extra}）· 买家关注：{cp.get(key)['focus']}"


def run_draft_playbook(*brief_values):
    """AI 起草自定义类目手册，填进编辑框（不保存）"""
    brief = collect_brief(brief_values, save=False)
    if not (brief.get("name") or brief.get("category")):
        return "⚠️ 请先填写商品名称和类目", gr.skip()
    try:
        draft = brief_mod.draft_playbook(brief, ecom.DETAIL_ROLES)
    except Exception as e:
        return err(e), gr.skip()
    text = json.dumps({**draft, "required": [list(r) for r in draft["required"]],
                       "detail_overrides": {str(k): v for k, v in draft["detail_overrides"].items()}},
                      ensure_ascii=False, indent=2)
    return (f"🤖 已起草「{draft['label']}」手册（parent：{cp.label(draft['parent'])}）。"
            "请核对买家关注点和风险词，改好后点「💾 保存并选用」。"), text


def run_save_playbook(text):
    try:
        key = cp.save_custom(json.loads(text or "{}"))
    except json.JSONDecodeError as e:
        return f"⚠️ JSON 格式不对：{e}", gr.skip()
    except ValueError as e:
        return f"⚠️ {e}", gr.skip()
    return (f"💾 已保存自定义类目手册「{cp.label(key)}」并选用。",
            gr.update(choices=cp.choices(), value=key))


def run_delete_playbook(key):
    if not cp.is_custom(key):
        return "⚠️ 只能删除自定义手册（下拉框里带 ⭐ 的）", gr.skip(), gr.skip()
    name = cp.label(key)
    cp.delete_custom(key)
    return f"🗑️ 已删除自定义类目手册「{name}」，改回自动识别。", \
        gr.update(choices=cp.choices(), value=cp.AUTO), ""


# ── 编剧技能 ──────────────────────────────────────────────────────────────────
# 非编剧类技能（由其他层在代码中固定使用），不出现在编剧下拉框里
NON_SCREENWRITING_SKILLS = {"h3-prompt-writing"}
# 注入 LLM 的技能文本总长度上限（字符），防止多选 + reference 撑爆上下文
SKILL_CHAR_BUDGET = 60_000
# 项目自带的技能（随仓库分发），默认选中；.agents/skills 下是用户自己放的技能
BUILTIN_SKILLS_DIR = PIPELINE_DIR / "skills"
DEFAULT_SKILLS = ("short-video-selling",)

SKILL_MAP = {}  # label -> 技能目录 Path
for skills_dir in (BUILTIN_SKILLS_DIR, PROJECT_ROOT / ".agents" / "skills"):
    if not skills_dir.is_dir():
        continue
    for d in sorted(skills_dir.iterdir()):
        md_path = d / "SKILL.md"
        if not (d.is_dir() and md_path.exists()) or d.name in NON_SCREENWRITING_SKILLS:
            continue
        if any(p.name == d.name for p in SKILL_MAP.values()):
            continue  # 同名技能以项目自带的为准
        title = d.name
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("# "):
                        title = line.strip("# \n")
                        break
        except OSError:
            pass
        SKILL_MAP[f"{d.name} | {title}"] = d
skill_choices = list(SKILL_MAP.keys())
default_skills = [label for label, d in SKILL_MAP.items() if d.name in DEFAULT_SKILLS]


def strip_frontmatter(text: str) -> str:
    """去掉 SKILL.md 开头的 YAML frontmatter（--- ... ---），只保留正文"""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip("\n")
    return text


def load_skill_content(labels, include_refs: bool) -> tuple[str, list[str]]:
    """
    读取所选技能（可多选）的 SKILL.md 正文，可选附带同目录 reference*.md。
    返回 (拼接后的文本, 因超出预算被截断/跳过的文件列表)。
    """
    if isinstance(labels, str):
        labels = [labels]
    parts, skipped, used = [], [], 0

    def add(title: str, text: str):
        nonlocal used
        remain = SKILL_CHAR_BUDGET - used
        if remain <= 0:
            skipped.append(title)
            return
        if len(text) > remain:
            text = text[:remain] + "\n...(已截断)"
            skipped.append(f"{title}（截断）")
        parts.append(f"## {title}\n{text}")
        used += len(text)

    # 先放所有 SKILL.md 主体，再放 reference，保证预算不够时优先保留核心方法论
    selected = [SKILL_MAP[l] for l in labels or [] if l in SKILL_MAP]
    for d in selected:
        try:
            add(f"技能：{d.name}", strip_frontmatter((d / "SKILL.md").read_text(encoding="utf-8")))
        except OSError:
            skipped.append(f"{d.name}/SKILL.md（读取失败）")
    if include_refs:
        for d in selected:
            for ref in sorted(d.glob("reference*.md")):
                try:
                    add(f"{d.name} 参考资料：{ref.name}", ref.read_text(encoding="utf-8"))
                except OSError:
                    skipped.append(f"{d.name}/{ref.name}（读取失败）")
    return "\n\n".join(parts), skipped


# ── 产品信息与卖点分析（视频和图文共用）──────────────────────────────────────────
BRIEF_FIELDS = brief_mod.INPUT_FIELDS + brief_mod.ANALYSIS_FIELDS


def collect_brief(values, save: bool = True) -> dict:
    """按 BRIEF_FIELDS 顺序把页面输入组装成 dict，并保存（刷新页面后恢复）"""
    brief = {k: (str(v).strip() if v is not None else "") for k, v in zip(BRIEF_FIELDS, values)}
    brief["playbook"] = brief.get("playbook") or cp.AUTO
    brief["positioning"] = brief.get("positioning") or pos.FOLLOW_PLATFORM
    if save:
        brief_mod.save_brief(brief)
    return brief


def brief_check_report(brief: dict) -> str:
    key = cp.resolve(brief)
    lib = al.load_library()
    label_text = al.label_context(lib)
    photo_types = {e.get("type") for e in lib}
    return f"\n📚 类目手册：{cp.label(key)}" + qc.format_report(
        "输入体检", qc.check_brief(brief, key, photo_types, label_text))


def run_brief_check(*values):
    """输入体检：不调用 LLM，只看信息够不够"""
    brief = collect_brief(values)
    return "🩺 输入体检完成。" + brief_check_report(brief)


def run_analyze(*values):
    """LLM 分析卖点；结果填回可编辑的分析框，用户可修改或再次分析"""
    try:
        brief = collect_brief(values)
        result, notes = brief_mod.analyze_product(brief)
        brief.update(result)
        brief_mod.save_brief(brief)
        msg = "✅ 卖点分析完成，可直接修改下方结果，或补充产品信息后重新分析。"
        if notes:
            msg += f"\n📝 建议补充/核实：{notes}"
        issues = qc.check_compliance("\n".join(result.values()), cp.resolve(brief), placeholders=False)
        if issues:
            msg += f"\n⚠️ 广告法 / 类目合规风险：{'、'.join(issues)}"
        msg += brief_check_report(brief)
        return [msg] + [result[k] for k in brief_mod.ANALYSIS_FIELDS]
    except Exception as e:
        return [err(e)] + [gr.skip()] * len(brief_mod.ANALYSIS_FIELDS)


# ── 层 1：策划 ────────────────────────────────────────────────────────────────
def concepts_summary(concepts: dict | None) -> str:
    """运行日志里列出 3 个创意方向，标出选中的那个"""
    items = (concepts or {}).get("concepts") or []
    if not items:
        return ""
    chosen = concepts.get("chosen", 0)
    lines = [f"\n💡 创意方向（选中第 {chosen + 1} 个{'：' + concepts['reason'] if concepts.get('reason') else ''}）："]
    for i, c in enumerate(items):
        mark = "👉" if i == chosen else "  "
        risk = f" ⚠️ {'、'.join(c['compliance'])}" if c.get("compliance") else ""
        lines.append(f"  {mark} {i + 1}. {c['angle']}（{c['score']} 分）钩子：{c['hook_line']}{risk}")
    return "\n".join(lines)


def run_layer1(story_template, platform, agent_skills, include_skill_refs, self_review, custom_beats,
               concept_first, *brief_values):
    brief = collect_brief(brief_values)
    product_name = brief["name"]
    usps = brief_mod.lines_of(brief["usp"])
    if not product_name or not usps:
        return "⚠️ 请填写商品名称，并点「分析卖点」或手动填写核心卖点", load_storyboard()
    if story_template == CUSTOM_TEMPLATE:
        try:
            parse_custom_beats(custom_beats)
        except ValueError as e:
            return f"⚠️ {e}", load_storyboard()

    skill_content, skill_skipped = load_skill_content(agent_skills, include_skill_refs)
    playbook = cp.resolve(brief)

    try:
        storyboard = generate_storyboard(
            product_name=product_name,
            usp=usps,
            brand_color=brief["brand_color"],
            story_template=story_template,
            custom_beats=custom_beats or "",
            target_platform=platform,
            agent_skill_content=skill_content,
            product_context=brief_mod.brief_context(brief),
            playbook=playbook,
            self_review=bool(self_review),
            concept_first=bool(concept_first),
            positioning=brief.get("positioning") or pos.FOLLOW_PLATFORM,
        )
        save_storyboard(storyboard, STORYBOARD_PATH)
    except Exception as e:
        return err(e), load_storyboard()

    # 新剧本的产品图需求、提示词、镜头视频都与旧剧本无关，全部作废
    # （layer3 会跳过已存在的提示词文件，不清掉会沿用旧商品的提示词）
    clear_dir(PRODUCT_REF_DIR)
    clear_dir(CHARACTER_DIR)
    clear_dir(PROMPTS_DIR, "*.txt")
    clear_dir(VIDEO_DIR, "*.mp4")

    refs = storyboard.get("product_refs", [])
    msg = f"✅ 分镜生成成功！共 {len(storyboard.get('shots', []))} 个镜头。"
    chars = characters_of(storyboard)
    if chars:
        msg += f"\n👤 {len(chars)} 个角色，定妆照将在层 2 自动生成（也可在「剧本」页上传真人照替换）。"
    if refs and ecom.get_product_images():
        msg += (f"\n📸 剧本需要 {len(refs)} 张产品镜头图，执行「2️⃣ 生成定妆照 + 基底图」时会以「📦 产品信息」页的实拍图为参考自动生成"
                "（也可在「② 素材准备」逐张点「🤖 现在生成」先看效果）。")
    elif refs:
        msg += f"\n📸 剧本需要 {len(refs)} 张产品镜头图，请先在「📦 产品信息」页上传产品实拍图，AI 以它为参考生成。"
    else:
        msg += "\n💡 本剧本没有真实产品入画的镜头，无需产品图。"
    # 硬校验（含口播的广告法 / 类目合规初筛）；自检开启时这是改过一轮之后的结果
    msg += f"\n📚 类目手册：{cp.label(playbook)} · 定位：{pos.label((storyboard.get('meta') or {}).get('positioning'))}"
    if (storyboard.get("meta") or {}).get("self_reviewed"):
        msg += " · 已按评分标准自检修改一轮"
    elif self_review:
        msg += " · 自检后问题没有减少，保留初稿"
    msg += concepts_summary((storyboard.get("meta") or {}).get("concepts"))
    if concept_first and not (storyboard.get("meta") or {}).get("concepts"):
        msg += "\n💡 创意方向这一步没有成功，已直接生成分镜"
    msg += qc.format_report("剧本检查", storyboard.get("quality_issues") or [])
    conflict = template_conflict(platform, story_template)
    if conflict:
        msg += f"\n⚠️ {conflict}"
    if skill_content:
        msg += f"\n📚 已注入编剧技能 {len(agent_skills or [])} 个，共 {len(skill_content)} 字符。"
    if skill_skipped:
        msg += f"\n⚠️ 超出技能注入上限 ({SKILL_CHAR_BUDGET} 字符)，以下内容被截断或跳过：\n" + "\n".join(skill_skipped)
    return msg, storyboard


# ── 产品图：按剧本需求逐张上传 ────────────────────────────────────────────────
def save_ref_image(ref_id: str, file_path):
    """保存某个 RefN 的产品图，并立即回填到使用它的 Ref2VA 镜头"""
    sb = load_storyboard()
    ref = next((r for r in sb.get("product_refs", []) if r.get("id") == ref_id), None)
    if ref is None:
        return f"⚠️ 当前剧本不需要 {ref_id}，请重新生成分镜", sb

    # 删除该 Ref 的旧图（扩展名可能不同）
    PRODUCT_REF_DIR.mkdir(parents=True, exist_ok=True)
    clear_dir(PRODUCT_REF_DIR, f"{ref_id}.*")

    new_path = ""
    if file_path:
        dest = PRODUCT_REF_DIR / f"{ref_id}{Path(file_path).suffix.lower() or '.png'}"
        shutil.copy(file_path, dest)
        new_path = str(dest)

    ref["image_path"] = new_path
    ref["image_source"] = "upload" if new_path else ""
    write_storyboard(sb)
    delete_shot_videos(ref.get("used_in", []))

    left = missing_refs(sb)
    action = "已上传" if new_path else "已移除"
    tail = f"还缺 {', '.join(left)}" if left else "所需产品图已全部就绪 ✅"
    return f"📸 {ref_id} {action}。{tail}", sb


def _ref_gen_prompt(ref: dict, product_name: str, labels: list[str]) -> str:
    """剧本要求的产品图 → 以实拍图为参考的生图提示词"""
    photos = "；".join(f"参考图 {i}：{l or '产品实拍'}" for i, l in enumerate(labels, 1))
    return (
        f"Create a product reference photo of \"{product_name}\" for a video shot.\n"
        f"Required content / angle (Chinese): {ref.get('desc', '')}\n"
        f"Reference photos of the REAL product ({photos}). Reference 1 is the primary view.\n"
        "The product must be exactly the one in the reference photos: keep the shape, proportions, "
        "packaging text, logo, colors, materials and details identical. Do not redesign, do not invent or "
        "change any text. If the required angle is not visible in the references, infer it conservatively "
        "and keep all visible details consistent.\n"
        "Photorealistic commercial product photography, sharp focus, product fully visible and dominant, "
        "clean simple background unless the description asks for a scene, no people unless required, "
        "no added text, no watermark."
    )


def generate_ref_image(ref_id: str, sb: dict | None = None, save: bool = True) -> str:
    """以「📦 产品信息」页的实拍图为参考，按剧本 desc 生成 RefN 产品图；返回保存路径"""
    sb = sb if sb is not None else load_storyboard()
    ref = next((r for r in sb.get("product_refs", []) if r.get("id") == ref_id), None)
    if ref is None:
        raise ValueError(f"当前剧本不需要 {ref_id}，请重新生成分镜")
    slots = ecom.product_slots()
    if not slots:
        raise ValueError("请先在「📦 产品信息」页上传产品实拍图")
    name = (sb.get("product") or {}).get("name") or brief_mod.load_brief().get("name", "")
    prompt = _ref_gen_prompt(ref, name, [label for _, _, label in slots])

    PRODUCT_REF_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PRODUCT_REF_DIR / f"{ref_id}_gen.png"
    ecom.edit_image(prompt, [p for _, p, _ in slots], tmp, "1:1")
    # 成功后才替换旧图（失败时保留原来的）
    clear_dir(PRODUCT_REF_DIR, f"{ref_id}.*")
    dest = PRODUCT_REF_DIR / f"{ref_id}.png"
    tmp.replace(dest)

    ref["image_path"] = str(dest)
    ref["image_source"] = "generated"
    if save:
        write_storyboard(sb)
    delete_shot_videos(ref.get("used_in", []))
    return str(dest)


def run_generate_ref(ref_id: str):
    try:
        generate_ref_image(ref_id)
        sb = load_storyboard()
        left = missing_refs(sb)
        tail = f"还缺 {', '.join(left)}" if left else "所需产品图已全部就绪 ✅"
        return f"🤖 {ref_id} 已按实拍图生成，请核对包装文字和 logo 是否与实物一致。{tail}", sb
    except Exception as e:
        return err(e), load_storyboard()


def generate_missing_refs() -> tuple[list[str], list[str]]:
    """为还没有图的 RefN 自动生成（层 2 调用）。返回 (成功 id, 失败说明)"""
    sb = load_storyboard()
    todo = missing_refs(sb)
    if not todo or not ecom.get_product_images():
        return [], []
    done, failed = [], []
    for rid in todo:
        try:
            generate_ref_image(rid, sb, save=False)
            done.append(rid)
        except Exception as e:
            print(traceback.format_exc())
            failed.append(f"{rid}：{str(e)[:200]}")
        write_storyboard(sb)  # 每张写回
    return done, failed


# ── 角色定妆照：层 2 自动生成，可上传真人照替换，可单独重新生成 ─────────────────
def _find_character(sb: dict, cid: str):
    return next((c for c in characters_of(sb) if c.get("id") == cid), None)


def save_character_image(cid: str, file_path):
    """上传/移除某角色的定妆照。移除后层 2 会重新自动生成"""
    sb = load_storyboard()
    c = _find_character(sb, cid)
    if c is None:
        return f"⚠️ 当前剧本没有角色 {cid}，请重新生成分镜", sb

    CHARACTER_DIR.mkdir(parents=True, exist_ok=True)
    new_path = ""
    if file_path:
        # 与生成的 {cid}_portrait.png 分开命名，上传/生成互不覆盖
        dest = CHARACTER_DIR / f"{cid}_upload{Path(file_path).suffix.lower() or '.png'}"
        clear_dir(CHARACTER_DIR, f"{cid}_upload.*")
        shutil.copy(file_path, dest)
        new_path = str(dest)

    c["image_path"] = new_path
    c["image_source"] = "upload" if new_path else ""
    write_storyboard(sb)
    delete_shot_videos(shots_with_character(sb, cid))

    name = c.get("name", cid)
    if new_path:
        return f"👤 {name} 的定妆照已替换为上传图片，相关镜头视频已作废。", sb
    return f"👤 {name} 的定妆照已移除，执行层 2 时会重新生成。", sb


def regenerate_portrait(cid: str):
    """按 appearance_en 重新生成某角色的定妆照（不满意长相时用）"""
    sb = load_storyboard()
    c = _find_character(sb, cid)
    if c is None:
        return f"⚠️ 当前剧本没有角色 {cid}", sb
    try:
        c["image_path"] = generate_character_portrait(c, output_dir=str(CHARACTER_DIR))
        c["image_source"] = "generated"
    except Exception as e:
        return err(e), sb
    write_storyboard(sb)
    delete_shot_videos(shots_with_character(sb, cid))
    return f"👤 {c.get('name', cid)} 的定妆照已重新生成，相关镜头视频已作废。", sb


# ── 层 2：视觉资产 ────────────────────────────────────────────────────────────
def run_layer2(is_draft):
    if not os.path.exists(STORYBOARD_PATH):
        return "⚠️ 未找到分镜数据，请先执行【1. 策划层】", {}

    # 剧本需要、还没有的产品图：以「📦 产品信息」页的实拍图为参考自动生成（先于 layer2，layer2 会读写同一份 storyboard）
    ref_done, ref_failed = generate_missing_refs()

    # 记录生成前的定妆照路径：草图定妆照被正式版替换后，用旧定妆照渲染的视频要作废
    # 草图→正式时文件名相同（{cid}_portrait.png），所以连同 image_source 一起比较
    before = {c.get("id"): (c.get("image_path"), c.get("image_source")) for c in characters_of(load_storyboard())}

    try:
        if is_draft:
            updated = quick_draft(STORYBOARD_PATH, str(OUTPUT_DIR / "assets" / "shots_draft"))
        else:
            # size 不传：由 layer2 读取分镜 meta.image_size（目标平台决定的画幅）
            updated = generate_all_shots(STORYBOARD_PATH, ASSETS_DIR)
    except Exception as e:
        return err(e), load_storyboard()

    for c in characters_of(updated):
        old = before.get(c.get("id"))
        if old and old[0] and old != (c.get("image_path"), c.get("image_source")):
            delete_shot_videos(shots_with_character(updated, c.get("id")))

    n_chars = len(characters_of(updated))
    msg = "✅ Layer2 视觉资产生成成功！" + (f"（含 {n_chars} 张角色定妆照，见「剧本」页）" if n_chars else "")
    if ref_done:
        msg += f"\n🤖 已按实拍图生成产品图 {', '.join(ref_done)}，请在「② 素材准备」核对包装文字和 logo。"
    if ref_failed:
        msg += "\n❌ 产品图生成失败（可在「② 素材准备」单独重试）：\n" + "\n".join(ref_failed)
    if is_draft and n_chars:
        msg += "\n💡 草图模式的定妆照仅供预览，关闭草图模式再跑一次会重新生成正式定妆照。"
    left = missing_refs(updated)
    if left:
        msg += (f"\n⚠️ 产品镜头图 {', '.join(left)} 还没有，对应的 Ref2VA 镜头无法渲染。"
                + ("请在「② 素材准备」点「🤖 现在生成」。" if ecom.get_product_images()
                   else "请先在「📦 产品信息」页上传产品实拍图，再点「🤖 现在生成」。"))

    # H3 提示词在后台自动生成，供右侧卡片（模式 A 复制）和模式 B 使用。
    # 失败不影响底图结果，模式 B 启动时会再补生成。
    h3_err = ensure_h3_prompts()
    msg += f"\n⚠️ H3 提示词生成失败（渲染前会自动重试）：{h3_err}" if h3_err else "\n✅ H3 提示词已自动生成。"
    return msg, load_storyboard()


# ── 层 3：H3 提示词（后台自动执行，无独立界面）─────────────────────────────────
def ensure_h3_prompts() -> str:
    """为缺少 H3 提示词的镜头补生成（layer3 会跳过已存在的文件，不会重复计费）。成功返回空串，失败返回错误信息"""
    try:
        process_all_prompts(STORYBOARD_PATH, str(PROMPTS_DIR))
        return ""
    except Exception as e:
        return str(e)


# ── 模式 B：Diffusers 批量渲染 ────────────────────────────────────────────────
def batch_render_with_diffusers(force_all=False, progress=gr.Progress()):
    """
    全自动批量调用 Modal Diffusers 渲染，全部成功后自动拼接。
    默认只渲染还没有视频的镜头（失败后重试不会重复花钱重渲已成功的镜头）；
    参考图变更时 save_ref_image / save_character_image 会删掉受影响镜头的视频，自然会被重渲。
    force_all=True 时清空全部镜头视频重新渲染。
    """
    # 始终以磁盘上的 storyboard 为准，避免 UI State 与文件不同步
    sb = load_storyboard()
    shots = sb.get("shots", [])
    if not shots:
        return "⚠️ 请先生成分镜", None

    # 产品图是 Ref2VA 镜头的硬依赖，缺图时直接拦下，避免浪费 GPU 时间
    left = missing_refs(sb)
    if left:
        return f"⚠️ 产品镜头图 {', '.join(left)} 还没有，请先在「② 素材准备」生成，再启动渲染", None
    # 定妆照是人物一致性的关键，缺了就会每个镜头长得不一样
    # 只拦 Ref2VA 镜头真正要用的定妆照（旧版分镜里有角色但仍是 T2VA 的镜头不需要）
    needed = {c for s in shots if str(s.get("mode", "")).upper() == "REF2VA" for c in (s.get("characters") or [])}
    no_portrait = [c for c in missing_portraits(sb) if c in needed]
    if no_portrait:
        return f"⚠️ 角色 {', '.join(no_portrait)} 还没有定妆照，请先执行【2. 视觉资产层】或在「剧本」页上传", None

    # 旧版分镜没有 meta.seed：补一个并写回，保证全片（含以后重渲的单镜）用同一个种子
    meta = sb.setdefault("meta", {})
    if meta.get("seed") is None:
        meta["seed"] = random.randint(0, 2**31 - 1)
        write_storyboard(sb)
    seed = meta["seed"]
    # 不传画幅时 H3 一律出 16:9，竖屏平台会变成横屏
    aspect_ratio = video_aspect_ratio(sb)
    progress(0, desc="检查 / 补生成 H3 提示词")
    h3_err = ensure_h3_prompts()
    missing_prompts = [str(s.get("id")) for s in shots if not prompt_file_for(s).exists()]

    # 1) 本地准备好每个镜头的输入；准备失败的镜头不提交，避免浪费 GPU
    jobs, failures = [], []
    for shot in shots:
        sid = shot_id(shot)
        mode = str(shot.get("mode", "T2VA")).upper()
        if mode == "REF2VA":
            # 按 shot_references 顺序传全部参考图（定妆照在前、产品图在后），
            # 顺序必须与 H3 提示词里的 <Picture N> 一致
            refs = shot_references(sb, shot)
            not_ready = [f"{r['label']}={r['name']}" for r in refs if not _ready(r["path"])]
            if not refs or not_ready:
                failures.append(f"镜头 {sid}: 参考图未就绪 {', '.join(not_ready) or '(无参考图)'}")
                continue
            # 回退用的 image_gen_prompt 没有 <Picture N>，参考图绑不上人物/产品，渲了也是废片
            if not prompt_file_for(shot).exists():
                failures.append(f"镜头 {sid}: Ref2VA 缺少 H3 提示词，未提交渲染（{h3_err or '请重试'}）")
                continue
            images = [Path(r["path"]).read_bytes() for r in refs]
            workflow = "ref2va"
        elif mode in T2V_MODES:
            images, workflow = [], "t2va"
        else:
            failures.append(f"镜头 {sid}: Modal 服务不支持模式 {mode}，请改为 T2VA 或 Ref2VA")
            continue
        jobs.append((sid, workflow, read_h3_prompt(shot), images, duration_to_frames(shot.get("duration_sec"))))

    if failures:
        return "❌ 以下镜头输入未就绪，未提交渲染：\n" + "\n".join(failures), None

    # 2) 逐个镜头串行渲染。t2va / ref2va 各自是独立的容器池，模型各加载一次，
    #    镜头交替出现时不会重复加载
    try:
        Runner = modal.Cls.from_name(MODAL_APP, "H3BatchRunner")
        # from_name 是延迟查找，不 hydrate 的话要到第一次 .remote() 才报错，且每个镜头各报一遍
        Runner.hydrate()
        runners = {wf: Runner(workflow=wf) for wf in {j[1] for j in jobs}}
    except Exception as e:
        return (
            f"❌ 找不到 Modal 服务 {MODAL_APP}：{e}\n\n"
            "请在项目根目录执行：\n"
            "  modal run h3_diffusers_service.py::download_weights   # 首次需要\n"
            "  modal deploy h3_diffusers_service.py\n"
            "然后用 `modal app list` 确认它处于 deployed 状态。",
            None,
        )

    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    # 镜头视频按画幅分批：画幅变了（含修复前一律按 16:9 渲染的旧视频），旧视频不能复用，
    # 否则成片里横竖混拼，-c copy 拼接也会出错
    ratio_mark = VIDEO_DIR / ".aspect_ratio"
    old_ratio = ratio_mark.read_text(encoding="utf-8").strip() if ratio_mark.exists() else "16:9"
    stale_ratio = old_ratio != aspect_ratio and any(VIDEO_DIR.glob("shot_*.mp4"))
    if force_all or stale_ratio:
        clear_dir(VIDEO_DIR, "shot_*.mp4")
    ratio_mark.write_text(aspect_ratio, encoding="utf-8")

    def video_of(sid):
        # 补零命名，保证 shot_10 排在 shot_09 之后
        return VIDEO_DIR / f"shot_{sid:02d}.mp4"

    todo = [j for j in jobs if not video_of(j[0]).exists()]
    skipped = len(jobs) - len(todo)

    # 3) 按 workflow 分组渲染：先全部 t2va，再全部 ref2va。
    #    两个容器池几乎不会同时在线，省一张卡的费用；
    #    文件按镜头编号命名，拼接时仍按分镜顺序，不受渲染顺序影响
    order = sorted(todo, key=lambda j: (j[1] != "t2va", j[0]))
    for idx, (sid, wf, prompt, images, frames) in enumerate(order):
        progress((idx, len(order)), desc=f"渲染镜头 {sid} ({wf} · {aspect_ratio})")
        try:
            video_bytes = runners[wf].render_single_shot.remote(
                prompt, images or None, frames, seed, aspect_ratio=aspect_ratio)
        except Exception as e:
            if "aspect_ratio" in str(e) and "unexpected keyword" in str(e):
                # 线上还是旧版服务，后面的镜头也会同样失败，不必逐个再试
                return (
                    f"❌ Modal 上部署的 {MODAL_APP} 是旧版本，不支持画幅参数。\n"
                    "请在项目根目录重新部署后再渲染：\n  modal deploy h3_diffusers_service.py",
                    None,
                )
            failures.append(f"镜头 {sid}: {e}")
            continue
        video_of(sid).write_bytes(video_bytes)
    progress((len(order), len(order)), desc="渲染完成")

    # jobs 按分镜顺序构建，拼接也按此顺序，包含本轮渲染的和之前已有的镜头
    rendered = [video_of(j[0]) for j in jobs]

    warn = f"\n♻️ 复用已有视频 {skipped} 个镜头（勾选「全部重新渲染」可强制重渲）" if skipped else ""
    if stale_ratio:
        warn += f"\n🔁 画幅由 {old_ratio} 改为 {aspect_ratio}，旧镜头视频已作废并全部重渲"
    if missing_prompts:
        warn += f"\n⚠️ 镜头 {', '.join(missing_prompts)} 未能生成 H3 提示词，已回退使用 image_gen_prompt"
        if h3_err:
            warn += f"（原因：{h3_err}）"
    if failures:
        return (
            f"❌ {len(failures)}/{len(shots)} 个镜头渲染失败，未执行拼接（已成功的镜头保存在 {VIDEO_DIR}）：\n"
            + "\n".join(failures) + warn,
            None,
        )

    status, final = concat_videos(rendered)
    return status + warn, final


# ── 层 4：FFmpeg 拼接 ─────────────────────────────────────────────────────────
def concat_videos(videos: list[Path]):
    if not videos:
        return "⚠️ 未找到渲染好的镜头视频", None

    concat_list_path = VIDEO_DIR / "concat_list.txt"
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for v in videos:
            # concat demuxer 以列表文件所在目录解析相对路径，这里统一写绝对路径；
            # Windows 下转正斜杠，并转义单引号
            safe_v = str(Path(v).resolve()).replace("\\", "/").replace("'", r"'\''")
            f.write(f"file '{safe_v}'\n")

    # 先删除旧成片，避免 FFmpeg 失败时误把上一次的结果当作成功
    if FINAL_VIDEO.exists():
        FINAL_VIDEO.unlink()

    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", str(concat_list_path), "-c", "copy", str(FINAL_VIDEO)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return "❌ 未找到 ffmpeg，请先安装并加入 PATH", None

    if result.returncode != 0 or not FINAL_VIDEO.exists():
        return f"❌ FFmpeg 拼接失败:\n{result.stderr[-2000:]}", None
    return f"✅ {len(videos)} 个镜头已拼接完成！", str(FINAL_VIDEO)


def manual_concat():
    """手动拼接：按当前分镜顺序拼接本地已有的镜头视频"""
    if not VIDEO_DIR.exists():
        return "⚠️ 未找到视频目录", None

    shots = load_storyboard().get("shots", [])
    if shots:
        # 按分镜顺序取，避免混入不属于当前分镜的残留文件
        videos = [VIDEO_DIR / f"shot_{shot_id(s):02d}.mp4" for s in shots]
        missing = [p.name for p in videos if not p.exists()]
        if missing:
            return f"⚠️ 以下镜头视频缺失，无法拼接：{', '.join(missing)}", None
    else:
        videos = sorted(VIDEO_DIR.glob("shot_*.mp4"))
    return concat_videos(videos)


# ── 主图 / 详情页 ─────────────────────────────────────────────────────────────
def ecom_product_images() -> list[str]:
    """电商图的产品参考图：优先用本页上传的；没有则沿用剧本里上传的产品图"""
    own = ecom.get_product_images()
    if own:
        return own
    refs = load_storyboard().get("product_refs", [])
    return [r["image_path"] for r in refs if _ready(r.get("image_path"))][:ecom.MAX_PRODUCT_REFS]


def ecom_portraits() -> dict[str, dict]:
    """已有定妆照的角色，可在主图/详情页中出镜（与视频里是同一个人）"""
    return {c["id"]: c for c in characters_of(load_storyboard()) if _ready(c.get("image_path"))}


def ecom_product_note() -> str:
    """说明当前素材库的情况（本页没传时沿用视频页的产品图）"""
    lib = al.load_library()
    if lib:
        pkg = al.package_refs(lib, ecom.MAX_PRODUCT_REFS)
        has_front = any(e.get("type") == "package_front" for e in lib)
        pending = sum(e.get("type_source") == "pending" for e in lib)
        msg = (f"素材库 {len(lib)} 张；锁定产品外观用：{'、'.join(e['id'] for e in pkg) or '无'}。"
               "主图 / 详情页策划时按类型自动给每屏分配素材。")
        if pending:
            msg += f"\n⏳ {pending} 张还没识别类型，点「🤖 自动识别类型」或手动选。"
        if not has_front:
            msg += "\n⚠️ 没有标为「包装正面」的照片，AI 画面里的产品外观可能不准。"
        return msg
    fallback = ecom_product_images()
    if fallback:
        return f"素材库为空，主图/详情页沿用当前剧本里的 {len(fallback)} 张产品图。"
    return "⚠️ 还没有产品图。建议至少上传：包装正面、包装背面（配料 / 标签），以及产品本体、使用中的实拍。"


def current_category():
    """当前产品信息对应的类目生成策略（照片识别、卡片保存都用它）"""
    return cp.category(cp.resolve(brief_mod.load_brief()))


def ecom_add_photos(files, auto_tag: bool):
    """上传多张照片进素材库；auto_tag 时立即用视觉模型识别类型（每 6 张一次调用）"""
    try:
        paths = [f if isinstance(f, str) else getattr(f, "name", "") for f in (files or [])]
        added, skipped = al.add_photos([p for p in paths if p])
        msg = f"📥 新增 {len(added)} 张" + (f"；跳过 {len(skipped)} 张：" + "；".join(skipped) if skipped else "")
        if added and auto_tag:
            done, errors = ecom.classify_photos(added, cat=current_category())
            msg += f"\n🤖 已识别 {done} 张类型，请在缩略图下核对，分错的直接改。"
            if errors:
                msg += "\n⚠️ " + "\n⚠️ ".join(errors) + "\n未识别的请手动选类型（视觉模型由 ECOM_CHECK_MODEL 指定）。"
        return msg, al.load_library(), None, ecom_product_note()
    except Exception as e:
        return err(e), al.load_library(), None, ecom_product_note()


def ecom_tag_photos(force: bool = False):
    """识别还没有类型的照片；force 时除了手动改过的，全部重新识别"""
    try:
        todo = al.needs_tagging(al.load_library(), force)
        if not todo:
            return "✅ 没有需要识别的照片（手动改过类型的不会被覆盖）", al.load_library(), ecom_product_note()
        done, errors = ecom.classify_photos(todo, cat=current_category())
        msg = f"🤖 已识别 {done}/{len(todo)} 张。" + ("\n⚠️ " + "\n⚠️ ".join(errors) if errors else "")
        return msg, al.load_library(), ecom_product_note()
    except Exception as e:
        return err(e), al.load_library(), ecom_product_note()


def ecom_set_photo_type(pid: str, kind: str):
    al.update_photo(pid, type=kind)
    return f"✅ {pid} 已设为「{al.type_label(kind)}」（手动设置，重新识别时不会覆盖）。已策划的详情页点「🔁 重新分配素材」生效", \
        al.load_library(), ecom_product_note()


def ecom_set_photo_note(pid: str, note: str):
    al.update_photo(pid, note=note)


def ecom_set_label_fields(pid: str, text: str):
    """包装背面的标签信息：每行「名称：值」"""
    fields = {}
    for line in (text or "").splitlines():
        k, sep, v = line.partition("：") if "：" in line else line.partition(":")
        if sep:
            fields[k.strip()] = v.strip()
    al.update_photo(pid, label_fields=fields)


def ecom_remove_photo(pid: str):
    al.remove_photo(pid)
    return f"🗑️ {pid} 已从素材库删除。用到它的详情页点「🔁 重新分配素材」换图", al.load_library(), ecom_product_note()


def _plan_issue_summary(plan: dict) -> str:
    playbook = (plan.get("meta") or {}).get("playbook")
    lines = []
    for it in plan.get("main_images", []) + plan.get("detail_sections", []):
        issues = ecom.item_issues(it, playbook)
        if issues:
            lines.append(f"  {it['id']}：{'、'.join(issues)}")
    return (("\n⚠️ 文案待处理（禁用词/类目风险/占位）：\n" + "\n".join(lines)) if lines
            else "\n✅ 文案未发现禁用词")


def ecom_plan(platform, use_chars, main_count, main_ratio, self_review, *brief_values):
    try:
        brief = collect_brief(brief_values)
        usp = brief_mod.lines_of(brief["usp"])
        if not brief["name"]:
            return "⚠️ 请先在「📦 产品信息」页填写商品名称", gr.skip()
        if not usp:
            return "⚠️ 请先点「分析卖点」或手动填写核心卖点", gr.skip()
        chars = list(ecom_portraits().values()) if use_chars else []
        playbook = cp.resolve(brief)
        plan = ecom.generate_ecom_plan(brief["name"], usp, brief["brand_color"], platform, chars,
                                       main_count=int(main_count), main_ratio=main_ratio,
                                       product_context=brief_mod.brief_context(brief),
                                       playbook=playbook, self_review=bool(self_review),
                                       positioning=brief.get("positioning") or pos.FOLLOW_PLATFORM)
        ecom.reset_outputs()
        ecom.save_plan(plan)
        reviewed = " · 已按评分标准自检修改一轮" if plan["meta"].get("self_reviewed") else \
            (" · 自检后问题没有减少，保留初稿" if self_review else "")
        msg = (f"✅ 已策划 {len(plan['main_images'])} 张 {main_ratio} 主图 + "
               f"{len(plan['detail_sections'])} 张 3:4 详情页（{platform}）。"
               f"\n📚 类目手册：{cp.label(playbook)} · 定位：{pos.label(plan['meta']['positioning'])}{reviewed}"
               f"\n请在右侧核对、修改文案，再点「生成全部图片」。"
               + qc.format_report("图文检查", plan.get("quality_issues") or []) + _plan_issue_summary(plan))
        return msg, plan
    except Exception as e:
        return err(e), gr.skip()


def _ecom_items(plan: dict):
    return [("main", it) for it in plan.get("main_images", [])] + \
           [("detail", it) for it in plan.get("detail_sections", [])]


def ecom_generate_all(force=False, text_mode=ecom.DEFAULT_TEXT_MODE, check_fidelity=False, progress=gr.Progress()):
    """
    并行生成：同时出 ecom.CONCURRENCY 张（各自提交任务、各自轮询，见 ecom.render_many）。
    底图可复用的只重排文字、不再调用接口；失败后再点一次只补失败的。
    素材库在策划之后变过（补拍、删图、改类型）时先自动重新分配素材，手动指定过的屏不动。
    切换写字方式后，有文字的图会重新生成（底图里有没有字不同），白底图等无字图照常复用。
    生成器：每完成一张就存盘，并把最新 plan 推给页面，卡片按完成顺序逐张显示，不用等整套跑完。
    check_fidelity：新生成的主图用视觉模型对比实拍图，核对包装文字 / logo / 配色（每张多一次调用）
    """
    plan = ecom.load_plan()
    if not plan.get("main_images"):
        yield "⚠️ 请先点「策划图文」", gr.skip()
        return
    products = ecom_product_images()
    if not products:
        yield "⚠️ 请先在「📦 产品信息」页上传产品实拍图", gr.skip()
        return
    lib = al.load_library()
    head = ""
    synced = ecom.sync_assets(plan, lib)
    if synced is not None:
        ecom.save_plan(plan)
        if synced["changed"]:
            head = f"🔁 素材库在策划后有变化，已重新分配素材（手动指定的不动）：{'、'.join(synced['changed'])}\n"

    items = _ecom_items(plan)
    total, done = len(items), 0
    generated, reused, failed, waiting, skipped = 0, 0, [], [], []
    progress(0, desc=f"同时出 {ecom.CONCURRENCY} 张")
    yield head + f"⏳ 开始生成 {total} 张（同时 {ecom.CONCURRENCY} 张，先完成的先显示）", ecom.load_plan()
    for kind, it, status, info in ecom.render_many(plan, items, products, ecom_portraits(), text_mode, lib,
                                                    force=bool(force), check_fidelity=bool(check_fidelity)):
        done += 1
        if status == "generated":
            generated += 1
        elif status == "reused":
            reused += 1
        elif status == "needs_photo":
            waiting.append(f"{it['id']}：{info}")
        elif status == "skipped":
            skipped.append(it["id"])
            continue
        else:
            print(f"[电商图] {it['id']} 失败：{info}")
            failed.append(f"{it['id']}：{info}")
        ecom.save_plan(plan)  # 每张写回，中途失败不丢已生成的
        progress(done / total, desc=f"{it['id']} {it['role'][:12]}")
        # 传重新读出的新对象，确保页面检测到变化并重绘
        yield (head + f"⏳ 已完成 {done}/{total}：新生成 {generated}，复用 {reused}，失败 {len(failed)}"
               + (f"，缺实拍 {len(waiting)}" if waiting else "")), ecom.load_plan()

    msg = f"共 {total} 张：新生成 {generated} 张，复用底图重排 {reused} 张。"
    if skipped:
        msg += (f"\n\n⛔ 图像接口连续 2 张服务端故障，已停止，{len(skipped)} 张未尝试（{'、'.join(skipped)}）。"
                "这是中转平台/上游的问题（不是提示词或图片的问题），稍后再点「生成全部图片」会从失败处继续。")
    check = "上架前请逐张核对产品包装文字/logo 是否与实物一致"
    if text_mode == "ai":
        check += "，并逐字核对 AI 写的中文（错字请改文案后点「重新生成画面」，或切换为程序叠字）"
    layout = [f"  {it['id']}：{'；'.join(it['layout_warnings'])}" for _, it in items if it.get("layout_warnings")]
    if layout:
        check += "\n📐 排版提示（自动缩字后仍放不下，或文字区背景较杂）：\n" + "\n".join(layout)
    fidelity = [f"  {it['id']}：{ecom.fidelity_note(it)}" for _, it in items if ecom.fidelity_note(it)]
    if fidelity:
        check += "\n" + "\n".join(fidelity)
    if waiting:
        msg += (f"\n\n📷 {len(waiting)} 屏缺实拍照片，没有出图：\n" + "\n".join(waiting)
                + "\n在这几张卡片上点「📷 上传这屏的参考照片」，再点一次「生成全部图片」只补这几屏。")
    if failed:
        msg = f"❌ {len(failed)} 张失败：\n" + "\n".join(failed) + "\n\n" + msg + "\n再点一次「生成全部图片」只补失败的。"
    else:
        msg = ("✅ " if not (waiting or skipped) else "") + msg + f"\n{check}。"
    yield head + msg + _plan_issue_summary(plan), ecom.load_plan()


def ecom_save_item(kind, item_id, regen, headline, subline, badges, scene, text_mode=ecom.DEFAULT_TEXT_MODE,
                   tag="", price_label="", price="", specs="", layout="standard", compare="",
                   asset_content=None, asset_mode=None, shot=None):
    """
    保存某张图的文案。regen=False：程序叠字的图只重排文字（免费）；AI 写字的图文字在画面里，只保存文案。
    regen=True：按当前写字方式重新生成画面。
    tag：主图左上角角标 / 详情页标题上方小标签；price_label + price：主图价格标签；specs：详情页参数表（每行「名称：值」）
    layout：详情页版式（standard / steps / grid / compare）；compare：对比表文本（首行「左列名｜右列名」，之后「项｜左｜右」）
    asset_content / asset_mode / shot：详情页素材（照片 id，"" = 不用）、出图方式（"" = 自动）、拍法；
    None 表示界面没有这几个控件（不改）。和原来不同时记为手动指定，并在下次生成时重出底图
    """
    try:
        plan = ecom.load_plan()
        it = ecom.find_item(plan, kind, item_id)
        if not it:
            return "⚠️ 找不到这张图，请重新策划", gr.skip()
        cat = ecom.plan_category(plan)       # 这类目的生成策略：图标、版式、素材匹配都按它
        lines = [l.strip() for l in (badges or "").splitlines() if l.strip()]
        tag = (tag or "").strip()
        layout_note = ""
        if kind == "main":
            price = ecom.clean_price(price)
            it.update(headline=headline.strip(), subline=subline.strip(), badges=lines[:3], tag=tag,
                      price=price, price_label=(price_label or "").strip() if price else "")
        else:
            old_share = ecom.detail_share(it)
            it.update(title=headline.strip(), body=subline.strip(), bullets=lines[:4], tag=tag,
                      specs=ecom.normalize_specs(specs or ""), compare=ecom.normalize_compare(compare or ""))
            it["icons"] = ecom.fill_icons(it.get("icons"), it["bullets"], cat)
            wanted = layout if layout in ecom.DETAIL_LAYOUTS else "standard"
            it["layout"] = ecom.normalize_layout(wanted, it, cat)
            if it["layout"] != wanted:
                layout_note = f"\n💡 内容不够「{ecom.DETAIL_LAYOUTS[wanted]}」用（步骤和网格至少 2 条要点，对比表至少 1 行），已按标准版式排"
            if ecom.detail_share(it) > old_share and _ready(it.get("base_path")) and not regen:
                layout_note += "\n💡 信息图需要更大的顶部留白，建议点「重新生成画面」，否则文字可能压到产品"
        it["scene_prompt_en"] = (scene or "").strip()
        lib = al.load_library()
        if asset_content is not None and not it.get("white_bg"):
            old = it.get("assets") or {}
            content, mode = (asset_content or ""), (asset_mode or "")
            if content != (old.get("content") or "") or mode != (old.get("mode") if old.get("source") == "user" else "") \
                    or (kind == "detail" and shot and shot != it.get("shot")):
                ecom.set_item_asset(it, lib, content, mode, shot or "", main=kind == "main", cat=cat)
                layout_note += f"\n🖼 素材已改为：{al.asset_note(it['assets'], lib)}"
                if it["assets"]["mode"] != "photo" and not regen:
                    layout_note += "\n💡 换了素材或拍法，点「重新生成画面」后生效"

        # 实拍直出是本地合成、不花钱：换成实拍直出时直接重出底图；AI 画面只在点「重新生成画面」时重画
        free_redo = kind == "detail" and (it.get("assets") or {}).get("mode") == "photo" \
            and ecom.needs_regen(it, text_mode, kind, lib)
        if regen or free_redo:
            products = ecom_product_images()
            if not products and not lib:
                return "⚠️ 请先上传产品图", plan
            try:
                ecom.render_item(plan, kind, it, products, ecom_portraits(), text_mode, lib)
            except ecom.NeedsPhoto as e:
                ecom.save_plan(plan)    # 文案照常保存，只是这屏先不出图
                return f"📷 {item_id} 文案已保存。{e}" + layout_note, plan
            msg = f"✅ {item_id} 已重新生成（{al.MODES.get(it['base_mode']) or ecom.TEXT_MODES.get(it['base_mode'], '')}）"
        elif _ready(it.get("base_path")) and it.get("base_mode") == "ai" and ecom.has_text(it):
            msg = f"✅ {item_id} 文案已保存。这张图的文字是 AI 画在画面里的，点「重新生成画面」后才会更新"
        elif _ready(it.get("base_path")):
            ecom.compose_item(plan, kind, it)
            msg = f"✅ {item_id} 文案已更新并重新排版"
        else:
            msg = f"✅ {item_id} 文案已保存（还没有底图，生成后生效）"

        ecom.save_plan(plan)
        issues = ecom.item_issues(it, (plan.get("meta") or {}).get("playbook"))
        layout = it.get("layout_warnings") or []
        return (msg + (f"\n⚠️ {'、'.join(issues)}" if issues else "")
                + (f"\n📐 排版提示：{'；'.join(layout)}" if layout else "")
                + layout_note), plan
    except Exception as e:
        return err(e), gr.skip()


def ecom_item_upload(kind, item_id, file):
    """
    卡片上给这一屏上传参考照片：放进素材库，类型按这屏要的记（算手动设置，不等识别），并指定为这屏的素材
    （手动指定，重新分配素材时保持不变）。同一张照片已经在素材库里就直接用那一张
    """
    try:
        plan = ecom.load_plan()
        it = ecom.find_item(plan, kind, item_id)
        path = file if isinstance(file, str) else getattr(file, "name", "")
        if not it or not path:
            return "⚠️ 找不到这张图，或没收到照片", gr.skip(), gr.skip(), gr.skip()
        added, skipped = al.add_photos([path])
        lib = al.load_library()
        entry = added[0] if added else al.find_same(lib, path)
        if not entry:
            return "⚠️ " + "；".join(skipped), gr.skip(), gr.skip(), gr.skip()
        if added:
            need = al.role_need(it.get("role", ""), kind == "main", ecom.plan_category(plan))
            al.update_photo(entry["id"], type=(it.get("assets") or {}).get("missing") or need["types"][0])
            lib = al.load_library()
        a = ecom.set_item_asset(it, lib, entry["id"], "", "", main=kind == "main", cat=ecom.plan_category(plan))
        ecom.save_plan(plan)
        e = al.find(lib, entry["id"])
        msg = (f"📥 {item_id} 的参考照片：{al.describe(e)}（类型不对就到「③ 产品素材库」改）\n"
               f"{al.asset_note(a, lib)}\n点「🔄 重新生成画面」或「生成全部图片」后生效。")
        return msg, plan, lib, ecom_product_note()
    except Exception as ex:
        return err(ex), gr.skip(), gr.skip(), gr.skip()


def ecom_export():
    """打包成品。zip 以「商品名_时间」命名存进 exports/，清空后也不会丢"""
    final = ecom.FINAL_DIR
    if not final.exists() or not any(final.glob("*.jpg")):
        return "⚠️ 还没有生成好的图片", None
    name = brief_mod.load_brief().get("name") or "ecom"
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_")[:40] or "ecom"
    ecom.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    archive = shutil.make_archive(str(ecom.EXPORT_DIR / f"{name}_{stamp}"), "zip", root_dir=str(final))
    n = len(list(final.glob("*.jpg")))
    return (f"✅ 已打包 {n} 张图：{Path(archive).name}（另存于 pipeline/output/ecom/exports/）。"
            "\n做下一个产品前，点「🧹 清空，开始新产品」。"), archive


def ecom_reassign():
    """素材库变了之后重新给详情页分配素材和拍法（文案不动，手动指定过的屏保持不变）"""
    try:
        plan = ecom.load_plan()
        if not plan.get("detail_sections"):
            return "⚠️ 请先点「策划图文」", gr.skip()
        lib = al.load_library()
        res = ecom.reassign_assets(plan, lib)
        ecom.save_plan(plan)
        msg = (f"🔁 已重新分配素材：{len(res['changed'])} 屏有变化（{'、'.join(res['changed']) or '无'}），"
               "下次「生成全部图片」时只重出这几屏的底图。")
        return msg + qc.format_report("素材检查", res["issues"]), plan
    except Exception as e:
        return err(e), gr.skip()


def ecom_clear():
    """清空本页的策划、底图、成品和素材库（「📦 产品信息」页的文字不动，已打包的 zip 保留）"""
    try:
        ecom.clear_all()
        return ("🧹 已清空主图/详情页的策划、底图、成品和产品素材库（已打包的 zip 保留在 exports/；"
                "已生成的视频剧本自带一份产品图，不受影响）。"
                "\n请在「📦 产品信息」页改成新产品的信息，上传新产品图后重新策划。"), {}, None, ecom_product_note(), []
    except Exception as e:
        return err(e), gr.skip(), gr.skip(), gr.skip(), gr.skip()


def render_photo_card(e: dict):
    """在 @gr.render 上下文中绘制素材库里的一张照片：缩略图、类型（可改）、备注、包装背面的标签信息、删除"""
    with gr.Column(min_width=150, elem_classes="asset-card"):
        q = e.get("quality") or {}
        src = {"pending": "待识别", "auto": "自动识别", "user": "手动", "legacy": "旧图位"}.get(e.get("type_source"), "")
        gr.Image(value=e["path"], label=f"{e['id']} · {src}", interactive=False, height=150, show_label=True)
        kind = gr.Dropdown(choices=al.type_choices(), value=e.get("type", al.DEFAULT_TYPE), show_label=False,
                           interactive=True)
        note = gr.Textbox(value=e.get("note", ""), placeholder="内容说明（选填）", show_label=False,
                          max_lines=1, interactive=True)
        gr.Markdown(al.quality_note(q), elem_classes="hint")
        if e.get("type") == "package_back":
            fields = "\n".join(f"{k}：{v}" for k, v in (e.get("label_fields") or {}).items())
            labels = gr.Textbox(value=fields, lines=3, interactive=True,
                                label="标签信息（自动读取，请核对；每行「名称：值」）")
            labels.blur(fn=lambda t, p=e["id"]: ecom_set_label_fields(p, t), inputs=[labels], outputs=[])
        btn_del = gr.Button("🗑️ 删除", size="sm")
    pid = e["id"]
    kind.input(fn=lambda k, p=pid: ecom_set_photo_type(p, k), inputs=[kind],
               outputs=[status_text, photo_state, ecom_product_md])
    note.blur(fn=lambda t, p=pid: ecom_set_photo_note(p, t), inputs=[note], outputs=[])
    btn_del.click(fn=lambda p=pid: ecom_remove_photo(p), inputs=[],
                  outputs=[status_text, photo_state, ecom_product_md])


def render_ecom_card(kind: str, it: dict, playbook: str | None = None, lib: list[dict] | None = None,
                     stamp: str = ""):
    """
    在 @gr.render 上下文中绘制单张主图/详情页卡片；lib：素材库（卡片显示和修改这一屏用哪张素材）。
    stamp：策划时间。输入框按「策划时间 + 编号」设 key：生成时每完成一张页面都会重绘，
    有 key 的框重绘后保留用户还没保存的编辑；重新策划后 key 变了，才换成新文案
    """
    is_main = kind == "main"
    lib = lib or []
    iid = it["id"]

    def key(name):
        return f"ecom-{stamp}-{iid}-{name}"
    issues = ecom.item_issues(it, playbook)
    final = it.get("final_path") if _ready(it.get("final_path")) else None
    with gr.Group(elem_classes="asset-card"), gr.Row():
        with gr.Column(scale=1, min_width=200):
            gr.Image(value=final, label=f"{it['id']} 成品" if final else f"{it['id']}（未生成）",
                     interactive=False, height=320, key=key("img"), preserved_by_key=None)
        with gr.Column(scale=2):
            people = "、".join(it.get("use_characters") or [])
            chips = [ui.chip("已生成", "ok") if final else ui.chip("待生成")]
            if final and ecom.has_text(it):
                if it.get("base_mode") == "ai":
                    chips.append(ui.chip("AI 写字 · 请逐字核对", "warn"))
                else:
                    chips.append(ui.chip("程序叠字" + ("（简版排版）" if it.get("renderer") == "pil" else ""), "accent"))
            if it.get("white_bg"):
                chips.append(ui.chip("白底图 · 不加文字"))
            if people:
                chips.append(ui.chip(f"出镜：{people}"))
            if it.get("shot"):
                chips.append(ui.chip(f"📷 {al.shot_label(it['shot'])}"))
            if (it.get("assets") or {}).get("needs_photo"):
                chips.append(ui.chip("缺实拍 · 上传后才出图", "warn"))
            layout = it.get("layout_warnings") or []
            fidelity = ecom.fidelity_note(it)
            warnings = ([f"⚠️ {'、'.join(issues)}"] if issues else []) + \
                       ([f"📐 排版提示：{'；'.join(layout)}"] if layout else []) + \
                       ([fidelity] if fidelity else [])
            notes = [al.asset_note(it.get("assets"), lib)] if it.get("assets") else []
            gr.HTML(ui.card_head(it["id"], it["role"], chips, warnings, notes=notes), key=key("head"),
                    preserved_by_key=None)
            if it.get("white_bg"):
                head = gr.Textbox(value="", visible=False)
                sub = gr.Textbox(value="", visible=False)
                badges = gr.Textbox(value="", visible=False)
                tag = gr.Textbox(value="", visible=False)
                price_label = gr.Textbox(value="", visible=False)
                price = gr.Textbox(value="", visible=False)
                specs = gr.Textbox(value="", visible=False)
                layout = gr.Textbox(value="standard", visible=False)
                compare = gr.Textbox(value="", visible=False)
            else:
                # gr.render 里新建的组件必须显式 interactive=True：否则前端会把「有默认值」的框当成只读展示
                # （策划后主标题/副标题/卖点有值就改不了，留空的角标/价格反而能改）
                head = gr.Textbox(label="主标题" if is_main else "标题",
                                  value=it.get("headline" if is_main else "title", ""), interactive=True, key=key("title"))
                sub = gr.Textbox(label="副标题" if is_main else "正文", interactive=True,
                                 value=it.get("subline" if is_main else "body", ""), lines=1 if is_main else 2, key=key("sub"))
                badges = gr.Textbox(label="卖点标签（每行一个，最多 3 个）" if is_main
                                    else "要点（每行一条，最多 4 条；步骤用 ①②③ 开头）", interactive=True,
                                    value="\n".join(it.get("badges" if is_main else "bullets", [])), lines=2, key=key("badges"))
                with gr.Row():
                    tag = gr.Textbox(label="左上角角标（选填）" if is_main else "标题上方小标签（选填）",
                                     value=it.get("tag", ""), max_lines=1, interactive=True, key=key("tag"))
                    price_label = gr.Textbox(label="价格说明（如 券后价）", value=it.get("price_label", ""),
                                             max_lines=1, visible=is_main, interactive=True, key=key("price_label"))
                    price = gr.Textbox(label="价格（数字，不带 ¥；留空则不显示）", value=it.get("price", ""),
                                       max_lines=1, visible=is_main, interactive=True, key=key("price"))
                specs = gr.Textbox(label="参数表（选填，每行「名称：值」，最多 6 行）", interactive=True,
                                   value=ecom.specs_text(it.get("specs")), lines=3, visible=not is_main, key=key("specs"))
                with gr.Row(visible=not is_main):
                    layout = gr.Dropdown(label="版式（信息图由程序排版，文字准确）", choices=ecom.layout_choices(),
                                         value=it.get("layout") or "standard", interactive=True, key=key("layout"))
                    compare = gr.Textbox(label="对比表（版式选对比表时用）", interactive=True, lines=3,
                                         value=ecom.compare_text(it.get("compare")),
                                         placeholder="普通做法｜本品\n冲泡｜要洗茶｜开水直接冲\n口感｜偏苦涩｜回味甘甜", key=key("compare"))
            if lib and not it.get("white_bg"):
                a = it.get("assets") or {}
                modes = [(v, k) for k, v in al.MODES.items() if not (is_main and k == "photo")]   # 主图不实拍直出
                # key 带上当前分配：分配变了（卡片上传照片、重新分配素材）就换成新值，没变时保留还没保存的选择
                cur = f"{a.get('content') or ''}-{a.get('mode') or ''}-{a.get('source') or ''}-{it.get('shot') or ''}"
                with gr.Row():
                    asset_content = gr.Dropdown(
                        label="素材照片（AI 照着它画，不凭空编）", interactive=True, value=a.get("content") or "",
                        choices=[("不指定", "")] + [(al.describe(e), e["id"]) for e in lib],
                        key=key(f"content-{cur}"))
                    asset_mode = gr.Dropdown(
                        label="出图方式", interactive=True,
                        value=a.get("mode") if a.get("source") == "user" else "",
                        choices=[("自动", "")] + modes, key=key(f"mode-{cur}"))
                    if is_main:
                        shot = gr.Textbox(value="", visible=False)
                    else:
                        shot = gr.Dropdown(label="拍法（实拍直出时不用）", interactive=True,
                                           value=it.get("shot") if it.get("shot") in al.SHOTS else None,
                                           choices=al.shot_choices(), key=key(f"shot-{cur}"))
            else:
                asset_content = gr.Textbox(value="", visible=False)
                asset_mode = gr.Textbox(value="", visible=False)
                shot = gr.Textbox(value="", visible=False)
            with gr.Accordion("画面描述（英文，改后点重新生成画面）", open=False):
                scene = gr.Textbox(value=it.get("scene_prompt_en", ""), lines=4, show_label=False,
                                   interactive=True, key=key("scene"))
            with gr.Row():
                btn_text = gr.Button("💾 保存文字并重排", size="sm", variant="primary")
                btn_regen = gr.Button("🔄 重新生成画面", size="sm")
                btn_up = None if it.get("white_bg") else gr.UploadButton(
                    "📷 上传这屏的参考照片", size="sm", file_types=["image"], file_count="single")
    if btn_up is not None:
        btn_up.upload(fn=lambda f, k=kind, i=iid: ecom_item_upload(k, i, f), inputs=[btn_up],
                      outputs=[status_text, ecom_state, photo_state, ecom_product_md],
                      concurrency_id="pipeline", concurrency_limit=1)
    # 写字方式取页面上的全局选项，「重新生成画面」按当前选择出图
    fields = [head, sub, badges, scene, ecom_text_mode, tag, price_label, price, specs, layout, compare]
    # 白底图 / 没有素材库时这三个是隐藏占位框，传 None 表示不改素材
    use_assets = bool(lib) and not it.get("white_bg")
    fields += [asset_content, asset_mode, shot]
    # kind / iid 用默认参数绑定，避免闭包在循环中取到最后一个值
    btn_text.click(fn=lambda *v, k=kind, i=iid, u=use_assets: ecom_save_item(k, i, False, *_asset_args(v, u)),
                   inputs=fields, outputs=[status_text, ecom_state], concurrency_id="pipeline", concurrency_limit=1)
    btn_regen.click(fn=lambda *v, k=kind, i=iid, u=use_assets: ecom_save_item(k, i, True, *_asset_args(v, u)),
                    inputs=fields, outputs=[status_text, ecom_state], concurrency_id="pipeline", concurrency_limit=1)


def _asset_args(values, use_assets: bool):
    """卡片输入 → ecom_save_item 的参数：没有素材控件时最后三个传 None（不改素材）"""
    values = list(values)
    return values if use_assets else values[:-3] + [None, None, None]


# ── UI ────────────────────────────────────────────────────────────────────────
def render_shot_card(sb, shot, char_names, scene_names):
    """在 @gr.render 上下文中绘制单个镜头卡片"""
    mode = str(shot.get("mode", "")).upper()
    has_h3 = prompt_file_for(shot).exists()
    who = "、".join(char_names.get(c, c) for c in shot.get("characters", [])) or "无人物"
    where = scene_names.get(shot.get("scene"), "—")
    with gr.Group(elem_classes="asset-card"):
        with gr.Row():
            with gr.Column(scale=1):
                chips = [ui.chip(f"{shot.get('duration_sec')}s"), ui.chip(shot.get("mode") or "—", "accent"),
                         ui.chip(f"节拍：{shot.get('emotion_beat') or '—'}"),
                         ui.chip(f"👤 {who}"), ui.chip(f"🏠 {where}"),
                         ui.chip("H3 提示词已就绪", "ok") if has_h3 else ui.chip("H3 提示词未生成", "warn")]
                gr.HTML(ui.card_head(f"S{shot_id(shot):02d}", f"镜头 {shot.get('id')}", chips))
                gr.Textbox(label="🗣️ 口播台词", value=shot.get("voiceover", ""), interactive=False)
                gr.Textbox(label="🎥 视觉构图描述", value=shot.get("visual_intent", ""), lines=2, interactive=False)
                gr.Textbox(
                    label="📝 H3 控制提示词 (复制到 ComfyUI MiniMax H3 节点)" if has_h3 else "📝 生图提示词 (尚未生成 H3 提示词)",
                    value=read_h3_prompt(shot), lines=4, interactive=False, **COPY_BTN,
                )

            with gr.Column(scale=1):
                img_path = shot.get("image_path", "")
                if mode == "REF2VA":
                    refs = shot_references(sb, shot)
                    # 告诉手动用户每张图该接到 ComfyUI 的哪个输入口
                    slots = "\n".join(
                        f"- `{r['label']}` ← {'👤 ' + r['name'] + ' 定妆照' if r['kind'] == 'character' else '📦 产品图 ' + r['id']}"
                        f"{'' if _ready(r['path']) else ' *(未就绪)*'}"
                        for r in refs
                    )
                    gr.Markdown(f"📸 **参考图生视频 (Ref2VA)**，按顺序接入 ComfyUI：\n{slots}")
                    ready = [(r["path"], r["label"]) for r in refs if _ready(r["path"])]
                    if ready:
                        gr.Gallery(value=ready, label="参考图", columns=len(ready), height=220,
                                   interactive=False, allow_preview=True)
                elif mode in T2V_MODES:
                    gr.Markdown("💡 **纯文生视频 (T2VA)**：无需参考图，直接由 H3 凭 Prompt 渲染。")
                    if _ready(img_path):
                        gr.Image(value=img_path, label="构图参考 (仅供确认，不传给 H3)", interactive=False)
                else:
                    gr.Markdown(f"📸 **图生视频 ({mode})** - 使用层 2 生成的底图")
                    if _ready(img_path):
                        gr.Image(value=img_path, label="基底图", interactive=False)
                    else:
                        gr.Markdown("*等待层 2 生成底图...*")

        with gr.Accordion("🛠️ 模式 A：单镜手动生成 (ComfyUI)", open=False):
            gr.Markdown(
                "复制上方 H3 提示词，粘贴到 ComfyUI 的 MiniMax H3 节点（Ref2VA 工作流）；"
                "参考图按右侧 Picture 编号顺序接入，顺序错了人物和产品会对调。"
                f"种子统一填 `{(sb.get('meta') or {}).get('seed', '随机')}`，全片画风更一致。"
            )
            if COMFYUI_URL:
                gr.Button("🔗 打开 Modal ComfyUI 页面", link=COMFYUI_URL, size="sm")
            else:
                gr.Markdown("未配置 `COMFYUI_URL`：部署 `comfyui_minimax_h3_modal.py` 后把地址写进 `pipeline/.env`。")


with gr.Blocks(title="AI 带货内容全链路流水线", **BLOCKS_KW) as demo:
    gr.HTML(ui.header_html("AI 带货内容全链路流水线", "一份产品信息，生成带货视频、主图和详情页",
                           ["产品信息", "带货视频", "主图 / 详情页"]))

    # 保存完整 storyboard（shots + product_refs + meta），刷新页面时从磁盘恢复
    sb_state = gr.State(load_storyboard)
    _brief = brief_mod.load_brief()

    # ── 运行日志：所有步骤共用，固定在右侧栏，切页、滚动时都看得到 ─────────────────
    _log_kw = dict(label="运行日志", show_label=False, interactive=False, autoscroll=True, elem_classes="console",
                   placeholder="点任意步骤按钮后，进度和结果显示在这里")
    if HAS_RIGHT_SIDEBAR:
        with gr.Sidebar(position="right", open=True, width=380, elem_classes="console-sidebar"):
            gr.HTML(ui.console_head_html())
            status_text = gr.Textbox(lines=26, max_lines=26, **_log_kw)
    else:
        with gr.Accordion("运行日志", open=True):
            status_text = gr.Textbox(lines=8, max_lines=8, **_log_kw)

    with gr.Tabs(elem_classes="nav-tabs"):
        # ════════════════════════════ 产品信息（视频和图文共用） ════════════════════════════
        with gr.Tab("📦 产品信息"):
            with gr.Row(equal_height=False):
                with gr.Column(scale=1, min_width=320):
                    with gr.Accordion("① 填写产品信息", open=True):
                        gr.Markdown("越具体，卖点分析越准。带货视频和主图/详情页共用这里的信息。", elem_classes="hint")
                        with gr.Row():
                            prod_name = gr.Textbox(label="商品名称", value=_brief["name"], scale=3)
                            prod_color = gr.ColorPicker(label="品牌主色", value=_brief["brand_color"],
                                                        scale=1, min_width=110)
                        prod_category = gr.Textbox(label="类目", value=_brief["category"],
                                                   placeholder="如：美妆护肤 · 精华 / 3C · 剃须刀")
                        prod_desc = gr.Textbox(label="产品描述 / 参数 / 特性", value=_brief["description"], lines=4,
                                               placeholder="成分、材质、规格、功能、工艺、包装内含……")
                        with gr.Row():
                            prod_audience = gr.Textbox(label="目标人群（选填）", value=_brief["audience"])
                            prod_price = gr.Textbox(label="价格区间（选填）", value=_brief["price"])
                        prod_endorse = gr.Textbox(
                            label="真实背书（选填，只有这里写的才会出现在文案中）", value=_brief["endorsements"],
                            lines=2, placeholder="证书、专利、检测报告编号、真实销量……没有就留空",
                        )
                        prod_reviews = gr.Textbox(
                            label="买家评价 / 问大家 / 竞品卖点（选填，粘贴原文）", value=_brief["reviews"], lines=3,
                            placeholder="真实的买家原话最能说明痛点和用语。只用来提炼痛点，不会被当作评价写进文案",
                        )
                        with gr.Row():
                            prod_playbook = gr.Dropdown(
                                choices=cp.choices(), scale=2,
                                # 保存的自定义手册被删掉时退回自动识别，否则下拉框的值不在选项里
                                value=_brief["playbook"] if _brief["playbook"] in [v for _, v in cp.choices()] else cp.AUTO,
                                label="类目手册（决定卖点角度、详情页屏序、类目合规词）",
                            )
                            prod_playbook_md = gr.Markdown(playbook_info(_brief), elem_classes="hint")
                        prod_positioning = gr.Dropdown(
                            choices=pos.choices(),
                            value=_brief.get("positioning") if _brief.get("positioning") in [v for _, v in pos.choices()]
                            else pos.FOLLOW_PLATFORM,
                            label="产品定位（决定详情页屏序、语气、画面方向和价格展示）",
                            info="同一产品上不同渠道打法不同：拼多多拼性价比，微信 / 视频号私域讲品牌和故事。"
                                 "选「跟随平台」时按所选平台自动决定",
                        )
                        with gr.Accordion("➕ 新建 / 编辑自定义类目手册（内置手册里没有你的类目时用）", open=False):
                            gr.Markdown(
                                "点「AI 起草」按上面的商品信息生成一份手册草稿，核对、修改后保存。"
                                "`parent` 是最接近的内置类目，它的合规风险词会被继承、不能删减；"
                                "`compliance` 里写这个类目额外要查的风险词。保存在 `pipeline/playbooks/`。"
                                "风险词只是初筛，食品、保健、宠物饲料等类目上线前请找法务或类目专家确认。",
                                elem_classes="hint",
                            )
                            playbook_editor = gr.Code(language="json", label="手册内容（JSON）", lines=14,
                                                      interactive=True)
                            with gr.Row():
                                btn_draft_pb = gr.Button("🤖 AI 起草", size="sm")
                                btn_save_pb = gr.Button("💾 保存并选用", size="sm", variant="primary")
                                btn_del_pb = gr.Button("🗑️ 删除当前选中的自定义手册", size="sm")
                        btn_check = gr.Button("🩺 输入体检（检查还缺哪些信息）", size="sm")
                        btn_analyze = gr.Button("🔍 分析卖点（再点一次即重新分析）", variant="primary")
                with gr.Column(scale=1, min_width=320):
                    with gr.Accordion("② 卖点分析", open=True):
                        gr.Markdown("LLM 生成，可直接修改；后续剧本和图文都以这里为准。", elem_classes="hint")
                        prod_usp = gr.Textbox(label="核心卖点（每行一个，按说服力排序）", value=_brief["usp"], lines=4)
                        prod_pain = gr.Textbox(label="用户痛点（每行一个）", value=_brief["pain_points"], lines=2)
                        prod_diff = gr.Textbox(label="差异化", value=_brief["differentiation"], lines=1)
                        prod_persona = gr.Textbox(label="目标人群画像", value=_brief["persona"], lines=1)
                        prod_scenes = gr.Textbox(label="使用场景（每行一个）", value=_brief["scenes"], lines=2)

            with gr.Accordion("③ 产品素材库（实拍照片）", open=True):
                gr.Markdown(
                    f"一次拖入多张（最多 {al.MAX_PHOTOS} 张），手机随手拍、精修图都可以：包装正面 / 背面（配料、标签）、"
                    "产品本体（干茶、片剂）、使用中（茶汤、宠物进食）、叶底 / 质地、原料 / 产地、证书。"
                    "上传后自动识别类型，策划时按类型给每屏分配素材：证据类的屏直接用实拍照片，"
                    "需要新构图的屏以包装图锁定外观再生成。分错类型的直接在下面改。",
                    elem_classes="hint")
                with gr.Row():
                    photo_upload = gr.File(label="拖入照片（可多选）", file_count="multiple", file_types=["image"],
                                           height=110, scale=3)
                    with gr.Column(scale=1, min_width=160):
                        photo_auto_tag = gr.Checkbox(label="上传后自动识别类型（视觉模型，每 6 张一次调用）",
                                                     value=True)
                        btn_tag = gr.Button("🤖 自动识别类型", size="sm")
                        btn_retag = gr.Button("重新识别（手动改过的不动）", size="sm")
                ecom_product_md = gr.Markdown(ecom_product_note, elem_classes="hint")
                photo_state = gr.State(al.load_library)

                @gr.render(inputs=[photo_state])
                def render_photos(lib):
                    lib = lib or []
                    if not lib:
                        return
                    for start in range(0, len(lib), 4):
                        with gr.Row():
                            for e in lib[start:start + 4]:
                                render_photo_card(e)
            gr.Markdown("填好后切到上方「🎬 带货视频」或「🖼️ 主图 / 详情页」继续。每一步的进度和结果都显示在右侧运行日志。",
                        elem_classes="tip-card")

        # 顺序必须与 BRIEF_FIELDS 一致（INPUT_FIELDS + ANALYSIS_FIELDS）
        brief_inputs = [prod_name, prod_category, prod_desc, prod_audience, prod_price, prod_endorse, prod_color,
                        prod_reviews, prod_playbook, prod_positioning,
                        prod_usp, prod_pain, prod_diff, prod_persona, prod_scenes]
        analysis_outputs = [prod_usp, prod_pain, prod_diff, prod_persona, prod_scenes]

        # ════════════════════════════ 带货视频 ════════════════════════════
        with gr.Tab("🎬 带货视频"), gr.Row():
            with gr.Column(scale=2, min_width=340):
                with gr.Accordion("① 剧本策划", open=True):
                    story_template = gr.Dropdown(
                        choices=template_choices(), value=DEFAULT_TEMPLATE,
                        label="视频类型 · 剧本模板 (决定编导角色、镜头数量和每个镜头的叙事任务)",
                    )
                    template_desc = gr.Markdown(template_info(DEFAULT_TEMPLATE))
                    custom_beats = gr.Textbox(
                        label="自定义节拍", lines=4, visible=False,
                        placeholder="开场钩子 | A-Roll | 主播举着茶杯问：这层白霜是什么？\n"
                                    "卖点展示 | B-Roll | 莓茶叶片上白霜的微距特写\n"
                                    "使用场景 | A-Roll | 办公室里冲泡、喝第一口的表情\n"
                                    "行动召唤 | A-Roll | 主播说出适合谁喝",
                    )

                    platform = gr.Dropdown(
                        choices=list(PLATFORMS), value=DEFAULT_PLATFORM,
                        label="目标平台 (决定画幅、单镜时长、口播字数和推荐模板)",
                    )
                    platform_desc = gr.Markdown(platform_info(DEFAULT_PLATFORM, DEFAULT_TEMPLATE))

                    agent_skill = gr.Dropdown(
                        choices=skill_choices, value=default_skills, multiselect=True,
                        label="编剧技能 (默认选中项目自带的短视频带货技能；长剧/舞台技能会稀释重点，按需添加)",
                    )
                    include_skill_refs = gr.Checkbox(
                        label=f"同时注入技能参考资料 reference*.md (更贴合技法，但更耗 token，上限 {SKILL_CHAR_BUDGET} 字符)",
                        value=False,
                    )
                    video_review = gr.Checkbox(
                        label="生成后让模型按评分标准自检并修改一轮（多一次 LLM 调用，约多一倍耗时和 token）",
                        value=False,
                    )
                    video_concept = gr.Checkbox(
                        label="先出 3 个创意方向，选最好的一个再写分镜（多一次 LLM 调用；钩子和冲突更有想法）",
                        value=True,
                    )
                    btn_layer1 = gr.Button("1️⃣ 生成分镜脚本", variant="primary")

                with gr.Accordion("② 素材准备：产品图 + 定妆照 + 基底图", open=True):
                    @gr.render(inputs=[sb_state])
                    def render_ref_uploads(sb):
                        refs = (sb or {}).get("product_refs", [])
                        if not (sb or {}).get("shots"):
                            gr.Markdown("生成分镜后，这里会列出剧本需要的真实产品图和拍摄要求。", elem_classes="hint")
                            return
                        if not refs:
                            gr.Markdown("💡 本剧本没有真实产品入画的镜头，无需上传产品图。", elem_classes="hint")
                            return

                        gr.Markdown("**📸 剧本需要的产品镜头图**：以「📦 产品信息」页的实拍图为参考由 AI 生成，"
                                    "执行 2️⃣ 时自动补齐；不满意点「重新生成」。", elem_classes="hint")
                        for ref in refs:
                            rid = ref["id"]
                            used = "、".join(f"镜头 {s}" for s in ref.get("used_in", []))
                            path = ref.get("image_path", "")
                            done = bool(path and os.path.exists(path))
                            with gr.Group(elem_classes="asset-card"):
                                gr.HTML(ui.card_head(rid, f"用于 {used}" if used else "产品镜头图",
                                                     [ui.chip("已生成", "ok") if done else ui.chip("待生成")],
                                                     notes=[ref.get("desc", "")]))
                                # 只展示，不在这里上传；产品实拍图统一在「📦 产品信息」页上传
                                gr.Image(value=path if done else None, interactive=False, height=180,
                                         label=rid if done else f"{rid}（待生成）")
                                btn_gen = gr.Button("🔄 重新生成" if done else "🤖 现在生成", size="sm")
                            # rid 用默认参数绑定，避免闭包在循环中取到最后一个值
                            btn_gen.click(fn=lambda rid=rid: run_generate_ref(rid), inputs=[],
                                          outputs=[status_text, sb_state],
                                          concurrency_id="pipeline", concurrency_limit=1)

                    gr.Markdown("👤 角色定妆照在右边「📜 剧本」页查看、重新生成或上传替换。", elem_classes="hint")
                    is_draft = gr.Checkbox(label="草图模式 (nano-banana-2-lite，又快又便宜，固定 1:1)", value=False)
                    btn_layer2 = gr.Button("2️⃣ 生成定妆照 + 基底图 (完成后自动生成 H3 提示词)", variant="primary")

                with gr.Accordion("③ 渲染与合成", open=True):
                    gr.Markdown(
                        "**模式 B · Diffusers 全自动**：一键在云端渲染并拼接成片。"
                        "默认只渲染还没有视频的镜头（每镜约 6 分钟 GPU），失败后再点一次只补失败的。",
                        elem_classes="hint",
                    )
                    force_all = gr.Checkbox(label="全部重新渲染（忽略已有镜头视频）", value=False)
                    btn_batch_run = gr.Button("🚀 批量渲染并自动拼接", variant="primary")
                    gr.Markdown("**模式 A · ComfyUI 手动**：在右边「🎞️ 分镜脚本」页逐镜复制提示词；渲染好的视频放进 "
                                "`pipeline/output/videos/` 后点下方拼接。", elem_classes="hint")
                    btn_layer4 = gr.Button("4️⃣ 拼接本地已有的镜头视频 (FFmpeg)", variant="secondary")

            with gr.Column(scale=3), gr.Tabs():
                with gr.Tab("📜 剧本"):
                    @gr.render(inputs=[sb_state])
                    def render_script(sb):
                        script = (sb or {}).get("script") or {}
                        if not (sb or {}).get("shots"):
                            gr.Markdown("在左侧点「1️⃣ 生成分镜脚本」\n\n生成后这里显示完整剧本、角色定妆照和场景设定。",
                                        elem_classes="empty-state")
                            return
                        if not script.get("full_text") and not script.get("characters"):
                            gr.Markdown("⚠️ 当前分镜是旧版本生成的，没有剧本数据，请重新生成分镜。", elem_classes="tip-card")
                            return

                        gr.Markdown(f"## {script.get('title') or '未命名'}\n\n{script.get('logline', '')}")
                        gr.Textbox(label="完整剧本", value=script.get("full_text", ""), lines=14,
                                   interactive=False, **COPY_BTN)

                        if script.get("characters"):
                            gr.Markdown(
                                "### 👤 角色定妆照\n"
                                "所有镜头共用，保证人物一致。定妆照会作为 Ref2VA 参考图传给 H3，层 2 按英文外貌自动生成；"
                                "不满意可重新生成，或上传真人模特照替换。"
                            )
                            source_label = {"generated": "AI 生成", "draft": "草图", "upload": "上传"}
                            for c in script["characters"]:
                                cid = c["id"]
                                path = c.get("image_path", "")
                                done = _ready(path)
                                used = "、".join(f"镜头 {s}" for s in shots_with_character(sb, cid)) or "无"
                                with gr.Group(elem_classes="asset-card"), gr.Row():
                                    with gr.Column(scale=1, min_width=160):
                                        img = gr.Image(  # interactive=True：已有定妆照时也要能拖图替换
                                            value=path if done else None, type="filepath", sources=["upload"],
                                            interactive=True,
                                            label=f"{cid} 定妆照" + (f"（{source_label.get(c.get('image_source'), '')}）" if done else "（层 2 生成）"),
                                            height=260,
                                        )
                                        regen = gr.Button("🔄 重新生成定妆照", size="sm")
                                    with gr.Column(scale=2):
                                        chips = [ui.chip("定妆照已就绪", "ok") if done else ui.chip("待生成"),
                                                 ui.chip(c.get("role") or "角色", "accent"), ui.chip(f"出场：{used}")]
                                        gr.HTML(ui.card_head(cid, c.get("name", ""), chips,
                                                             notes=[c.get("appearance_zh", "")]))
                                        gr.Textbox(label="英文外貌锚点 (生成定妆照 + 原样写进每条 H3 提示词)",
                                                   value=c.get("appearance_en", ""), lines=3,
                                                   interactive=False, **COPY_BTN)
                                # cid 用默认参数绑定，避免闭包在循环中取到最后一个值
                                img.upload(fn=lambda f, cid=cid: save_character_image(cid, f),
                                           inputs=[img], outputs=[status_text, sb_state])
                                img.clear(fn=lambda cid=cid: save_character_image(cid, None),
                                          inputs=[], outputs=[status_text, sb_state])
                                regen.click(fn=lambda cid=cid: regenerate_portrait(cid),
                                            inputs=[], outputs=[status_text, sb_state],
                                            concurrency_id="pipeline", concurrency_limit=1)
                        if script.get("scenes"):
                            gr.Markdown("### 🏠 场景设定")
                            for s in script["scenes"]:
                                gr.Markdown(f"**{s['id']} · {s.get('name', '')}**：{s.get('description_en', '')}")

                with gr.Tab("🎞️ 分镜脚本"):
                    @gr.render(inputs=[sb_state])
                    def render_shot_cards(sb):
                        shots = (sb or {}).get("shots", [])
                        if not shots:
                            gr.Markdown("在左侧点「1️⃣ 生成分镜脚本」\n\n生成后这里会逐镜列出口播、构图、H3 提示词和参考图。",
                                        elem_classes="empty-state")
                            return

                        script = (sb or {}).get("script") or {}
                        char_names = {c["id"]: c.get("name", c["id"]) for c in script.get("characters", [])}
                        scene_names = {s["id"]: s.get("name", s["id"]) for s in script.get("scenes", [])}

                        # 方便一次性复制到 ComfyUI 逐镜粘贴
                        all_prompts = "\n\n".join(
                            f"===== 镜头 {s.get('id')} ({s.get('mode')}, {s.get('duration_sec')}s) =====\n{read_h3_prompt(s)}"
                            for s in shots
                        )
                        with gr.Accordion("📋 全部 H3 提示词 (一次复制)", open=False):
                            gr.Textbox(value=all_prompts, lines=10, interactive=False,
                                       show_label=False, **COPY_BTN)

                        for shot in shots:
                            render_shot_card(sb, shot, char_names, scene_names)

                with gr.Tab("🍿 成片"):
                    final_video = gr.Video(label="拼接合成的最终广告片")

        # ════════════════════════════ 主图 / 详情页 ════════════════════════════
        with gr.Tab("🖼️ 主图 / 详情页"), gr.Row():
            with gr.Column(scale=2, min_width=340):
                with gr.Accordion("① 规格设置", open=True):
                    ecom_platform = gr.Dropdown(choices=list(ecom.ECOM_PLATFORMS), value=ecom.DEFAULT_ECOM_PLATFORM,
                                                label="电商平台")
                    with gr.Row():
                        ecom_main_count = gr.Radio(choices=list(ecom.MAIN_COUNTS), value=ecom.MAIN_COUNTS[0],
                                                   label="主图张数")
                        ecom_main_ratio = gr.Radio(choices=list(ecom.MAIN_RATIOS), value=ecom.MAIN_RATIOS[0],
                                                   label="主图比例")
                    gr.Markdown("主图 1:1 = 1440×1440，3:4 = 1440×1920；详情页固定 15 张 3:4。", elem_classes="hint")
                    ecom_use_chars = gr.Checkbox(label="允许定妆照里的模特出镜（与视频同一人）", value=True)
                    gr.Markdown("📸 产品实拍图在「📦 产品信息」页上传（与带货视频共用）。", elem_classes="hint")

                with gr.Accordion("② 策划与生成", open=True):
                    ecom_review = gr.Checkbox(
                        label="策划后让模型按评分标准自检并修改一轮（多一次 LLM 调用）", value=False)
                    btn_ecom_plan = gr.Button("① 策划图文 (LLM 出每张图的文案和画面)", variant="primary")
                    btn_ecom_reassign = gr.Button("🔁 重新分配素材（补拍 / 改了照片类型后用，文案不变）", size="sm")
                    ecom_text_mode = gr.Radio(
                        choices=[(label, key) for key, label in ecom.TEXT_MODES.items()],
                        value=ecom.DEFAULT_TEXT_MODE, label="文字上图方式",
                        info="程序叠字：中文不会错，改字只重排不花钱，由 HTML 模板排版（角标、价格标签、参数表）。"
                             "AI 直接写字：设计感更强，但可能错字/漏字，改字要重新生成画面。"
                             "切换后点生成，有文字的图会按新方式重新生成。",
                    )
                    ecom_force = gr.Checkbox(label="全部重新生成画面（默认只生成还没有底图的）", value=False)
                    ecom_fidelity = gr.Checkbox(
                        label="生成后让视觉模型核对主图里的产品（包装文字 / logo / 配色是否与实拍图一致，每张多一次调用）",
                        value=False)
                    btn_ecom_gen = gr.Button("② 生成全部图片", variant="primary")

                with gr.Accordion("③ 导出", open=True):
                    btn_ecom_export = gr.Button("📦 打包下载全部成品", variant="secondary")
                    ecom_zip = gr.File(label="下载", interactive=False, height=96)
                    btn_ecom_clear = gr.Button("🧹 清空，开始新产品", variant="stop", size="sm")
                    gr.Markdown("清空本页策划、底图、成品和产品图；已打包的 zip 保留在 "
                                "`pipeline/output/ecom/exports/`。「📦 产品信息」页的文字不清，请手动改。",
                                elem_classes="hint")

                gr.Markdown(
                    "💡 主图拉点击率（一图一卖点），详情页拉转化率（痛点 → 卖点 → 证据 → 场景 → 细节 → 规格售后）。"
                    "文案中的【占位】需替换为真实信息；上架前请核对包装文字和 logo。", elem_classes="tip-card"
                )

            with gr.Column(scale=3):
                ecom_state = gr.State(ecom.load_plan)

                @gr.render(inputs=[ecom_state])
                def render_ecom(plan):
                    plan = plan or {}
                    if not plan.get("main_images"):
                        gr.Markdown("在左侧设置规格并点「① 策划图文」\n\n这里会列出每张主图和详情页的文案，可逐张修改后再生成。",
                                    elem_classes="empty-state")
                        return
                    meta = plan.get("meta", {})
                    ratio = meta.get("main_ratio", "1:1")
                    lib = al.load_library()
                    mw, mh = ecom.TARGET_SIZES[ratio]
                    dw, dh = ecom.TARGET_SIZES[ecom.DETAIL_RATIO]
                    with gr.Tabs():
                        with gr.Tab(f"主图 {len(plan['main_images'])} 张 · {ratio}（{mw}×{mh}）"):
                            gr.Markdown(f"{meta.get('platform', '')} · 首图决定点击率", elem_classes="hint")
                            for it in plan["main_images"]:
                                render_ecom_card("main", it, meta.get("playbook"), lib, meta.get("created", ""))
                        with gr.Tab(f"详情页 {len(plan['detail_sections'])} 张 · 3:4（{dw}×{dh}）"):
                            gr.Markdown("按序号逐张上传", elem_classes="hint")
                            for it in plan["detail_sections"]:
                                render_ecom_card("detail", it, meta.get("playbook"), lib, meta.get("created", ""))

    story_template.change(fn=template_info, inputs=[story_template], outputs=[template_desc])
    story_template.change(fn=lambda t: gr.update(visible=t == CUSTOM_TEMPLATE), inputs=[story_template],
                          outputs=[custom_beats])
    # 平台和模板互相影响：换平台时把模板切到推荐模板；手动换成不适合的模板时提示
    platform.change(fn=on_platform_change, inputs=[platform, story_template],
                    outputs=[platform_desc, story_template])
    story_template.change(fn=platform_info, inputs=[platform, story_template], outputs=[platform_desc])
    for box in (prod_category, prod_name, prod_desc):
        box.blur(fn=lambda *v: playbook_info(collect_brief(v, save=False)), inputs=brief_inputs,
                 outputs=[prod_playbook_md])
    prod_playbook.change(fn=lambda *v: playbook_info(collect_brief(v, save=False)), inputs=brief_inputs,
                         outputs=[prod_playbook_md])
    # 自定义类目手册：选中自定义手册时把内容填进编辑框
    prod_playbook.change(fn=lambda k: cp.custom_to_text(k) if cp.is_custom(k) else gr.skip(),
                         inputs=[prod_playbook], outputs=[playbook_editor])

    # 所有步骤都读写同一份 storyboard / 输出目录，共用一个并发队列串行执行，避免互相覆盖
    queue_kw = dict(concurrency_id="pipeline", concurrency_limit=1)

    btn_check.click(fn=run_brief_check, inputs=brief_inputs, outputs=[status_text])
    btn_draft_pb.click(fn=run_draft_playbook, inputs=brief_inputs, outputs=[status_text, playbook_editor],
                       **queue_kw)
    btn_save_pb.click(fn=run_save_playbook, inputs=[playbook_editor], outputs=[status_text, prod_playbook]) \
        .then(fn=lambda *v: playbook_info(collect_brief(v, save=False)), inputs=brief_inputs,
              outputs=[prod_playbook_md])
    btn_del_pb.click(fn=run_delete_playbook, inputs=[prod_playbook], outputs=[status_text, prod_playbook, playbook_editor])
    btn_analyze.click(fn=run_analyze, inputs=brief_inputs, outputs=[status_text] + analysis_outputs, **queue_kw)
    btn_layer1.click(
        fn=run_layer1,
        inputs=[story_template, platform, agent_skill, include_skill_refs, video_review, custom_beats,
                video_concept] + brief_inputs,
        outputs=[status_text, sb_state],
        **queue_kw,
    )
    btn_layer2.click(fn=run_layer2, inputs=[is_draft], outputs=[status_text, sb_state], **queue_kw)
    btn_batch_run.click(fn=batch_render_with_diffusers, inputs=[force_all],
                        outputs=[status_text, final_video], **queue_kw)
    btn_layer4.click(fn=manual_concat, inputs=[], outputs=[status_text, final_video], **queue_kw)

    photo_upload.upload(fn=ecom_add_photos, inputs=[photo_upload, photo_auto_tag],
                        outputs=[status_text, photo_state, photo_upload, ecom_product_md], **queue_kw)
    btn_tag.click(fn=lambda: ecom_tag_photos(False), inputs=[],
                  outputs=[status_text, photo_state, ecom_product_md], **queue_kw)
    btn_retag.click(fn=lambda: ecom_tag_photos(True), inputs=[],
                    outputs=[status_text, photo_state, ecom_product_md], **queue_kw)
    btn_ecom_plan.click(fn=ecom_plan,
                        inputs=[ecom_platform, ecom_use_chars, ecom_main_count, ecom_main_ratio, ecom_review]
                        + brief_inputs,
                        outputs=[status_text, ecom_state], **queue_kw)
    btn_ecom_gen.click(fn=ecom_generate_all, inputs=[ecom_force, ecom_text_mode, ecom_fidelity],
                       outputs=[status_text, ecom_state], **queue_kw)
    btn_ecom_export.click(fn=ecom_export, inputs=[], outputs=[status_text, ecom_zip])
    btn_ecom_reassign.click(fn=ecom_reassign, inputs=[], outputs=[status_text, ecom_state], **queue_kw)
    btn_ecom_clear.click(fn=ecom_clear, inputs=[],
                         outputs=[status_text, ecom_state, ecom_zip, ecom_product_md, photo_state],
                         **queue_kw)

if __name__ == "__main__":
    demo.launch(server_port=7860, share=False, **LAUNCH_KW)
