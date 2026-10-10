"""
层 2 · 视觉资产层
使用 right.codes「draw」令牌 + 图像模型生成：
  1. 角色定妆照：每个角色一张，作为 Ref2VA 参考图锁定全片人物长相
  2. per-shot 静态基底图：用于确认构图（纯文生视频镜头）
Base URL：https://www.right.codes/v1

截图中可见的图像模型（按质量/价格排序）：
  - gpt-image-2.5          高质量，OpenAI 出品，$0.06/张
  - gpt-image-2.5-flare    gpt-image-2.5 变体（不可用，备选）
  - gpt-image-2.5-sunburst gpt-image-2.5 变体
  - nano-banana            Google 出品，极速廉价，适合批量草稿
  - nano-banana-2          Google 出品，中等质量
  - nano-banana-2-lite     Google 出品，$0.05/张，最便宜

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
#   RIGHT_CODES_DRAW_KEY=sk-...    # 「draw」令牌（截图 sk***73）
RIGHT_CODES_BASE_URL = "https://www.rightapi.ai/v1"
RIGHT_CODES_DRAW_KEY = os.environ.get("RIGHT_CODES_DRAW_KEY", "")

# 默认使用 gpt-image-2.5（高质量，兼容 OpenAI images.generate 接口）
DEFAULT_IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-2.5")

# 带货竖版：1024×1792；横版/正方：1024×1024
DEFAULT_SIZE = os.environ.get("IMAGE_SIZE", "1024×1792")  # 9:16 竖屏

# 角色定妆照固定竖版，便于容纳全身
PORTRAIT_SIZE = "1024x1792"
CHARACTER_DIR = "./pipeline/output/assets/characters"


# ── 每个镜头对应的生图风格前缀 ────────────────────────────────────────────────
STYLE_PREFIX = (
    "Product photography, professional studio shot, ultra-sharp, photorealistic, "
    "commercial advertising quality, 4K resolution, clean background, "
)

# 定妆照：中性表情、正面全身、纯色背景、平光。参考图越"干净"，H3 越容易只提取人物身份而不带入多余元素
PORTRAIT_PREFIX = (
    "Character reference photo for a live-action commercial, full-body front view, standing straight, "
    "neutral relaxed expression, looking at camera, face clearly visible, plain light grey seamless studio "
    "background, soft even lighting, no props, no text, photorealistic, sharp focus, "
)

# 带货场景 → 额外风格修饰词映射
EMOTION_BEAT_STYLE: dict[str, str] = {
    "开场钩子":   "dramatic lighting, hero shot, ",
    "卖点展示":   "clean white/gradient background, product centered, macro detail, ",
    "使用场景":   "lifestyle photography, natural warm lighting, model using product, ",
    "信任背书":   "clinical white background, scientific/medical aesthetic, ",
    "行动召唤":   "bright vibrant colors, energetic composition, urgency feel, ",
}


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
) -> str:
    """为单个分镜生成静态基底图，返回保存路径"""
    beat = shot.get("emotion_beat", "")
    final_prompt = STYLE_PREFIX + EMOTION_BEAT_STYLE.get(beat, "") + shot.get("image_gen_prompt", "")
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
        shot["image_path"] = generate_shot_image(shot, output_dir=output_dir, model=model, size=size)
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
