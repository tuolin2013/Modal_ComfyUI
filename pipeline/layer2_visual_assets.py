"""
层 2 · 视觉资产层
使用 right.codes「draw」令牌 + 图像模型生成：
  1. 角色定妆照：每个角色一张，作为 Ref2VA 参考图锁定全片人物长相
  2. per-shot 静态基底图：用于确认构图（纯文生视频镜头）
Base URL：https://www.rightapi.ai/v1

可选图像模型（以平台模型列表和计价为准）：
  - gpt-image-2.5          高质量
  - gpt-image-2.5-sunburst gpt-image-2.5 变体
  - nano-banana            极速廉价，适合批量草稿
  - nano-banana-2          中等质量
  - nano-banana-2-lite     最便宜，草图模式使用

带货场景推荐：
  - 商品棚拍精图 → gpt-image-2.5
  - 快速草图校验 → nano-banana-2-lite
"""

import base64
import json
import os
import time
import urllib.request
from pathlib import Path

import requests

# 加载 pipeline/.env
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

# ── right.codes 接入配置 ─────────────────────────────────────────────────────
# 在 .env 文件中设置（参见 pipeline/.env.example）：
#   RIGHT_CODES_DRAW_KEY=sk-...    # 「draw」画图令牌
RIGHT_CODES_BASE_URL = "https://www.rightapi.ai/v1"
RIGHT_CODES_DRAW_KEY = os.environ.get("RIGHT_CODES_DRAW_KEY", "")

# 默认使用 gpt-image-2.5（高质量，兼容 OpenAI images.generate 接口）
DEFAULT_IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-2.5")

# 带货竖版 9:16：1008×1792（16 的倍数）；正常由分镜 meta.image_size（目标平台）决定，这里只是兜底
DEFAULT_SIZE = os.environ.get("IMAGE_SIZE", "1008x1792")  # 9:16 竖屏

# 角色定妆照固定竖版，便于容纳全身
PORTRAIT_SIZE = "1008x1792"
CHARACTER_DIR = "./pipeline/output/assets/characters"


# ── 每个镜头对应的生图风格前缀 ────────────────────────────────────────────────
# 产品特写（B-Roll）用棚拍质感；有人物或生活场景的镜头用生活纪实质感：
# 观众要觉得「这就是我的生活」，影棚光和干净背景会让人物镜头像广告片，失去代入感
STYLE_PREFIX = (
    "Product photography, professional studio shot, ultra-sharp, photorealistic, "
    "commercial advertising quality, 4K resolution, clean background, "
)
LIFESTYLE_PREFIX = (
    "Candid lifestyle photograph, shot on a phone or a 35mm lens, natural available light, "
    "real lived-in everyday setting with small authentic imperfections, "
    "any people look like ordinary relatable people with unposed natural expressions, "
    "photorealistic, no studio lighting, no seamless backdrop, "
)
# 这些节拍是纯产品特写，用棚拍质感；工艺、开箱、产地这类节拍是纪实画面，按生活场景处理
PRODUCT_BEATS = {"卖点展示", "信任背书", "对比实验"}


def style_prefix(shot: dict) -> str:
    """有人物出场、或节拍不是产品特写的镜头用生活纪实风格"""
    if shot.get("characters") or shot.get("emotion_beat") not in PRODUCT_BEATS:
        return LIFESTYLE_PREFIX
    return STYLE_PREFIX

# 定妆照：中性表情、正面全身、纯色背景、平光。参考图越"干净"，H3 越容易只提取人物身份而不带入多余元素
PORTRAIT_PREFIX = (
    "Character reference photo for a live-action commercial, full-body front view, standing straight, "
    "neutral relaxed expression, looking at camera, face clearly visible, plain light grey seamless studio "
    "background, soft even lighting, no props, no text, photorealistic, sharp focus, "
)

# 节拍 → 额外风格修饰词。分棚拍和生活两套，跟前缀一起选，避免「no studio」后面又接「clean studio」
# （新节拍名没有映射时不加修饰词）。都不用 medical / clinical 风格：画面暗示医疗功效同样有合规风险
STUDIO_BEAT_STYLE: dict[str, str] = {
    "卖点展示":   "clean white/gradient background, product centered, macro detail, ",
    "信任背书":   "clean bright studio, crisp detail close-up, trustworthy mood, ",
    "对比实验":   "side-by-side comparison setup, identical lighting on both sides, clean background, ",
}
LIFESTYLE_BEAT_STYLE: dict[str, str] = {
    "开场钩子":   "a relatable everyday moment caught mid-action, ",
    "使用场景":   "the person naturally using the product in their own space, ",
    "行动召唤":   "warm satisfied everyday moment, product naturally in frame, ",
    "开箱":       "top-down unboxing on a real desk, soft daylight, hands opening the package, ",
    "使用步骤":   "clear hands-on demonstration in a real home setting, close-up of hands and product, ",
    "产地溯源":   "documentary landscape photography, natural light, authentic rural setting, ",
    "工艺细节":   "documentary close-up of the craft process, natural texture, real workshop or field setting, ",
    # 产品特写节拍里有人出镜时的生活版本
    "卖点展示":   "the person holds the product close to the camera in natural light, product details clearly visible, ",
    "信任背书":   "close-up of hands examining the product in natural light, honest unpolished feel, ",
    "对比实验":   "a simple side-by-side comparison on a real desk or kitchen counter, same natural light on both sides, ",
}
# 旧名字：所有节拍的修饰词（棚拍优先），给只想查某个节拍有没有风格的调用方
EMOTION_BEAT_STYLE: dict[str, str] = {**LIFESTYLE_BEAT_STYLE, **STUDIO_BEAT_STYLE}

# 画幅 → 构图提示（程序加，不靠模型记得）
FRAMING = {"9:16": "vertical 9:16 composition, ", "16:9": "horizontal 16:9 composition, ",
           "1:1": "square 1:1 composition, ", "3:4": "vertical 3:4 composition, "}


def beat_style(shot: dict) -> str:
    beat = shot.get("emotion_beat", "")
    table = STUDIO_BEAT_STYLE if style_prefix(shot) == STYLE_PREFIX else LIFESTYLE_BEAT_STYLE
    return table.get(beat, "")


def compose_shot_prompt(shot: dict, storyboard: dict | None = None) -> str:
    """
    镜头底图的完整提示词 = 风格前缀 + 节拍修饰 + 画幅 + 场景设定原文 + 出场角色外貌原文 + 本镜的 image_gen_prompt。
    场景和角色由程序按 id 拼进去，不靠模型在每镜里抄写（模型常改写或省略，镜头之间就会漂）；
    image_gen_prompt 里已经原样包含的设定不重复拼接
    """
    storyboard = storyboard or {}
    script = storyboard.get("script") or {}
    own = str(shot.get("image_gen_prompt") or "").strip()
    parts = [style_prefix(shot), beat_style(shot),
             FRAMING.get((storyboard.get("meta") or {}).get("aspect_ratio"), "")]
    scenes = {s.get("id"): s for s in script.get("scenes") or [] if isinstance(s, dict)}
    scene = scenes.get(shot.get("scene"))
    desc = str((scene or {}).get("description_en") or "").strip().rstrip(".")
    if desc and desc not in own:
        parts.append(f"Setting: {desc}. ")
    chars = {c.get("id"): c for c in script.get("characters") or [] if isinstance(c, dict)}
    for cid in shot.get("characters") or []:
        look = str((chars.get(cid) or {}).get("appearance_en") or "").strip().rstrip(".")
        if look and look not in own:
            parts.append(f"{(chars[cid].get('name') or cid)}: {look}. ")
    parts.append(own)
    return "".join(parts).strip()


def fallback_size(size: str) -> str:
    """OpenAI 文档列出的标准尺寸（1024x1024 / 1024x1536 / 1536x1024），按原尺寸的横竖方向选"""
    try:
        w, h = (int(v) for v in str(size).lower().replace("×", "x").split("x"))
    except ValueError:
        return "1024x1024"
    if w == h:
        return "1024x1024"
    return "1024x1536" if h > w else "1536x1024"


def _generate_image(final_prompt: str, output_path: str, model: str, size: str, label: str) -> str:
    """调用 right.codes 图像接口生成一张图并保存，返回保存路径。带 502 / 网络异常重试"""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    size_normalized = size.replace("×", "x")

    print(f"[层2] 生成{label} | 模型：{model} | 尺寸：{size_normalized}")
    print(f"      Prompt：{final_prompt[:80]}...")

    headers = {
        "Authorization": f"Bearer {RIGHT_CODES_DRAW_KEY}",
        "Content-Type": "application/json",
        "Connection": "close",
    }
    payload = {"model": model, "prompt": final_prompt, "n": 1, "size": size_normalized}

    max_retries = 3
    resp = None
    for attempt in range(max_retries):
        # 临时移除 NO_PROXY 以避免 requests 忽略系统代理
        old_no_proxy = os.environ.pop("NO_PROXY", None)
        old_no_proxy_lower = os.environ.pop("no_proxy", None)
        try:
            # 大图常要 1–2 分钟，120 秒超时后重提交可能重复扣费，放宽到 300 秒
            resp = requests.post(
                f"{RIGHT_CODES_BASE_URL.rstrip('/')}/images/generations",
                headers=headers, json=payload, timeout=300.0,
            )
            if resp.status_code == 200:
                break
            # 平台不接受这个尺寸：换成同方向的标准尺寸再试（不扣费，请求被拒时还没开始生成）
            fallback = fallback_size(payload["size"])
            if 400 <= resp.status_code < 500 and "size" in resp.text.lower() and fallback != payload["size"]:
                print(f"[层2] 不支持尺寸 {payload['size']}，改用 {fallback}")
                payload["size"] = fallback
                continue
            if (resp.status_code >= 500 or resp.status_code == 429) and attempt < max_retries - 1:
                print(f"[{attempt+1}/{max_retries}] API 返回 {resp.status_code}，10 秒后重试...")
                time.sleep(10)
                continue
            raise Exception(f"API请求失败 ({resp.status_code}): {resp.text[:300]}")
        except requests.exceptions.ReadTimeout as e:
            # 服务端可能已经在生成并计费，不自动重提交
            raise Exception(f"{label} 生图超时（300 秒），为避免重复扣费未自动重试，请稍后手动重试") from e
        except requests.exceptions.RequestException as e:
            # 针对 SSL EOF 或者网络重置，也进行重试
            if attempt < max_retries - 1:
                print(f"[{attempt+1}/{max_retries}] 网络请求异常 ({type(e).__name__})，5秒后重试...")
                time.sleep(5)
                continue
            raise
        finally:
            if old_no_proxy is not None:
                os.environ["NO_PROXY"] = old_no_proxy
            if old_no_proxy_lower is not None:
                os.environ["no_proxy"] = old_no_proxy_lower

    img_data = resp.json()["data"][0]
    # 优先 b64_json（如果平台支持）；否则下载 URL
    if img_data.get("b64_json"):
        with open(output_path, "wb") as f:
            f.write(base64.b64decode(img_data["b64_json"]))
    elif img_data.get("url"):
        urllib.request.urlretrieve(img_data["url"], output_path)
    else:
        raise ValueError(f"{label}：响应中无 url 也无 b64_json")

    print(f"[层2] [OK] {label} 已保存：{output_path}")
    return output_path


def generate_shot_image(
    shot: dict,
    output_dir: str = "./pipeline/output/assets/shots",
    model: str = DEFAULT_IMAGE_MODEL,
    size: str = DEFAULT_SIZE,
    storyboard: dict | None = None,
) -> str:
    """为单个分镜生成静态基底图，返回保存路径。storyboard 用来拼场景/角色设定和画幅"""
    beat = shot.get("emotion_beat", "")
    final_prompt = compose_shot_prompt(shot, storyboard)
    output_path = os.path.join(output_dir, f"shot_{int(shot['id']):02d}_base.png")
    return _generate_image(final_prompt, output_path, model, size, f"镜头{shot['id']} 基底图 ({beat})")


def generate_character_portrait(
    character: dict,
    output_dir: str = CHARACTER_DIR,
    model: str = DEFAULT_IMAGE_MODEL,
    size: str = PORTRAIT_SIZE,
) -> str:
    """
    为单个角色生成定妆照，返回保存路径。
    提示词只用 appearance_en（外貌/服装），与 H3 提示词里的角色描述是同一段文字，图文一致。
    """
    final_prompt = PORTRAIT_PREFIX + character.get("appearance_en", "")
    output_path = os.path.join(output_dir, f"{character['id']}_portrait.png")
    return _generate_image(final_prompt, output_path, model, size, f"角色 {character['id']} {character.get('name', '')} 定妆照")


def generate_all_shots(
    storyboard_path: str = "./pipeline/output/storyboard.json",
    output_dir: str = "./pipeline/output/assets/shots",
    model: str = DEFAULT_IMAGE_MODEL,
    size: str = None,
    portrait_size: str = PORTRAIT_SIZE,
    draft: bool = False,
) -> dict:
    """
    读取层 1 输出的分镜 JSON：
      1. 为还没有定妆照的角色生成定妆照（已上传或已生成的不重复生成，避免重复计费、也避免长相漂移）
      2. 为 T2VA 镜头生成构图基底图
    Ref2VA 镜头不生图：它的参考图是角色定妆照 + 用户上传的产品图。

    Args:
        size: 基底图尺寸；为 None 时使用分镜 meta.image_size（由目标平台决定），再回退到 DEFAULT_SIZE
        portrait_size: 定妆照尺寸

    Returns:
        更新后的分镜 JSON dict
    """
    with open(storyboard_path, "r", encoding="utf-8") as f:
        storyboard = json.load(f)

    size = size or storyboard.get("meta", {}).get("image_size") or DEFAULT_SIZE
    characters = (storyboard.get("script") or {}).get("characters", [])

    print(f"[层2] 开始 | {len(characters)} 个角色 | {len(storyboard['shots'])} 个镜头 | 模型：{model}")

    for c in characters:
        path = c.get("image_path", "")
        # 草图模式生成的定妆照质量低，正式出图时要重新生成；上传的和正式生成的保留
        is_draft_portrait = c.get("image_source") == "draft" and not draft
        if path and os.path.exists(path) and not is_draft_portrait:
            print(f"[层2] 角色 {c['id']} 已有定妆照，跳过")
            continue
        c["image_path"] = generate_character_portrait(c, model=model, size=portrait_size)
        c["image_source"] = "draft" if draft else "generated"
        # 每生成一张就写回，中途失败时已生成的定妆照不会丢
        with open(storyboard_path, "w", encoding="utf-8") as f:
            json.dump(storyboard, f, ensure_ascii=False, indent=2)

    for shot in storyboard["shots"]:
        if str(shot.get("mode", "")).upper() == "REF2VA":
            shot["image_path"] = ""  # 参考图由 layer1.shot_references() 动态计算
            continue
        # 构图底图只供确认、不传给 H3：同一模式下已有就不重复生成（每点一次层 2 都重画很费钱）。
        # 新剧本的 image_path 为空，会正常生成；草图/正式模式输出目录不同，切换模式会重新生成
        old = shot.get("image_path") or ""
        if old and os.path.exists(old) and Path(old).parent.resolve() == Path(output_dir).resolve():
            print(f"[层2] 镜头 {shot['id']} 已有底图，跳过")
            continue
        shot["image_path"] = generate_shot_image(shot, output_dir=output_dir, model=model, size=size,
                                                 storyboard=storyboard)
        with open(storyboard_path, "w", encoding="utf-8") as f:
            json.dump(storyboard, f, ensure_ascii=False, indent=2)

    with open(storyboard_path, "w", encoding="utf-8") as f:
        json.dump(storyboard, f, ensure_ascii=False, indent=2)

    print(f"\n[层2] ✓ 全部完成，已回填至：{storyboard_path}")
    return storyboard


def quick_draft(
    storyboard_path: str = "./pipeline/output/storyboard.json",
    output_dir: str = "./pipeline/output/assets/shots_draft",
) -> dict:
    """使用 nano-banana-2-lite 快速生成草图（成本最低），用于人工确认构图方向"""
    return generate_all_shots(
        storyboard_path=storyboard_path,
        output_dir=output_dir,
        model="nano-banana-2-lite",
        size="1024x1024",           # nano 系列仅支持正方形
        portrait_size="1024x1024",
        draft=True,
    )


# ── 示例入口 ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="层 2：生成角色定妆照与分镜基底图")
    parser.add_argument("--storyboard", default="./pipeline/output/storyboard.json")
    parser.add_argument("--output-dir", default="./pipeline/output/assets/shots")
    parser.add_argument("--model", default=DEFAULT_IMAGE_MODEL,
                        help="gpt-image-2.5 / nano-banana-2 / nano-banana-2-lite")
    parser.add_argument("--size", default=None)
    parser.add_argument("--draft", action="store_true",
                        help="快速草图模式（nano-banana-2-lite，最便宜）")
    args = parser.parse_args()

    if args.draft:
        storyboard = quick_draft(args.storyboard, args.output_dir + "_draft")
    else:
        storyboard = generate_all_shots(args.storyboard, args.output_dir, args.model, args.size)

    print("\n[层2] 生成完成：")
    for c in (storyboard.get("script") or {}).get("characters", []):
        print(f"  角色 {c['id']} → {c.get('image_path', '未生成')}")
    for shot in storyboard["shots"]:
        print(f"  镜头{shot['id']} → {shot.get('image_path') or '(Ref2VA，使用参考图)'}")
