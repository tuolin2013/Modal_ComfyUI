"""
MiniMax H3 批量渲染服务（Modal + Diffusers）

首次使用（只需一次）：
  1. 预下载权重到共享卷，避免第一次渲染时边下载边跑导致超时：
       modal run h3_diffusers_service.py::download_weights
  2. 部署：
       modal deploy h3_diffusers_service.py

客户端调用（见 pipeline/app_gradio.py）：
  Runner = modal.Cls.from_name("minimax-h3-diffusers-service", "H3BatchRunner")
  t2va   = Runner(workflow="t2va")
  ref2va = Runner(workflow="ref2va")
  video_bytes = ref2va.render_single_shot.remote(prompt, [img1_bytes, img2_bytes], 124, seed)

设计要点：
  - workflow 是类参数：t2va 和 ref2va 各有独立的容器池，各自只加载一次模型，
    镜头交替出现时不会反复卸载/重载几十 GB 权重。
  - 客户端逐个镜头串行调用；每个 workflow 最多 1 个容器（MAX_CONTAINERS）。
  - 渲染结束后容器保温 SCALEDOWN_WINDOW 秒，连续两次批量渲染不必重新冷启动。

references 顺序 = 提示词里的 <Picture 1>、<Picture 2>...
本项目约定：先角色定妆照，后产品图（见 pipeline/layer1_planning.shot_references）。
"""

import modal

APP_NAME = "minimax-h3-diffusers-service"
MODEL_ID = "MiniMaxAI/MiniMax-H3"
MODELS_DIR = "/models"

FPS = 24
MAX_REF_IMAGES = 9            # H3 ref2va 上限：最多 9 张图
WORKFLOWS = ("t2va", "ref2va")

# 实测：A100-80GB 在 pipe.to("cuda") 阶段就 OOM（整套 bf16 组件已占 78.7 GiB，还没开始推理）。
# 可选（显存 ≥ 96GB）：
#   "RTX-PRO-6000"  96GB  $3.03/h  最便宜；加载后只剩 ~17GB 给推理，若推理时 OOM 改用 H200
#   "H200"         141GB  $4.54/h  显存宽裕，最稳
GPU = "RTX-PRO-6000"
# 实测：RTX-PRO-6000 (96GB) 同样在 pipe.to("cuda") 时 OOM（已占 94.3 GiB 还没搬完），整套组件 > 95GB。
# 开启后组件常驻内存，推理时用到哪个才搬上 GPU（文本编码器 → transformer → VAE 依次上卡），
# 显存峰值约等于最大的单个组件 + 推理激活。代价是每个镜头多出组件搬运时间。
CPU_OFFLOAD = True
MAX_CONTAINERS = 1            # 每个 workflow 最多同时开几张卡；客户端目前串行调用，先保持 1
SCALEDOWN_WINDOW = 120        # 空闲 2 分钟后释放 GPU（空闲也按秒计费）
RENDER_TIMEOUT = 1800

# diffusers 的 ref2va 修复只在 main 分支（huggingface/diffusers#14379：旧版忽略 references）。
# 跑通后把 "main" 换成当时的 commit hash 固定下来，避免以后重建镜像时装到行为不同的版本。
DIFFUSERS_REF = "main"

app = modal.App(APP_NAME)

# 共享卷存放模型权重，所有容器共用，只下载一次
volume = modal.Volume.from_name("minimax-h3-weights", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git", "ffmpeg")
    .pip_install(
        "torch",
        "torchvision",
        "transformers>=4.45.0",
        "accelerate",
        "huggingface_hub",
        "pillow",
        "sentencepiece",
        "qwen-vl-utils",
        "av",                  # encode_video 写入音轨需要
    )
    .pip_install(f"git+https://github.com/huggingface/diffusers.git@{DIFFUSERS_REF}")
    .env({
        "HF_HUB_CACHE": MODELS_DIR,
        "HF_XET_HIGH_PERFORMANCE": "1",     # 新版 huggingface_hub 的高速下载开关（取代 hf_transfer）
        "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",  # 减少显存碎片
    })
)


def _verify_and_repair(deep: bool = False) -> list[str]:
    """
    对照 HF 上的文件大小（deep=True 时再比对 sha256）校验卷里的权重，
    删除不完整/损坏的文件并强制重新下载。返回修复过的文件列表。

    snapshot_download 只看文件在不在，不看是否完整：下载中断留下的半截文件会被当成已下载，
    加载时才报 "Unable to load weights from checkpoint file"。
    """
    import hashlib
    import os

    from huggingface_hub import HfApi, hf_hub_download

    info = HfApi().model_info(MODEL_ID, files_metadata=True)
    snapshot = os.path.join(MODELS_DIR, f"models--{MODEL_ID.replace('/', '--')}", "snapshots", info.sha)
    print(f">>> 校验 {MODEL_ID}@{info.sha[:8]}，共 {len(info.siblings)} 个文件")

    bad = []
    for s in info.siblings:
        p = os.path.join(snapshot, s.rfilename)
        if not os.path.exists(p):
            reason = "缺失"
        elif s.size is not None and os.path.getsize(p) != s.size:
            reason = f"大小不符 {os.path.getsize(p)} != {s.size}"
        elif deep and s.lfs is not None:
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(64 << 20), b""):
                    h.update(chunk)
            reason = "sha256 不符" if h.hexdigest() != s.lfs.sha256 else ""
        else:
            reason = ""
        if reason:
            print(f"!!! {s.rfilename}: {reason}")
            bad.append(s.rfilename)

    for name in bad:
        p = os.path.join(snapshot, name)
        # 快照里是指向 blobs/ 的软链接，链接和实际文件都要删，否则 force_download 可能复用坏 blob
        if os.path.lexists(p):
            target = os.path.realpath(p)
            os.remove(p)
            if target != p and os.path.exists(target):
                os.remove(target)
        print(f">>> 重新下载 {name} ...")
        hf_hub_download(MODEL_ID, name, revision=info.sha, cache_dir=MODELS_DIR, force_download=True)
        volume.commit()  # 每修好一个就提交，中途断开也不丢进度

    print(">>> 校验通过，全部文件完整" if not bad else f">>> 已修复 {len(bad)} 个文件")
    return bad


@app.function(image=image, volumes={MODELS_DIR: volume}, timeout=3 * 3600)
def download_weights():
    """把权重下载到共享卷并校验完整性：modal run h3_diffusers_service.py::download_weights"""
    from huggingface_hub import snapshot_download

    path = snapshot_download(MODEL_ID, cache_dir=MODELS_DIR)
    volume.commit()
    print(f">>> 权重已缓存到共享卷：{path}")
    _verify_and_repair()


@app.function(image=image, volumes={MODELS_DIR: volume}, timeout=3 * 3600)
def verify_weights(deep: bool = False):
    """
    只校验+修复，不重新下载完整的文件：
      modal run h3_diffusers_service.py::verify_weights          # 按文件大小，几秒钟
      modal run h3_diffusers_service.py::verify_weights --deep   # 再比对 sha256，要读完全部权重，较慢
    """
    _verify_and_repair(deep)


def _component_report(pipe) -> float:
    """打印各组件的类名、精度和参数体积（GiB），返回合计"""
    import torch

    comps = getattr(pipe, "components", None)
    if not isinstance(comps, dict):
        comps = {}
        for n in dir(pipe):
            if n.startswith("_"):
                continue
            try:
                comps[n] = getattr(pipe, n)
            except Exception:
                pass

    rows, total = [], 0.0
    for name, m in comps.items():
        if not isinstance(m, torch.nn.Module):
            continue
        params = list(m.parameters())
        gib = sum(p.numel() * p.element_size() for p in params) / 2**30
        dtypes = ",".join(sorted({str(p.dtype).replace("torch.", "") for p in params}))
        total += gib
        rows.append(f"    {name:<22} {type(m).__name__:<38} {dtypes:<16} {gib:6.1f} GiB")
    print(">>> 组件参数体积：\n" + "\n".join(rows) + f"\n    合计 {total:.1f} GiB")
    return total


def _patch_text_encoder_offload(comps: dict):
    """
    2026-10-10 实测：开启 ComponentsManager 自动 offload 后，ref2va 文本编码报
    "Expected all tensors to be on the same device, cuda:0 and cpu"。
    原因：diffusers 的 H3 编码步骤直接调用 text_encoder.model(...)，绕过了挂在 text_encoder 外层的
    offload 钩子，权重留在 CPU，输入却在 GPU。
    这里在 text_encoder.model 上补一个前置钩子：调用前先把其它大组件挪回 CPU，再把整个
    text_encoder（含视觉塔）搬上 GPU；并在 transformer*/vae 运行前把 text_encoder 挪回 CPU，
    避免 62G + 62G 同时在卡上导致 OOM。
    """
    import torch

    te = comps.get("text_encoder")
    inner = getattr(te, "model", None)
    if not isinstance(te, torch.nn.Module) or not isinstance(inner, torch.nn.Module):
        print(">>> 未找到 text_encoder.model，跳过 offload 补丁")
        return

    def on_cuda(m) -> bool:
        p = next(m.parameters(), None)
        return p is not None and p.device.type == "cuda"

    others = {k: v for k, v in comps.items() if isinstance(v, torch.nn.Module) and v is not te}

    def before_text_encoder(_module, _args):
        if on_cuda(te):
            return
        for k, m in others.items():
            if on_cuda(m):
                m.to("cpu")
        torch.cuda.empty_cache()
        te.to("cuda")

    def before_other(_module, _args):
        if on_cuda(te):
            te.to("cpu")
            torch.cuda.empty_cache()

    inner.register_forward_pre_hook(before_text_encoder)
    for m in others.values():
        m.register_forward_pre_hook(before_other)
    print(f">>> 已给 text_encoder.model 补 offload 钩子（其它组件：{list(others)}）")


# H3 服务端要求：帧数按 17n+5 向上取整后在 120–360 之间（实测报错信息），即 124–345
MIN_FRAMES, MAX_FRAMES = 124, 345


def _valid_frames(frames: int) -> int:
    """把帧数调整到 H3 接受的 17n+5 且在范围内；兼容旧客户端传 107 帧（4.5 秒）"""
    import math

    n = math.ceil((max(int(frames), MIN_FRAMES) - 5) / 17)
    return min(17 * n + 5, MAX_FRAMES)


@app.cls(
    gpu=GPU,
    image=image,
    volumes={MODELS_DIR: volume},
    timeout=RENDER_TIMEOUT,
    # CPU offload 时整套权重（>95GB）常驻内存，默认内存放不下，申请 160GB
    memory=160 * 1024 if CPU_OFFLOAD else None,
    max_containers=MAX_CONTAINERS,
    scaledown_window=SCALEDOWN_WINDOW,
)
class H3BatchRunner:
    # 每个 workflow 值对应独立的容器池
    workflow: str = modal.parameter(default="t2va")

    @modal.enter()
    def load_pipeline(self):
        import time

        import torch
        from diffusers import ModularPipeline

        if self.workflow not in WORKFLOWS:
            raise ValueError(f"未知 workflow: {self.workflow}，可选 {WORKFLOWS}")

        # Ampere/Hopper 上开启 TF32，matmul 明显加速，对生成质量几乎无影响
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True

        t0 = time.time()
        print(f">>> 加载 MiniMax H3 ({self.workflow}) ...")
        if CPU_OFFLOAD:
            # 组件常驻 CPU，用到哪个才搬到 GPU，显存峰值≈单个最大组件
            from diffusers import ComponentsManager

            manager = ComponentsManager()
            manager.enable_auto_cpu_offload(device="cuda")
            pipe = ModularPipeline.from_pretrained(
                MODEL_ID, workflow=self.workflow, cache_dir=MODELS_DIR, components_manager=manager
            )
            pipe.load_components(dtype=torch.bfloat16)
        else:
            pipe = ModularPipeline.from_pretrained(MODEL_ID, workflow=self.workflow, cache_dir=MODELS_DIR)
            pipe.load_components(dtype=torch.bfloat16)

        # 打印每个组件的实际大小和精度：用来判断能否整套放进某张卡，
        # 以及 dtype=bfloat16 是否真的生效（出现 float32 说明参数名不对，体积会翻倍）
        _component_report(pipe)

        # diffusers 加载组件失败时只打印 "you can safely ignore" 然后继续，
        # 结果要到推理时才报莫名其妙的错。这里直接拦下。
        # 注意：开启 ComponentsManager 后组件可能不挂在 pipe.transformer 属性上，
        # 所以以 pipe.components 为准，并把实际组件名打印出来，避免误判
        comps = getattr(pipe, "components", None)
        comps = comps if isinstance(comps, dict) else {}
        print(">>> 组件状态：" + ", ".join(
            f"{k}={'None' if v is None else type(v).__name__}" for k, v in comps.items()))
        # 不同 workflow 的主模型组件名不同：2026-10-10 实测 ref2va 叫 transformer_ref（t2va 可能叫 transformer），
        # 所以只要有一个以 transformer 开头且已加载的组件就算成功
        transformers = {k: v for k, v in comps.items() if k.startswith("transformer")}
        if getattr(pipe, "transformer", None) is not None:
            transformers.setdefault("transformer", pipe.transformer)
        if not any(v is not None for v in transformers.values()):
            raise RuntimeError(
                f"transformer 组件加载失败。实际组件：{list(comps)}（见上一行组件状态）。\n"
                "若有 transformer*=None：多半是卷里的权重文件不完整，运行：\n"
                "  modal run h3_diffusers_service.py::verify_weights\n"
                "若根本没有 transformer* 组件：多半是 diffusers main 分支改了组件名/加载方式"
            )
        print(f">>> 主模型组件：{[k for k, v in transformers.items() if v is not None]}")

        if CPU_OFFLOAD:
            _patch_text_encoder_offload(comps)

        if not CPU_OFFLOAD:
            pipe.to("cuda")
        print(f">>> 显存占用 {torch.cuda.memory_allocated() / 2**30:.1f} GiB")
        self.pipe = pipe
        print(f">>> 加载完成，用时 {time.time() - t0:.0f}s")

    @modal.method()
    def render_single_shot(
        self,
        prompt: str,
        images=None,
        duration_frames: int = 124,
        seed: int | None = None,
        num_inference_steps: int = 30,
    ) -> bytes:
        """
        渲染单个镜头，返回 mp4 字节（含 H3 生成的音轨）。

        Args:
            prompt: H3 提示词（Ref2VA 中用 <Picture N> 引用参考图）
            images: 参考图字节列表，顺序即 <Picture N>；兼容旧调用：直接传单个 bytes
            duration_frames: 帧数（24fps）
            seed: 随机种子；全片共用同一个种子可让画风更统一
        """
        import os
        import tempfile
        import time

        import torch
        from diffusers.utils import encode_video

        if isinstance(images, (bytes, bytearray)):
            images = [images]
        images = [b for b in (images or []) if b]

        # 输入与 workflow 不匹配时直接报错，避免静默生成错误结果
        if self.workflow == "ref2va" and not images:
            raise ValueError("ref2va 需要至少 1 张参考图")
        if self.workflow == "t2va" and images:
            raise ValueError("t2va 不接受参考图，请改用 workflow='ref2va'")
        if len(images) > MAX_REF_IMAGES:
            raise ValueError(f"参考图最多 {MAX_REF_IMAGES} 张，收到 {len(images)} 张")

        frames = _valid_frames(duration_frames)
        if frames != int(duration_frames):
            print(f">>> 帧数 {duration_frames} 不在 H3 允许范围，调整为 {frames}")
            duration_frames = frames
        kwargs = dict(
            prompt=prompt,
            num_frames=frames,
            num_inference_steps=num_inference_steps,
            output=["videos", "audio", "sampling_rate"],
        )
        if seed is not None:
            kwargs["generator"] = torch.Generator("cuda").manual_seed(int(seed))

        t0 = time.time()
        print(f">>> [{self.workflow}] {len(images)} 张参考图 | {duration_frames} 帧 | seed={seed} | {prompt[:60]}...")

        # 临时目录随 with 结束自动删除，容器连续渲染时不会堆积文件
        with tempfile.TemporaryDirectory() as tmpdir:
            if images:
                from diffusers.modular_pipelines.minimax_h3 import MiniMaxH3ImageReference

                refs = []
                for i, data in enumerate(images, 1):
                    p = os.path.join(tmpdir, f"ref_{i}.png")
                    with open(p, "wb") as f:
                        f.write(data)
                    refs.append(MiniMaxH3ImageReference.from_file(p))
                kwargs["references"] = refs

            with torch.inference_mode():
                results = self.pipe(**kwargs)

            out_path = os.path.join(tmpdir, "out.mp4")
            audio = results.get("audio")
            encode_video(
                results["videos"][0],
                fps=FPS,
                output_path=out_path,
                audio=audio[0] if audio is not None else None,
                audio_sample_rate=results.get("sampling_rate"),
            )
            with open(out_path, "rb") as f:
                data = f.read()

        print(f">>> 渲染完成，用时 {time.time() - t0:.0f}s，{len(data) / 1e6:.1f} MB")
        return data
