import modal
import os
import shutil
import subprocess

app = modal.App("comfyui-minimax-h3")

# 模型盘 + 输出盘（容器销毁后 output/ 会丢，所以单独持久化）
models_vol = modal.Volume.from_name("minimax-h3-models", create_if_missing=True)
output_vol = modal.Volume.from_name("minimax-h3-output", create_if_missing=True)

MODELS_DIR = "/root/ComfyUI/models"
OUTPUT_DIR = "/root/ComfyUI/output"

# 使用最新 master，以获得最新的节点模板兼容性修复
# 如需固定版本可改回 tag，例如 "v0.30.0"
COMFY_REF = "master"

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("git", "ffmpeg", "libgl1", "libglib2.0-0")
    # 固定一组互相匹配的 torch 版本；cu128 同时支持 A100 / H100 / B200
    .pip_install(
        "torch==2.8.0",
        "torchvision==0.23.0",
        "torchaudio==2.8.0",
        index_url="https://download.pytorch.org/whl/cu128",
    )
    .run_commands(
        f"git clone https://github.com/comfyanonymous/ComfyUI.git /root/ComfyUI "
        f"&& cd /root/ComfyUI && git checkout {COMFY_REF}",
        "pip install -r /root/ComfyUI/requirements.txt",
        # 原脚本的 comfy-kitchen 修复：只在当前 COMFY_REF 确实有问题时保留
        # 注意：它可能和 requirements.txt 要求的版本冲突，构建日志里留意 pip 的冲突提示
        "pip install comfy-kitchen",
        "rm -rf /root/ComfyUI/models /root/ComfyUI/output",
    )
    .run_commands(
        # 自定义节点：clone 后都要装各自的 requirements
        "cd /root/ComfyUI/custom_nodes "
        "&& git clone https://github.com/LeonQ8/ComfyUI-ALLinONE-MinimaxH3.git "
        "&& git clone https://github.com/kijai/ComfyUI-KJNodes.git "
        "&& git clone https://github.com/ltdrdata/ComfyUI-Manager.git "
        "&& git clone https://github.com/vrgamegirl19/comfyui-vrgamedevgirl.git",  # Audio Drive mode dependency
        "cd /root/ComfyUI/custom_nodes && for d in */; do "
        "  [ -f \"$d/requirements.txt\" ] && pip install -r \"$d/requirements.txt\" || true; "
        "done",
    )
    .pip_install("huggingface_hub", "hf_xet")
    .env({"HF_XET_HIGH_PERFORMANCE": "1"})  # 替代已废弃的 HF_HUB_ENABLE_HF_TRANSFER
)

# 按 ComfyUI 目录分类的 Comfy-Org 拆分权重
# 用 glob 匹配文件名，不依赖仓库里的子目录结构
COMFY_ORG_FILES = {
    "diffusion_models": [
        "*minimax_h3_ref2va_pruned_int8_convrot*.safetensors",
        "*minimax_h3_fl2va_pruned_int8_convrot*.safetensors",
    ],
    # A100 没有原生 FP4，用 int8 文字编码器；H100 上也建议先用 int8
    "text_encoders": [
        "*qwen3vl_32b_minimax_h3*int8*.safetensors",
        "*qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    ],
    "vae": [
        "*minimax_h3_video_vae_fp16*.safetensors",
        "*minimax_h3_audio_vae_fp32*.safetensors",
    ],
}


@app.function(image=image, volumes={MODELS_DIR: models_vol}, timeout=4 * 3600)
def download_weights():
    from huggingface_hub import snapshot_download

    staging = "/tmp/hf_staging"
    for subdir, patterns in COMFY_ORG_FILES.items():
        target = os.path.join(MODELS_DIR, subdir)
        os.makedirs(target, exist_ok=True)
        snapshot_download(
            repo_id="Comfy-Org/MiniMax-H3",
            local_dir=staging,
            allow_patterns=patterns,
        )
        # 把匹配到的文件平铺到 ComfyUI 对应目录
        found = 0
        for root, _, files in os.walk(staging):
            for f in files:
                if f.endswith(".safetensors"):
                    dst = os.path.join(target, f)
                    if not os.path.exists(dst):
                        shutil.move(os.path.join(root, f), dst)
                    found += 1
        shutil.rmtree(staging, ignore_errors=True)
        if found == 0:
            raise RuntimeError(f"{subdir}: 没有匹配到文件 {patterns}，请到 HF 仓库核对文件名")
        print(f"{subdir}: {found} 个文件就绪")

    models_vol.commit()
    print("下载完成")


@app.function(
    image=image,
    gpu="A100-80GB",
    volumes={MODELS_DIR: models_vol, OUTPUT_DIR: output_vol},
    max_containers=1,        # 只开一个容器：ComfyUI 的队列和状态都在单进程里
    scaledown_window=600,    # 空闲 10 分钟后关机，避免长任务期间被回收
    timeout=24 * 3600,
)
@modal.concurrent(max_inputs=100)  # 前端会并发发很多请求，必须允许同容器并发
# 安全提醒：不加鉴权时，任何拿到 URL 的人都能用你的 GPU。
# requires_proxy_auth=True 会要求请求带 Modal-Key / Modal-Secret 头，
# 浏览器直接打开会被拒，需要配合能加请求头的插件或反向代理使用。
@modal.web_server(port=8188, startup_timeout=600)
def ui():
    # web_server 的函数应该启动后台进程并返回，Modal 会自己等端口就绪
    subprocess.Popen(
        [
            "python", "main.py",
            "--listen", "0.0.0.0",
            "--port", "8188",
            "--output-directory", OUTPUT_DIR,
        ],
        cwd="/root/ComfyUI",
    )
