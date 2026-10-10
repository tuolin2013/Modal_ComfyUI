import os
import re
import json
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
    STORY_TEMPLATES, DEFAULT_TEMPLATE, PLATFORMS, DEFAULT_PLATFORM,
    shot_references, duration_to_frames,
)
from layer2_visual_assets import generate_all_shots, quick_draft, generate_character_portrait
from layer3_h3_prompt import process_all_prompts
import ecom_images as ecom
import product_brief as brief_mod

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
# Gradio 6 移除了 show_copy_button（改为 buttons=["copy"]），theme 也从 Blocks 移到了 launch()
GRADIO_MAJOR = int(gr.__version__.split(".")[0])
COPY_BTN = {"buttons": ["copy"]} if GRADIO_MAJOR >= 6 else {"show_copy_button": True}
THEME = gr.themes.Soft()
BLOCKS_KW = {} if GRADIO_MAJOR >= 6 else {"theme": THEME}
LAUNCH_KW = {"theme": THEME} if GRADIO_MAJOR >= 6 else {}

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
    tpl = STORY_TEMPLATES.get(name)
    if not tpl:
        return ""
    beats = " → ".join(f"{b[0]}({b[1]})" for b in tpl["beats"])
    return f"{tpl['desc']}\n\n**{len(tpl['beats'])} 个镜头：** {beats}"


def platform_info(name: str) -> str:
    p = PLATFORMS.get(name)
    if not p:
        return ""
    return f"**画幅 {p['aspect_ratio']}**（基底图 {p['image_size']}）· {p['guide']}"


# ── 编剧技能 ──────────────────────────────────────────────────────────────────
# 非编剧类技能（由其他层在代码中固定使用），不出现在编剧下拉框里
NON_SCREENWRITING_SKILLS = {"h3-prompt-writing"}
# 注入 LLM 的技能文本总长度上限（字符），防止多选 + reference 撑爆上下文
SKILL_CHAR_BUDGET = 60_000

SKILL_MAP = {}  # label -> 技能目录 Path
skills_dir = PROJECT_ROOT / ".agents" / "skills"
if skills_dir.is_dir():
    for d in sorted(skills_dir.iterdir()):
        md_path = d / "SKILL.md"
        if not (d.is_dir() and md_path.exists()) or d.name in NON_SCREENWRITING_SKILLS:
            continue
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


def collect_brief(values) -> dict:
    """按 BRIEF_FIELDS 顺序把页面输入组装成 dict，并保存（刷新页面后恢复）"""
    brief = {k: (str(v).strip() if v is not None else "") for k, v in zip(BRIEF_FIELDS, values)}
    brief_mod.save_brief(brief)
    return brief


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
        issues = ecom.check_compliance("\n".join(result.values()))
        if issues:
            msg += f"\n⚠️ 广告法风险：{'、'.join(issues)}"
        return [msg] + [result[k] for k in brief_mod.ANALYSIS_FIELDS]
    except Exception as e:
        return [err(e)] + [gr.skip()] * len(brief_mod.ANALYSIS_FIELDS)


# ── 层 1：策划 ────────────────────────────────────────────────────────────────
def run_layer1(story_template, platform, agent_skills, include_skill_refs, *brief_values):
    brief = collect_brief(brief_values)
    product_name = brief["name"]
    usps = brief_mod.lines_of(brief["usp"])
    if not product_name or not usps:
        return "⚠️ 请填写商品名称，并点「分析卖点」或手动填写核心卖点", load_storyboard()

    skill_content, skill_skipped = load_skill_content(agent_skills, include_skill_refs)

    try:
        storyboard = generate_storyboard(
            product_name=product_name,
            usp=usps,
            brand_color=brief["brand_color"],
            story_template=story_template,
            target_platform=platform,
            agent_skill_content=skill_content,
            product_context=brief_mod.brief_context(brief),
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
        msg += (f"\n📸 剧本需要 {len(refs)} 张产品镜头图，执行「2️⃣ 生成定妆照 + 基底图」时会以顶部实拍图为参考自动生成"
                "（也可在「② 素材准备」逐张点「🤖 现在生成」先看效果）。")
    elif refs:
        msg += f"\n📸 剧本需要 {len(refs)} 张产品镜头图，请先在页面顶部上传产品实拍图，AI 以它为参考生成。"
    else:
        msg += "\n💡 本剧本没有真实产品入画的镜头，无需产品图。"
    # 口播会直接念出来，同样要过广告法初筛
    vo_issues = ecom.check_compliance("\n".join(str(s.get("voiceover") or "") for s in storyboard.get("shots", [])))
    if vo_issues:
        msg += f"\n⚠️ 口播广告法风险：{'、'.join(vo_issues)}（可在「剧本」页修改台词）"
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
    """以顶部产品实拍图为参考，按剧本 desc 生成 RefN 产品图；返回保存路径"""
    sb = sb if sb is not None else load_storyboard()
    ref = next((r for r in sb.get("product_refs", []) if r.get("id") == ref_id), None)
    if ref is None:
        raise ValueError(f"当前剧本不需要 {ref_id}，请重新生成分镜")
    slots = ecom.product_slots()
    if not slots:
        raise ValueError("请先在页面顶部上传产品实拍图")
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

    # 剧本需要、还没有的产品图：以顶部实拍图为参考自动生成（先于 layer2，layer2 会读写同一份 storyboard）
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
                   else "请先在页面顶部上传产品实拍图，再点「🤖 现在生成」。"))

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
    if force_all:
        clear_dir(VIDEO_DIR, "shot_*.mp4")

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
        progress((idx, len(order)), desc=f"渲染镜头 {sid} ({wf})")
        try:
            video_bytes = runners[wf].render_single_shot.remote(prompt, images or None, frames, seed)
        except Exception as e:
            failures.append(f"镜头 {sid}: {e}")
            continue
        video_of(sid).write_bytes(video_bytes)
    progress((len(order), len(order)), desc="渲染完成")

    # jobs 按分镜顺序构建，拼接也按此顺序，包含本轮渲染的和之前已有的镜头
    rendered = [video_of(j[0]) for j in jobs]

    warn = f"\n♻️ 复用已有视频 {skipped} 个镜头（勾选「全部重新渲染」可强制重渲）" if skipped else ""
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
    """说明当前生图实际用哪些产品图（本页没传时沿用视频页的）"""
    if ecom.get_product_images():
        return (f"已上传 {len(ecom.get_product_images())} 张（图 1 为主参考）。"
                "换图后，视频里已生成的产品图不会自动更新，可在「② 素材准备」点重新生成。")
    fallback = ecom_product_images()
    if fallback:
        return f"顶部未上传，主图/详情页沿用当前剧本里的 {len(fallback)} 张产品图。"
    return "⚠️ 还没有产品图，请至少上传 1 张，再生成剧本。"


def ecom_set_product(slot: int, file_path):
    """上传 / 替换 / 删除第 slot 个产品图位"""
    try:
        saved = ecom.set_product_image(slot, file_path)
        msg = (f"✅ 产品图 {slot} 已保存" if saved else f"🗑️ 产品图 {slot} 已删除") + \
              "。已生成的图不受影响，需要时点「重新生成画面」。"
        return msg, ecom_product_note()
    except Exception as e:
        return err(e), ecom_product_note()


def _plan_issue_summary(plan: dict) -> str:
    lines = []
    for it in plan.get("main_images", []) + plan.get("detail_sections", []):
        issues = ecom.item_issues(it)
        if issues:
            lines.append(f"  {it['id']}：{'、'.join(issues)}")
    return ("\n⚠️ 文案待处理（禁用词/占位）：\n" + "\n".join(lines)) if lines else "\n✅ 文案未发现禁用词"


def ecom_plan(platform, use_chars, main_count, main_ratio, *brief_values):
    try:
        brief = collect_brief(brief_values)
        usp = brief_mod.lines_of(brief["usp"])
        if not brief["name"]:
            return "⚠️ 请先在顶部填写商品名称", gr.skip()
        if not usp:
            return "⚠️ 请先点「分析卖点」或手动填写核心卖点", gr.skip()
        chars = list(ecom_portraits().values()) if use_chars else []
        plan = ecom.generate_ecom_plan(brief["name"], usp, brief["brand_color"], platform, chars,
                                       main_count=int(main_count), main_ratio=main_ratio,
                                       product_context=brief_mod.brief_context(brief))
        ecom.reset_outputs()
        ecom.save_plan(plan)
        msg = (f"✅ 已策划 {len(plan['main_images'])} 张 {main_ratio} 主图 + "
               f"{len(plan['detail_sections'])} 张 3:4 详情页（{platform}）。"
               f"\n请在右侧核对、修改文案，再点「生成全部图片」。" + _plan_issue_summary(plan))
        return msg, plan
    except Exception as e:
        return err(e), gr.skip()


def _ecom_items(plan: dict):
    return [("main", it) for it in plan.get("main_images", [])] + \
           [("detail", it) for it in plan.get("detail_sections", [])]


def ecom_generate_all(force=False, text_mode=ecom.DEFAULT_TEXT_MODE, progress=gr.Progress()):
    """
    逐张生成（串行）。底图可复用的只重排文字、不再调用接口；失败后再点一次只补失败的。
    切换写字方式后，有文字的图会重新生成（底图里有没有字不同），白底图等无字图照常复用。
    生成器：每完成一张就把最新 plan 推给页面，右侧卡片逐张显示，不用等整套跑完。
    """
    plan = ecom.load_plan()
    if not plan.get("main_images"):
        yield "⚠️ 请先点「策划图文」", gr.skip()
        return
    products = ecom_product_images()
    if not products:
        yield "⚠️ 请先在页面顶部上传产品实拍图", gr.skip()
        return
    portraits = ecom_portraits()

    items = _ecom_items(plan)
    failed, generated, reused = [], 0, 0
    service_fails, aborted = 0, ""  # 连续服务端故障计数
    for i, (kind, it) in enumerate(items):
        progress(i / len(items), desc=f"{it['id']} {it['role'][:12]}")
        if i:
            # 推送上一张的结果：plan 每张都已写盘，传重新读出的新对象，确保页面检测到变化并重绘
            yield (f"⏳ 进行中 {i}/{len(items)}：新生成 {generated}，复用 {reused}，失败 {len(failed)}",
                   ecom.load_plan())
        try:
            if not force and not ecom.needs_regen(it, text_mode):
                ecom.compose_item(plan, kind, it)
                reused += 1
            else:
                ecom.render_item(plan, kind, it, products, portraits, text_mode)
                generated += 1
                service_fails = 0
        except ecom.ServiceError as e:
            print(f"[电商图] {it['id']} 失败：{e}")
            failed.append(f"{it['id']}：{e}")
            service_fails += 1
            if service_fails >= 2:
                # 接口连续两张都是 5xx / 连不上，后面的图大概率也会失败，停下来省时间
                aborted = (f"\n\n⛔ 图像接口连续 {service_fails} 张服务端故障，已暂停，剩余 "
                           f"{len(items) - i - 1} 张未尝试。这是中转平台/上游的问题（不是提示词或图片的问题），"
                           "稍后再点「生成全部图片」会从失败处继续。")
                ecom.save_plan(plan)
                break
        except Exception as e:
            print(traceback.format_exc())
            failed.append(f"{it['id']}：{str(e)[:300]}")
        ecom.save_plan(plan)  # 每张写回，中途失败不丢已生成的

    msg = f"共 {len(items)} 张：新生成 {generated} 张，复用底图重排 {reused} 张。" + aborted
    check = "上架前请逐张核对产品包装文字/logo 是否与实物一致"
    if text_mode == "ai":
        check += "，并逐字核对 AI 写的中文（错字请改文案后点「重新生成画面」，或切换为程序叠字）"
    if failed:
        msg = f"❌ {len(failed)} 张失败：\n" + "\n".join(failed) + "\n\n" + msg + "\n再点一次「生成全部图片」只补失败的。"
    else:
        msg = "✅ " + msg + f"\n{check}。"
    yield msg + _plan_issue_summary(plan), ecom.load_plan()


def ecom_save_item(kind, item_id, regen, headline, subline, badges, scene, text_mode=ecom.DEFAULT_TEXT_MODE):
    """
    保存某张图的文案。regen=False：程序叠字的图只重排文字（免费）；AI 写字的图文字在画面里，只保存文案。
    regen=True：按当前写字方式重新生成画面。
    """
    try:
        plan = ecom.load_plan()
        it = ecom.find_item(plan, kind, item_id)
        if not it:
            return "⚠️ 找不到这张图，请重新策划", gr.skip()
        lines = [l.strip() for l in (badges or "").splitlines() if l.strip()]
        if kind == "main":
            it.update(headline=headline.strip(), subline=subline.strip(), badges=lines[:3])
        else:
            it.update(title=headline.strip(), body=subline.strip(), bullets=lines[:4])
        it["scene_prompt_en"] = (scene or "").strip()

        if regen:
            products = ecom_product_images()
            if not products:
                return "⚠️ 请先上传产品图", plan
            ecom.render_item(plan, kind, it, products, ecom_portraits(), text_mode)
            msg = f"✅ {item_id} 已重新生成（{ecom.TEXT_MODES[it['base_mode']]}）"
        elif _ready(it.get("base_path")) and it.get("base_mode") == "ai" and ecom.has_text(it):
            msg = f"✅ {item_id} 文案已保存。这张图的文字是 AI 画在画面里的，点「重新生成画面」后才会更新"
        elif _ready(it.get("base_path")):
            ecom.compose_item(plan, kind, it)
            msg = f"✅ {item_id} 文案已更新并重新排版"
        else:
            msg = f"✅ {item_id} 文案已保存（还没有底图，生成后生效）"

        ecom.save_plan(plan)
        issues = ecom.item_issues(it)
        return msg + (f"\n⚠️ {'、'.join(issues)}" if issues else ""), plan
    except Exception as e:
        return err(e), gr.skip()


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


def ecom_clear():
    """清空本页的策划、底图、成品和产品图（顶部产品信息不动，已打包的 zip 保留）"""
    try:
        ecom.clear_all()
        n = ecom.MAX_PRODUCT_REFS
        return ("🧹 已清空主图/详情页的策划、底图、成品和顶部产品实拍图（已打包的 zip 保留在 exports/；"
                "已生成的视频剧本自带一份产品图，不受影响）。"
                "\n请在顶部改成新产品的信息，上传新产品图后重新策划。"), {}, None, ecom_product_note(), \
            *([None] * n), *([""] * n)
    except Exception as e:
        return (err(e), gr.skip(), gr.skip(), gr.skip(), *([gr.skip()] * (ecom.MAX_PRODUCT_REFS * 2)))


def render_ecom_card(kind: str, it: dict):
    """在 @gr.render 上下文中绘制单张主图/详情页卡片"""
    is_main = kind == "main"
    issues = ecom.item_issues(it)
    final = it.get("final_path") if _ready(it.get("final_path")) else None
    with gr.Group(), gr.Row():
        with gr.Column(scale=1, min_width=200):
            gr.Image(value=final, label=f"{it['id']} 成品" if final else f"{it['id']}（未生成）",
                     interactive=False, height=300)
        with gr.Column(scale=2):
            people = "、".join(it.get("use_characters") or []) or "无"
            mode = ""
            if final and ecom.has_text(it):
                mode = " · " + ("AI 写字，请逐字核对" if it.get("base_mode") == "ai" else "程序叠字")
            gr.Markdown(f"**{it['id']} · {it['role']}** · 出镜角色：{people}{mode}"
                        + (f"\n\n⚠️ {'、'.join(issues)}" if issues else ""))
            if it.get("white_bg"):
                gr.Markdown("白底图不加文字。")
                head = gr.Textbox(value="", visible=False)
                sub = gr.Textbox(value="", visible=False)
                badges = gr.Textbox(value="", visible=False)
            else:
                head = gr.Textbox(label="主标题" if is_main else "标题",
                                  value=it.get("headline" if is_main else "title", ""))
                sub = gr.Textbox(label="副标题" if is_main else "正文",
                                 value=it.get("subline" if is_main else "body", ""), lines=1 if is_main else 2)
                badges = gr.Textbox(label="角标（每行一个，最多 3 个）" if is_main else "要点（每行一条，最多 4 条）",
                                    value="\n".join(it.get("badges" if is_main else "bullets", [])), lines=2)
            with gr.Accordion("画面描述（英文，改后点重新生成画面）", open=False):
                scene = gr.Textbox(value=it.get("scene_prompt_en", ""), lines=4, show_label=False)
            with gr.Row():
                btn_text = gr.Button("💾 保存文字并重排", size="sm")
                btn_regen = gr.Button("🔄 重新生成画面", size="sm")
    iid = it["id"]
    # 写字方式取页面上的全局选项，「重新生成画面」按当前选择出图
    fields = [head, sub, badges, scene, ecom_text_mode]
    # kind / iid 用默认参数绑定，避免闭包在循环中取到最后一个值
    btn_text.click(fn=lambda *v, k=kind, i=iid: ecom_save_item(k, i, False, *v),
                   inputs=fields, outputs=[status_text, ecom_state], concurrency_id="pipeline", concurrency_limit=1)
    btn_regen.click(fn=lambda *v, k=kind, i=iid: ecom_save_item(k, i, True, *v),
                    inputs=fields, outputs=[status_text, ecom_state], concurrency_id="pipeline", concurrency_limit=1)


# ── UI ────────────────────────────────────────────────────────────────────────
def render_shot_card(sb, shot, char_names, scene_names):
    """在 @gr.render 上下文中绘制单个镜头卡片"""
    mode = str(shot.get("mode", "")).upper()
    has_h3 = prompt_file_for(shot).exists()
    who = "、".join(char_names.get(c, c) for c in shot.get("characters", [])) or "无人物"
    where = scene_names.get(shot.get("scene"), "—")
    with gr.Group():
        with gr.Row():
            with gr.Column(scale=1):
                gr.Markdown(
                    f"#### 🎬 镜头 {shot.get('id')} ({shot.get('duration_sec')}s) | 模式: `{shot.get('mode')}` | 节拍: `{shot.get('emotion_beat')}`"
                    f"\n\n👤 {who} · 🏠 {where}"
                )
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
    gr.Markdown("# 🎬 AI 带货内容全链路流水线")

    # 保存完整 storyboard（shots + product_refs + meta），刷新页面时从磁盘恢复
    sb_state = gr.State(load_storyboard)

    # ── 顶部：产品信息 + 卖点分析（视频和图文共用）+ 系统状态 ────────────────────
    _brief = brief_mod.load_brief()
    with gr.Row():
        with gr.Column(scale=3):
            with gr.Accordion("📦 产品信息与卖点（视频脚本和主图/详情页共用）", open=True):
                with gr.Row():
                    with gr.Column(scale=1, min_width=300):
                        gr.Markdown("**① 填写产品信息**（越具体，卖点分析越准）")
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
                        btn_analyze = gr.Button("🔍 分析卖点（再点一次即重新分析）", variant="primary")
                    with gr.Column(scale=1, min_width=300):
                        gr.Markdown("**② 卖点分析**（LLM 生成，可直接修改；后续剧本和图文都以这里为准）")
                        prod_usp = gr.Textbox(label="核心卖点（每行一个，按说服力排序）", value=_brief["usp"], lines=4)
                        prod_pain = gr.Textbox(label="用户痛点（每行一个）", value=_brief["pain_points"], lines=2)
                        prod_diff = gr.Textbox(label="差异化", value=_brief["differentiation"], lines=1)
                        prod_persona = gr.Textbox(label="目标人群画像", value=_brief["persona"], lines=1)
                        prod_scenes = gr.Textbox(label="使用场景（每行一个）", value=_brief["scenes"], lines=2)

            with gr.Accordion("📸 产品实拍图（视频剧本和主图/详情页共用）", open=True):
                gr.Markdown(f"最多 {ecom.MAX_PRODUCT_REFS} 张不同角度（如正面 / 侧面 / 细节），包装文字清晰可辨。"
                            "AI 以这些图为参考生成剧本需要的产品镜头图和主图/详情页，保证与实物一致。"
                            "图下可写角度说明（帮助 AI 理解每张图）。点图片右上角 ✕ 删除，拖入新图即替换。")
                ecom_slots, ecom_labels = [], []
                with gr.Row():
                    for slot in range(1, ecom.MAX_PRODUCT_REFS + 1):
                        with gr.Column(min_width=120):
                            # gr.Image 按原比例完整显示（不像 Gallery 缩略图那样裁成方块）
                            ecom_slots.append(gr.Image(
                                value=(lambda s=slot: ecom.product_slot_path(s)),
                                label=f"产品图 {slot}" + ("（主）" if slot == 1 else ""),
                                type="filepath", sources=["upload"], height=220,
                            ))
                            ecom_labels.append(gr.Textbox(
                                value=(lambda s=slot: ecom.product_label(s)), show_label=False,
                                placeholder=["正面平视，白底", "侧面 45°", "细节特写 / 使用中"][slot - 1]
                                if slot <= 3 else "角度 / 内容说明",
                            ))
                ecom_product_md = gr.Markdown(ecom_product_note)
        with gr.Column(scale=2):
            status_text = gr.Textbox(label="系统状态", interactive=False, lines=12, max_lines=12, autoscroll=True)

    # 顺序必须与 BRIEF_FIELDS 一致（INPUT_FIELDS + ANALYSIS_FIELDS）
    brief_inputs = [prod_name, prod_category, prod_desc, prod_audience, prod_price, prod_endorse, prod_color,
                    prod_usp, prod_pain, prod_diff, prod_persona, prod_scenes]
    analysis_outputs = [prod_usp, prod_pain, prod_diff, prod_persona, prod_scenes]

    with gr.Tabs():
        # ════════════════════════════ 带货视频 ════════════════════════════
        with gr.Tab("🎬 带货视频"), gr.Row():
            with gr.Column(scale=2, min_width=340):
                with gr.Accordion("① 剧本策划", open=True):
                    story_template = gr.Dropdown(
                        choices=list(STORY_TEMPLATES), value=DEFAULT_TEMPLATE,
                        label="剧本模板 (决定镜头数量和每个镜头的叙事任务)",
                    )
                    template_desc = gr.Markdown(template_info(DEFAULT_TEMPLATE))

                    platform = gr.Dropdown(
                        choices=list(PLATFORMS), value=DEFAULT_PLATFORM,
                        label="目标平台 (决定画幅和剧本节奏)",
                    )
                    platform_desc = gr.Markdown(platform_info(DEFAULT_PLATFORM))

                    agent_skill = gr.Dropdown(
                        choices=skill_choices, value=[], multiselect=True,
                        label="编剧技能 (可多选，留空则不注入)",
                    )
                    include_skill_refs = gr.Checkbox(
                        label=f"同时注入技能参考资料 reference*.md (更贴合技法，但更耗 token，上限 {SKILL_CHAR_BUDGET} 字符)",
                        value=False,
                    )
                    btn_layer1 = gr.Button("1️⃣ 生成分镜脚本", variant="primary")

                with gr.Accordion("② 素材准备：产品图 + 定妆照 + 基底图", open=True):
                    @gr.render(inputs=[sb_state])
                    def render_ref_uploads(sb):
                        refs = (sb or {}).get("product_refs", [])
                        if not (sb or {}).get("shots"):
                            gr.Markdown("*生成分镜后，这里会列出剧本需要的真实产品图和拍摄要求。*")
                            return
                        if not refs:
                            gr.Markdown("💡 本剧本没有真实产品入画的镜头，无需上传产品图。")
                            return

                        gr.Markdown("**📸 剧本需要的产品镜头图**：以顶部实拍图为参考由 AI 生成，"
                                    "执行 2️⃣ 时自动补齐；不满意点「重新生成」。")
                        for ref in refs:
                            rid = ref["id"]
                            used = "、".join(f"镜头 {s}" for s in ref.get("used_in", []))
                            path = ref.get("image_path", "")
                            done = bool(path and os.path.exists(path))
                            with gr.Group():
                                gr.Markdown(f"**{'✅' if done else '⬜'} {rid}** · 用于 {used}\n\n{ref.get('desc', '')}")
                                # 只展示，不在这里上传；产品实拍图统一在页面顶部上传
                                gr.Image(value=path if done else None, interactive=False, height=180,
                                         label=rid if done else f"{rid}（待生成）")
                                btn_gen = gr.Button("🔄 重新生成" if done else "🤖 现在生成", size="sm")
                            # rid 用默认参数绑定，避免闭包在循环中取到最后一个值
                            btn_gen.click(fn=lambda rid=rid: run_generate_ref(rid), inputs=[],
                                          outputs=[status_text, sb_state],
                                          concurrency_id="pipeline", concurrency_limit=1)

                    gr.Markdown("👤 角色定妆照在右侧「剧本」页查看、重新生成或上传替换。")
                    is_draft = gr.Checkbox(label="草图模式 (nano-banana-2-lite，又快又便宜，固定 1:1)", value=False)
                    btn_layer2 = gr.Button("2️⃣ 生成定妆照 + 基底图 (完成后自动生成 H3 提示词)", variant="primary")

                with gr.Accordion("③ 渲染与合成", open=True):
                    gr.Markdown(
                        "**模式 B · Diffusers 全自动**：一键在云端渲染并拼接成片。"
                        "默认只渲染还没有视频的镜头（每镜约 6 分钟 GPU），失败后再点一次只补失败的。"
                    )
                    force_all = gr.Checkbox(label="全部重新渲染（忽略已有镜头视频）", value=False)
                    btn_batch_run = gr.Button("🚀 批量渲染并自动拼接", variant="primary")
                    gr.Markdown("**模式 A · ComfyUI 手动**：在右侧「分镜脚本」页逐镜复制提示词；渲染好的视频放进 "
                                "`pipeline/output/videos/` 后点下方拼接。")
                    btn_layer4 = gr.Button("4️⃣ 拼接本地已有的镜头视频 (FFmpeg)", variant="secondary")

            with gr.Column(scale=3), gr.Tabs():
                with gr.Tab("📜 剧本"):
                    @gr.render(inputs=[sb_state])
                    def render_script(sb):
                        script = (sb or {}).get("script") or {}
                        if not (sb or {}).get("shots"):
                            gr.Markdown("*生成分镜后，这里显示完整剧本、角色和场景设定。*")
                            return
                        if not script.get("full_text") and not script.get("characters"):
                            gr.Markdown("⚠️ 当前分镜是旧版本生成的，没有剧本数据，请重新生成分镜。")
                            return

                        gr.Markdown(f"## {script.get('title') or '未命名'}\n\n{script.get('logline', '')}")
                        gr.Textbox(label="完整剧本", value=script.get("full_text", ""), lines=14,
                                   interactive=False, **COPY_BTN)

                        if script.get("characters"):
                            gr.Markdown(
                                "### 👤 角色定妆照 (所有镜头共用，保证人物一致)\n"
                                "定妆照会作为 Ref2VA 参考图传给 H3。层 2 按英文外貌自动生成；"
                                "不满意可重新生成，或上传真人模特照替换。"
                            )
                            source_label = {"generated": "AI 生成", "draft": "草图", "upload": "上传"}
                            for c in script["characters"]:
                                cid = c["id"]
                                path = c.get("image_path", "")
                                done = _ready(path)
                                used = "、".join(f"镜头 {s}" for s in shots_with_character(sb, cid)) or "无"
                                with gr.Group(), gr.Row():
                                    with gr.Column(scale=1, min_width=160):
                                        img = gr.Image(
                                            value=path if done else None, type="filepath", sources=["upload"],
                                            label=f"{cid} 定妆照" + (f"（{source_label.get(c.get('image_source'), '')}）" if done else "（层 2 生成）"),
                                            height=260,
                                        )
                                        regen = gr.Button("🔄 重新生成定妆照", size="sm")
                                    with gr.Column(scale=2):
                                        gr.Markdown(
                                            f"**{'✅' if done else '⬜'} {cid} · {c.get('name', '')}**（{c.get('role', '')}）· 出场：{used}"
                                            f"\n\n{c.get('appearance_zh', '')}"
                                        )
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
                            gr.Markdown("*请先点击左侧「生成分镜脚本」，生成后将在此处自动装配各个镜头的资产卡片。*")
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
                    gr.Markdown("主图 1:1 = 1440×1440，3:4 = 1440×1920；详情页固定 15 张 3:4。")
                    ecom_use_chars = gr.Checkbox(label="允许定妆照里的模特出镜（与视频同一人）", value=True)

                gr.Markdown("📸 产品实拍图在页面顶部上传（与带货视频共用）。")

                with gr.Accordion("③ 策划与生成", open=True):
                    btn_ecom_plan = gr.Button("① 策划图文 (LLM 出每张图的文案和画面)", variant="primary")
                    ecom_text_mode = gr.Radio(
                        choices=[(label, key) for key, label in ecom.TEXT_MODES.items()],
                        value=ecom.DEFAULT_TEXT_MODE, label="文字上图方式",
                        info="程序叠字：中文不会错，改字只重排不花钱，版式较模板化。"
                             "AI 直接写字：设计感更强，但可能错字/漏字，改字要重新生成画面。"
                             "切换后点生成，有文字的图会按新方式重新生成。",
                    )
                    ecom_force = gr.Checkbox(label="全部重新生成画面（默认只生成还没有底图的）", value=False)
                    btn_ecom_gen = gr.Button("② 生成全部图片", variant="primary")

                with gr.Accordion("④ 导出", open=True):
                    btn_ecom_export = gr.Button("📦 打包下载全部成品", variant="secondary")
                    ecom_zip = gr.File(label="下载", interactive=False)
                    btn_ecom_clear = gr.Button("🧹 清空，开始新产品", variant="stop", size="sm")
                    gr.Markdown("清空本页策划、底图、成品和产品图；已打包的 zip 保留在 "
                                "`pipeline/output/ecom/exports/`。顶部产品信息不清，请手动改。")

                gr.Markdown(
                    "💡 主图拉点击率（一图一卖点），详情页拉转化率（痛点 → 卖点 → 证据 → 场景 → 细节 → 规格售后）。"
                    "文案中的【占位】需替换为真实信息；上架前请核对包装文字和 logo。"
                )

            with gr.Column(scale=3):
                ecom_state = gr.State(ecom.load_plan)

                @gr.render(inputs=[ecom_state])
                def render_ecom(plan):
                    plan = plan or {}
                    if not plan.get("main_images"):
                        gr.Markdown("*在左侧设置规格并点「策划图文」，这里会列出每张主图和详情页的文案，可逐张修改。*")
                        return
                    meta = plan.get("meta", {})
                    ratio = meta.get("main_ratio", "1:1")
                    mw, mh = ecom.TARGET_SIZES[ratio]
                    dw, dh = ecom.TARGET_SIZES[ecom.DETAIL_RATIO]
                    with gr.Tabs():
                        with gr.Tab(f"主图 {len(plan['main_images'])} 张 · {ratio}（{mw}×{mh}）"):
                            gr.Markdown(f"{meta.get('platform', '')} · 首图决定点击率")
                            for it in plan["main_images"]:
                                render_ecom_card("main", it)
                        with gr.Tab(f"详情页 {len(plan['detail_sections'])} 张 · 3:4（{dw}×{dh}）"):
                            gr.Markdown("按序号逐张上传")
                            for it in plan["detail_sections"]:
                                render_ecom_card("detail", it)

    story_template.change(fn=template_info, inputs=[story_template], outputs=[template_desc])
    platform.change(fn=platform_info, inputs=[platform], outputs=[platform_desc])

    # 所有步骤都读写同一份 storyboard / 输出目录，共用一个并发队列串行执行，避免互相覆盖
    queue_kw = dict(concurrency_id="pipeline", concurrency_limit=1)

    btn_analyze.click(fn=run_analyze, inputs=brief_inputs, outputs=[status_text] + analysis_outputs, **queue_kw)
    btn_layer1.click(
        fn=run_layer1,
        inputs=[story_template, platform, agent_skill, include_skill_refs] + brief_inputs,
        outputs=[status_text, sb_state],
        **queue_kw,
    )
    btn_layer2.click(fn=run_layer2, inputs=[is_draft], outputs=[status_text, sb_state], **queue_kw)
    btn_batch_run.click(fn=batch_render_with_diffusers, inputs=[force_all],
                        outputs=[status_text, final_video], **queue_kw)
    btn_layer4.click(fn=manual_concat, inputs=[], outputs=[status_text, final_video], **queue_kw)

    for slot, img in enumerate(ecom_slots, 1):
        # slot 用默认参数绑定，避免闭包在循环中取到最后一个值
        img.upload(fn=lambda f, s=slot: ecom_set_product(s, f), inputs=[img],
                   outputs=[status_text, ecom_product_md])
        img.clear(fn=lambda s=slot: ecom_set_product(s, None), inputs=[],
                  outputs=[status_text, ecom_product_md])
    for slot, box in enumerate(ecom_labels, 1):
        box.blur(fn=lambda t, s=slot: ecom.set_product_label(s, t), inputs=[box], outputs=[])
    btn_ecom_plan.click(fn=ecom_plan,
                        inputs=[ecom_platform, ecom_use_chars, ecom_main_count, ecom_main_ratio] + brief_inputs,
                        outputs=[status_text, ecom_state], **queue_kw)
    btn_ecom_gen.click(fn=ecom_generate_all, inputs=[ecom_force, ecom_text_mode],
                       outputs=[status_text, ecom_state], **queue_kw)
    btn_ecom_export.click(fn=ecom_export, inputs=[], outputs=[status_text, ecom_zip])
    btn_ecom_clear.click(fn=ecom_clear, inputs=[],
                         outputs=[status_text, ecom_state, ecom_zip, ecom_product_md] + ecom_slots + ecom_labels,
                         **queue_kw)

if __name__ == "__main__":
    demo.launch(server_port=7860, share=False, **LAUNCH_KW)
