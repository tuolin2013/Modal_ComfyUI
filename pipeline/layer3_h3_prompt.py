"""
层 3 · H3 提示词层
读取层 2 处理后的 storyboard.json（包含 base_url 和图片路径），
利用 h3-prompt-writing 技能指导 right.codes LLM 生成严谨的 H3 提示词结构。
"""

import json
import os
import re
import time
import requests
from pathlib import Path
from dotenv import load_dotenv

from layer1_planning import shot_references, duration_to_frames, RENDER_FPS, LLM_TIMEOUT, LLM_RETRIES

load_dotenv(Path(__file__).parent / ".env")

RIGHT_CODES_BASE_URL = "https://www.rightapi.ai/v1"
RIGHT_CODES_LLM_KEY = os.environ.get("RIGHT_CODES_LLM_KEY", "")
PLANNING_MODEL = os.environ.get("PLANNING_MODEL", "claude-opus-5-5")


# 技能文档路径
SKILL_DIR = Path(__file__).parent.parent / ".agents" / "skills" / "h3-prompt-writing" / "references"
BASE_GUIDE_PATH = SKILL_DIR / "base-en.txt"
REF_GUIDE_PATH = SKILL_DIR / "ref-en.txt"

def load_guide(path: Path) -> str:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return ""

def build_continuity_context(storyboard: dict, idx: int) -> str:
    """
    为第 idx 个镜头组装一致性上下文：出场角色设定、场景设定、前后镜头衔接。
    每个镜头是独立生成的视频，H3 之间没有记忆，只能靠每条提示词里重复同一份设定文字来对齐。
    """
    script = storyboard.get("script") or {}
    shots = storyboard.get("shots", [])
    shot = shots[idx]
    chars = {c["id"]: c for c in script.get("characters", []) if isinstance(c, dict) and c.get("id")}
    scenes = {s["id"]: s for s in script.get("scenes", []) if isinstance(s, dict) and s.get("id")}

    lines = []
    shot_chars = [chars[c] for c in shot.get("characters", []) if c in chars]
    if shot_chars:
        lines.append("Characters in this shot (copy these appearance descriptions VERBATIM, do not paraphrase or add new traits):")
        for c in shot_chars:
            lines.append(f"- {c.get('name', c['id'])} ({c.get('role', '')}): {c.get('appearance_en', '')}")
    scene = scenes.get(shot.get("scene"))
    if scene:
        lines.append(f"Scene (keep location, lighting and color palette identical across shots in this scene): "
                     f"{scene.get('name', '')}: {scene.get('description_en', '')}")

    def brief(s):
        return f"[{s.get('emotion_beat', '')}] {s.get('visual_intent', '')} | camera: {s.get('camera', '')}"

    if idx > 0:
        lines.append(f"Previous shot (this shot cuts from it; match screen direction, character positions and lighting): {brief(shots[idx - 1])}")
    if idx < len(shots) - 1:
        lines.append(f"Next shot (end this shot in a state that cuts naturally into it): {brief(shots[idx + 1])}")
    return "\n".join(lines)


def build_reference_mapping(references: list[dict]) -> str:
    """把 shot_references() 的结果写成 <Picture N> 对照表，顺序必须与渲染时传入参考图的顺序一致"""
    lines = []
    for r in references:
        if r["kind"] == "character":
            lines.append(
                f"- <{r['label']}>: character reference portrait of {r['name']} ({r['id']}). "
                f"Use it ONLY to lock this person's face, hairstyle, body and outfit; "
                f"ignore its plain grey background and standing pose."
            )
        else:
            lines.append(
                f"- <{r['label']}>: the real product ({r['id']}). Keep its shape, packaging text, logo, "
                f"colors and materials exactly as in the picture; it is a rigid object and must not deform."
            )
    return "\n".join(lines)


def generate_h3_prompt(shot: dict, context: str = "", references: list[dict] | None = None) -> str:
    """根据单个镜头信息（及一致性上下文、参考图对照表），生成符合 H3 规范的提示词"""
    mode = str(shot.get("mode", "T2VA"))
    is_ref = mode.upper() == "REF2VA"
    # 用实际渲染帧数换算的时长（如 5 秒 → 124 帧 ≈ 5.17 秒），让提示词时间轴与成片一致
    duration = rendered_seconds(shot.get("duration_sec", 5.0))
    voiceover = shot.get("voiceover", "")
    visual = shot.get("visual_intent", "")
    camera = shot.get("camera", "")
    img_prompt = shot.get("image_gen_prompt", "")
    
    print(f"[层3] 正在生成镜头 {shot['id']} ({mode}) 的 H3 提示词...")

    # 根据模式加载指南
    base_guide = load_guide(BASE_GUIDE_PATH)
    ref_guide = load_guide(REF_GUIDE_PATH) if is_ref else ""
    
    system_prompt = f"""
You are an expert H3 Video Prompt Writer. 
Follow the guidelines strictly to write a prompt for a video generation task.

Here is the Video Prompt Writing Guide:
<guide>
{base_guide}
</guide>
"""
    if is_ref:
        system_prompt += f"""
Additionally, since this is a Ref2VA task, follow the Full-Reference Mode Guide:
<ref_guide>
{ref_guide}
</ref_guide>
"""

    if is_ref and references:
        ref_section = (
            "Reference images (passed to the model in this exact order):\n"
            + build_reference_mapping(references)
            + "\n"
        )
        ref_rule = (
            "3. This is Ref2VA. Refer to every reference image above by its exact tag (<Picture 1>, <Picture 2> ...) "
            "and do not invent tags that are not listed. Bind each character's name to their <Picture N> tag the first "
            "time they appear, e.g. \"the woman from <Picture 1>\"."
        )
    else:
        ref_section = ""
        ref_rule = "3. This is a text-to-video shot. Do NOT use any <Picture N> tags."

    user_prompt = f"""
Please write the H3 prompt for the following shot.
Task Mode: {mode}
Target Video Duration: {duration:.2f} seconds

Shot Details:
- Visual Intent: {visual}
- Base Image Prompt (for context): {img_prompt}
- Camera Movement: {camera}
- Voiceover (Chinese): {voiceover}

{ref_section}
Continuity (this shot is one part of a multi-shot commercial; each shot is rendered separately):
{context or "- (no extra context)"}

Constraints:
1. Timing must fit exactly {duration:.2f} seconds; any timestamp you write must be within it.
2. Keep one continuous shot; do not describe cuts inside it.
{ref_rule}
4. The voiceover MUST be spoken by a character (S1) or off-screen voiceover (S1). Put the exact Chinese text inside <d>[Chinese] ...</d>, word for word.
5. Do NOT output markdown code blocks around the final prompt. Just output the raw prompt text directly.
6. Every character listed under Continuity must be described with the exact appearance text given, so all shots render the same person.
7. Do NOT ask for any on-screen text, subtitles, captions, price tags or logo animations; text is added in post-production.
8. Use exactly these sections, in this order: {", ".join(REF_SECTIONS if is_ref else BASE_SECTIONS)}.
"""

    headers = {
        "Authorization": f"Bearer {RIGHT_CODES_LLM_KEY}",
        "Content-Type": "application/json",
        "Connection": "close"
    }
    
    payload = {
        "model": PLANNING_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        # 按规范填写的任务，温度低一些更稳定
        "temperature": 0.4,
        "max_tokens": 2048
    }

    n_refs = len(references or []) if is_ref else 0
    last_err = ""
    for attempt in range(LLM_RETRIES):
        # 临时移除 NO_PROXY 以避免 requests 忽略系统代理
        old_no_proxy = os.environ.pop("NO_PROXY", None)
        old_no_proxy_lower = os.environ.pop("no_proxy", None)
        try:
            resp = requests.post(
                f"{RIGHT_CODES_BASE_URL.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
                timeout=LLM_TIMEOUT,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = type(e).__name__
            print(f"[层3] 镜头 {shot['id']} [{attempt + 1}/{LLM_RETRIES}] {last_err}，5 秒后重试...")
            time.sleep(5)
            continue
        finally:
            if old_no_proxy is not None:
                os.environ["NO_PROXY"] = old_no_proxy
            if old_no_proxy_lower is not None:
                os.environ["no_proxy"] = old_no_proxy_lower

        if resp.status_code >= 500:
            last_err = f"接口返回 {resp.status_code}"
            print(f"[层3] 镜头 {shot['id']} [{attempt + 1}/{LLM_RETRIES}] {last_err}，5 秒后重试...")
            time.sleep(5)
            continue
        if resp.status_code != 200:
            raise Exception(f"API请求失败 ({resp.status_code}): {resp.text[:300]}")

        text = _strip_code_fence(resp.json()["choices"][0]["message"]["content"] or "")
        problem = check_picture_tags(text, n_refs) or check_prompt_structure(text, is_ref, voiceover, duration)
        if not problem:
            return text
        last_err = problem
        print(f"[层3] 镜头 {shot['id']} [{attempt + 1}/{LLM_RETRIES}] 提示词不合格：{problem}，重新生成...")
        # 把问题告诉模型再写一次，比原样重试更容易改对
        payload["messages"] = payload["messages"][:2] + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": f"The prompt above fails a check: {problem}. "
                                        "Rewrite the complete prompt and fix this, keeping everything else."},
        ]

    raise Exception(f"镜头 {shot['id']} 的 H3 提示词生成失败：{last_err}")


def rendered_seconds(duration_sec) -> float:
    """与 app_gradio.duration_to_frames 一致的实际渲染时长（帧数 / 24）"""
    return duration_to_frames(duration_sec) / RENDER_FPS


# h3-prompt-writing 规范要求的段落（顺序见 references/base-en.txt、ref-en.txt）
BASE_SECTIONS = ("integrated_multimodal_description", "overall_soundscape", "non_diegetic_music")
REF_SECTIONS = ("subject_definitions", "summary", "retention_analysis", "detailed_description",
                "overall_soundscape", "non_diegetic_music")
# 要求模型在画面里写字的说法：H3 画出来的中文基本是乱码，字幕和花字留给后期。
# 「留出字幕区（space for subtitles）」是给后期留位置，不算
_ON_SCREEN_TEXT = re.compile(
    r"\b(?:on-screen|onscreen)\s+(?:text|caption|title)s?\b|\bsubtitles?\s+(?:read|reads|say|says|show|shows|appear)|"
    r"\bcaptions?\s+(?:read|say|show|appear)|\btext\s+overlay\b|\b(?:title|text)\s+(?:card|appears|reads)\b|"
    r"\bwords?\s+appear\b|\b(?:display|displays|showing|shows)\s+(?:the\s+)?(?:text|words|slogan)\b", re.I)
# H3 规范的时间写法：00:03.500（分:秒.毫秒）。只认这种格式，避免把画幅 9:16 当成时间点
_TIMESTAMP = re.compile(r"\b(\d{2}):(\d{2}\.\d{1,3})\b")


def _norm_speech(text: str) -> str:
    """比对口播时忽略空白和标点（模型常把「……」改成「...」）"""
    return re.sub(r"[\s\W_]+", "", str(text or ""))


def check_prompt_structure(text: str, is_ref: bool, voiceover: str = "", duration: float | None = None) -> str:
    """
    按 h3-prompt-writing 规范校验提示词结构，返回问题描述；合格返回空串。
    只查能确定的硬问题：段落缺失、口播没原样放进 <d>、要求画面写字、时间轴超出时长。
    """
    lowered = text.lower()
    sections = REF_SECTIONS if is_ref else BASE_SECTIONS
    missing = [s for s in sections if not re.search(rf"^\s*{s}\s*:", text, flags=re.M | re.I)]
    if missing:
        return f"缺少规范要求的段落：{', '.join(missing)}"
    positions = [re.search(rf"^\s*{s}\s*:", text, flags=re.M | re.I).start() for s in sections]
    if positions != sorted(positions):
        return f"段落顺序不对，应为 {' → '.join(sections)}"

    if _norm_speech(voiceover):
        spoken = "".join(re.findall(r"<d>(.*?)</d>", text, flags=re.S))
        spoken = re.sub(r"\[[^\]]*\]", "", spoken)          # 去掉 [Chinese] 语言标签
        if _norm_speech(voiceover) not in _norm_speech(spoken):
            return "口播没有原样放进 <d>[Chinese] ...</d>（H3 会念错或漏念台词）"

    m = _ON_SCREEN_TEXT.search(text)
    if m:
        return f"要求在画面里出现文字（「{m.group(0)}」），H3 画不出正确的中文，字幕留给后期"

    if duration:
        for mm, ss in _TIMESTAMP.findall(text):
            t = int(mm) * 60 + float(ss)
            if t > duration + 0.05:
                return f"时间点 {mm}:{ss} 超出镜头时长 {duration:.2f} 秒"
    if "```" in lowered:
        return "输出里带了 markdown 代码块"
    return ""


def check_picture_tags(text: str, n_refs: int) -> str:
    """
    校验提示词里的 <Picture N> 与实际传入的参考图数量一致，返回问题描述；合格返回空串。
    标签错位会让 H3 把人物/产品绑错，而且结果会被缓存复用，所以写盘前拦下。
    """
    if not text.strip():
        return "输出为空"
    used = {int(n) for n in re.findall(r"<Picture\s*(\d+)>", text)}
    if n_refs == 0:
        return f"T2VA 提示词不应出现 <Picture N>（出现了 {sorted(used)}）" if used else ""
    if not used:
        return "Ref2VA 提示词没有引用任何 <Picture N>"
    extra = sorted(n for n in used if n < 1 or n > n_refs)
    if extra:
        return f"引用了不存在的参考图 {extra}（只有 {n_refs} 张）"
    return ""


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```[a-zA-Z]*\s*\n?(.*?)\n?```$", text, re.S)
    return m.group(1).strip() if m else text

def process_all_prompts(storyboard_path: str = "./pipeline/output/storyboard.json", output_dir: str = "./pipeline/output/prompts"):
    os.makedirs(output_dir, exist_ok=True)
    
    with open(storyboard_path, "r", encoding="utf-8") as f:
        storyboard = json.load(f)
        
    failures = []
    for idx, shot in enumerate(storyboard["shots"]):
        try:
            sid = int(shot["id"])
        except (TypeError, ValueError, KeyError):
            sid = idx + 1  # 与 app_gradio.shot_id 不同时会对不上文件；新剧本的 id 已在层 1 重排为 1..N
        out_file = os.path.join(output_dir, f"shot_{sid:02d}_h3_prompt.txt")

        # 如果已经存在，可以选择跳过
        if os.path.exists(out_file):
            print(f"[层3] 镜头 {shot.get('id')} 的提示词已存在，跳过。")
            continue

        # 单个镜头失败不影响其它镜头；不合格的提示词不写盘（否则会被缓存复用）
        try:
            prompt_text = generate_h3_prompt(
                shot, build_continuity_context(storyboard, idx), shot_references(storyboard, shot)
            )
        except Exception as e:
            print(f"[层3] [FAIL] 镜头 {shot.get('id')}：{e}")
            failures.append(f"镜头 {shot.get('id')}：{str(e)[:200]}")
            continue
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(prompt_text)
        print(f"[层3] [OK] 镜头 {shot.get('id')} 提示词已保存 -> {out_file}\n")

    if failures:
        raise Exception("；".join(failures))
        
if __name__ == "__main__":
    process_all_prompts()
