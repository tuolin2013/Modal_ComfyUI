import os
import json
import gradio as gr
from dotenv import load_dotenv
from pathlib import Path

# 加载环境变量
load_dotenv(Path(__file__).parent / ".env")

# 导入我们的 Pipeline 层
from layer1_planning import generate_storyboard, save_storyboard
from layer1_planning import generate_storyboard, save_storyboard
from layer2_visual_assets import generate_all_shots, quick_draft
from layer3_h3_prompt import process_all_prompts

# 全局变量保存当前状态
current_storyboard = None
STORYBOARD_PATH = "./pipeline/output/storyboard.json"
ASSETS_DIR = "./pipeline/output/assets/shots"
PRODUCT_REF_DIR = "./pipeline/output/user_product_refs"

import subprocess
import modal

COMFYUI_URL = "https://tuolin2011--comfyui-minimax-h3-ui-dev.modal.run"

def batch_render_with_diffusers(shots, ref_files, progress=gr.Progress()):
    """【模式B】：全自动批量调用 Diffusers 渲染并拼接成片"""
    output_videos = []
    
    # 查找 Modal 上的 Diffusers 服务
    H3Runner = modal.Cls.from_name("minimax-h3-diffusers-service", "H3BatchRunner")()
    
    total_shots = len(shots)
    video_dir = "./pipeline/output/videos"
    os.makedirs(video_dir, exist_ok=True)
    
    for idx, shot in enumerate(shots):
        progress((idx) / total_shots, desc=f"正在渲染镜头 {shot['id']}/{total_shots} ({shot['emotion_beat']})...")
        
        # 读取底图（如果是 I2VA 且需要产品图）
        img_bytes = None
        if shot["mode"] == "I2VA" and ref_files:
            # 根据 ref_needed 匹配，或者直接用第一张
            with open(ref_files[0].name, "rb") as f:
                img_bytes = f.read()
                
        # 远程呼叫 Modal 执行生成
        video_bytes = H3Runner.render_single_shot.remote(
            prompt=shot.get("image_gen_prompt", ""),
            image_bytes=img_bytes,
            duration_frames=124 # 约 5 秒
        )
        
        shot_path = os.path.join(video_dir, f"shot_{shot['id']}.mp4")
        with open(shot_path, "wb") as f:
            f.write(video_bytes)
        output_videos.append(shot_path)
        
    progress(0.95, desc="正在通过 FFmpeg 拼接完整成片...")
    final_output = os.path.join(video_dir, "final_19s_commercial.mp4")
    concat_list_path = os.path.join(video_dir, "concat_list.txt")
    
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for v in output_videos:
            f.write(f"file '{v}'\n")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path, "-c", "copy", final_output])
    
    return "✅ 批量渲染与拼接完成！", final_output

def save_product_images(files):
    if not files:
        return []
    os.makedirs(PRODUCT_REF_DIR, exist_ok=True)
    saved_paths = []
    for idx, file_obj in enumerate(files):
        # file_obj.name is the temp path in Gradio
        filename = f"ref_{idx+1}_{os.path.basename(file_obj.name)}"
        dest_path = os.path.join(PRODUCT_REF_DIR, filename)
        shutil.copy(file_obj.name, dest_path)
        saved_paths.append(dest_path)
    return saved_paths

def auto_concat_videos():
    """
    搜索 Modal 渲染输出目录的所有 shot_*.mp4 并无损拼接。
    由于目前还未实现 Modal 回传，这里搜索默认视频目录。
    """
    video_dir = "./pipeline/output/videos"
    os.makedirs(video_dir, exist_ok=True)
        gallery_items = []
        for shot in updated_storyboard["shots"]:
            img_path = shot.get("image_path", "")
            if img_path and os.path.exists(img_path):
                caption = f"镜头 {shot['id']} | {shot['emotion_beat']}\n模式: {shot['mode']}"
# MISSING LINE 96
# MISSING LINE 97
# MISSING LINE 98
# MISSING LINE 99
            return "⚠️ 未找到分镜数据，请先执行【1. 策划层】"
            
        prompts_dir = "./pipeline/output/prompts"
        process_all_prompts(STORYBOARD_PATH, prompts_dir)
        
        # 读取生成的提示词作为预览
        preview_text = ""
        for file in sorted(os.listdir(prompts_dir)):
            if file.endswith(".txt"):
                with open(os.path.join(prompts_dir, file), "r", encoding="utf-8") as f:
                    preview_text += f"=== {file} ===\n{f.read()}\n\n"
                    
        return f"✅ H3 提示词已成功生成到 {prompts_dir}!\n\n预览:\n{preview_text}"
    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"❌ 发生错误: {str(e)}\n\n详情:\n{traceback.format_exc()}"


# ─── Gradio UI 构建 ─────────────────────────────────────────────────────────
with gr.Blocks(title="AI 带货视频流水线") as demo:
    gr.Markdown("# 🎬 AI 带货视频全链路流水线 (Antigravity + right.codes + H3)")
    
    # 存储解析后的镜头状态列表
    shots_state = gr.State([])
    
    with gr.Row():
        # 左侧：输入与控制台
        with gr.Column(scale=1):
            gr.Markdown("### 层 1 · 策划层 (输入商品信息)")
            skill_path = os.path.join(".agents", "skills", skill_dirname, "SKILL.md")
            if os.path.exists(skill_path):
                with open(skill_path, "r", encoding="utf-8") as f:
                    skill_content = f.read()
        
        storyboard = generate_storyboard(
            product_name=product_name,
            usp=usps,
            brand_color=brand_color,
            story_template=story_template,
            target_platform=platform,
            num_refs=len(saved_refs),
            agent_skill_content=skill_content
        )
        save_storyboard(storyboard, STORYBOARD_PATH)
        current_storyboard = storyboard
        
        return "✅ 分镜生成成功！已保存到 " + STORYBOARD_PATH, storyboard.get("shots", [])
    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"❌ 发生错误: {str(e)}\n\n详情:\n{traceback.format_exc()}", []

def run_layer2(is_draft):
    global current_storyboard
    if not current_storyboard:
        if os.path.exists(STORYBOARD_PATH):
            with open(STORYBOARD_PATH, 'r', encoding='utf-8') as f:
                current_storyboard = json.load(f)
        else:
            return "⚠️ 未找到分镜数据，请先执行【1. 策划层】", []
    
    try:
        output_dir = ASSETS_DIR + "_draft" if is_draft else ASSETS_DIR
        
        if is_draft:
            generate_all_shots(STORYBOARD_PATH, output_dir, model="nano-banana-2-lite", size="1024x1024")
        else:
            # 使用默认配置（在 layer2 脚本里配置的）
            generate_all_shots(STORYBOARD_PATH, output_dir)
            
        # 重新读取带 image_path 的 json
        with open(STORYBOARD_PATH, 'r', encoding='utf-8') as f:
            updated_storyboard = json.load(f)
            
        # 组装 Gradio Gallery 数据 [(image_path, caption)]
        gallery_items = []
        for shot in updated_storyboard["shots"]:
            img_path = shot.get("image_path", "")
            if img_path and os.path.exists(img_path):
                caption = f"镜头 {shot['id']} | {shot['emotion_beat']}\n模式: {shot['mode']}"
                elem_id="gallery", 
                columns=2, 
                rows=2, 
                object_fit="contain",
                height="auto"
            )

    # 绑定事件
    btn_layer1.click(
        fn=run_layer1,
        inputs=[prod_name, prod_usp, prod_images, prod_color, shot_num, platform],
        outputs=[status_text, storyboard_preview]
    )
    
    btn_layer2.click(
        fn=run_layer2,
        inputs=[is_draft],
        outputs=[status_text, gallery]
    )
            if file.endswith(".txt"):
                with open(os.path.join(prompts_dir, file), "r", encoding="utf-8") as f:
                    preview_text += f"=== {file} ===\n{f.read()}\n\n"
                    
        return f"✅ H3 提示词已成功生成到 {prompts_dir}!\n\n预览:\n{preview_text}"
    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"❌ 发生错误: {str(e)}\n\n详情:\n{traceback.format_exc()}"


# ─── Gradio UI 构建 ─────────────────────────────────────────────────────────
with gr.Blocks(title="AI 带货视频流水线") as demo:
    gr.Markdown("# 🎬 AI 带货视频全链路流水线 (Antigravity + right.codes + H3)")
    
    # 存储解析后的镜头状态列表
    shots_state = gr.State([])
    
    with gr.Row():
        # 左侧：输入与控制台
        with gr.Column(scale=1):
            gr.Markdown("### 层 1 · 策划层 (输入商品信息)")
            prod_name = gr.Textbox(label="商品名称", placeholder="例如：玻尿酸原液精华")
            prod_usp = gr.Textbox(label="核心卖点 (每行一条)", lines=4, placeholder="1000万分子玻尿酸\n3秒渗透不油腻\n敏感肌可用")
            
            # 🌟 新增：多张真实产品参考图上传区
            prod_images = gr.File(
                label="📸 上传真实产品图（支持多选：包装盒、Logo、产品微距等）", 
                file_count="multiple", 
                file_types=["image"]
            )
            
            # 动态获取已安装的 Skills，排除非剧本类技能
            skill_choices = ["不使用（默认）"]
            skills_dir = os.path.join(".agents", "skills")
            if os.path.exists(skills_dir):
                for d in sorted(os.listdir(skills_dir)):
                    if d == "h3-prompt-writing":
                        continue  # 排除 H3 提示词撰写专属技能
                    
                    md_path = os.path.join(skills_dir, d, "SKILL.md")
                    if os.path.isdir(os.path.join(skills_dir, d)) and os.path.exists(md_path):
                        # 尝试读取文件内的 # 标题作为中英对照名称
                        title = d
                        with open(md_path, "r", encoding="utf-8") as f:
                            for line in f:
                                if line.startswith("# "):
                                    title = line.strip("# \n")
                                    break
                        skill_choices.append(f"{d} | {title}")
                        
                                    img_path = shot.get("image_path", "")
                                    if img_path and os.path.exists(img_path):
                                        gr.Image(value=img_path, label="Layer2 生成的基底图", interactive=False)
                                    else:
                                        gr.Markdown("*等待层2生成基底图...*")
            
            gr.Markdown("### 🍿 最终成品预览")
            final_video = gr.Video(label="拼接合成的最终广告片")

    # 绑定事件
    btn_layer1.click(
        fn=run_layer1,
        inputs=[prod_name, prod_usp, prod_images, prod_color, story_template, platform],
        outputs=[status_text, shots_state]
    )
    
    # 修改 layer2 事件，以便更新 shots_state
    def run_layer2_wrapper(is_draft):
        status, _ = run_layer2(is_draft)
            
            # 绑定上传事件：用户一选图，右边立即预览
            prod_images.change(
                fn=lambda files: [f.name for f in files] if files else [],
                inputs=[prod_images],
                outputs=[prod_gallery]
            )
            
            gr.Markdown("---")
            gr.Markdown("### 层 2 · 视觉资产层 (批量生成基底图)")
            is_draft = gr.Checkbox(label="使用草图模式 (nano-banana-2-lite，又快又便宜)", value=False)
            btn_layer2 = gr.Button("2️⃣ 批量生成视觉基底图", variant="primary")
            
            gr.Markdown("---")
            gr.Markdown("### 层 3 · H3 提示词层 (批量应用 H3 规范)")
            btn_layer3 = gr.Button("3️⃣ 生成 H3 TXT 控制提示词", variant="primary")
            
            gr.Markdown("---")
            gr.Markdown("### 层 4 · 后期合成层 (FFmpeg)")
            btn_layer4 = gr.Button("4️⃣ 一键拼接最终视频", variant="primary")
            
            status_text = gr.Textbox(label="系统状态", interactive=False)

        # 右侧：结果展示（动态渲染）
        with gr.Column(scale=1):
            gr.Markdown("### 🎞️ 动态镜头流水线（由脚本自动生成）")
            
            @gr.render(inputs=[shots_state, prod_images])
            def render_shot_cards(shots, uploaded_refs):
                if not shots:
                    gr.Markdown("*请先点击左侧「生成分镜脚本」，生成后将在此处自动装配各个镜头的资产卡片。*")
                    return
                    
                for shot in shots:
                    with gr.Group():
                        with gr.Row():
                            # 左边：显示剧本信息与口播
                            with gr.Column(scale=1):
                                gr.Markdown(f"#### 🎬 镜头 {shot.get('id')} ({shot.get('duration_sec')}s) | 模式: `{shot.get('mode')}` | 节拍: `{shot.get('emotion_beat')}`")
                                gr.Textbox(label="🗣️ 口播台词", value=shot.get("voiceover", ""), interactive=False)
                                gr.Textbox(label="🎥 视觉构图描述", value=shot.get("visual_intent", ""), lines=2, interactive=False)
                                gr.Textbox(label="📝 H3 控制提示词 (Prompt)", value=shot.get("image_gen_prompt", ""), lines=2, interactive=False)
                                
                            # 右边：自动分配底图展示或基底图预览
                            with gr.Column(scale=1):
                                mode = str(shot.get("mode", "")).upper()
                                ref_needed = shot.get("ref_needed")
                                
                                if mode == "T2VA" or mode == "T2V":
                                    gr.Markdown("💡 **纯文生视频 (T2VA)**：无需准备底图，直接由 H3 凭借 Prompt 渲染人戏。")
                                    # 如果 layer 2 跑完了生成了 image_path 也可以展示
                                    img_path = shot.get("image_path", "")
                                    if img_path and os.path.exists(img_path):
                                        gr.Image(value=img_path, label="Layer2 生成的参考图", interactive=False)
                                else:
                                    # 如果需要引用真实图
                                    ref_tip = f"🔒 绑定产品真实图：`{ref_needed}`" if ref_needed else "⚠️ 未指定具体参考图"
                                    gr.Markdown(f"📸 **图生视频 ({mode})** - {ref_tip}")
                                    
                                    # 尝试查找已生成的图
                                    img_path = shot.get("image_path", "")
                                    if img_path and os.path.exists(img_path):
                                        gr.Image(value=img_path, label="Layer2 生成的基底图", interactive=False)
                                    else:
                                        gr.Markdown("*等待层2生成基底图...*")
                                        
                        with gr.Tabs():
                            with gr.Tab("🛠️ 模式 A：单镜手动调试 (ComfyUI)"):
                                gr.Markdown("适合初调、验证效果。复制上方提示词，直接去 Modal 网页端操作：")
                                with gr.Row():
                                    open_comfy_btn = gr.Button("🔗 打开 Modal ComfyUI 页面", link=COMFYUI_URL, size="sm")

            gr.Markdown("---")
            gr.Markdown("### ⚡ 模式 B：全自动批量渲染 (Diffusers)")
            gr.Markdown("剧本和底图确认无误后，一键在云端全自动生成并合成带货成片：")
            btn_batch_run = gr.Button("🚀 启动 Diffusers 批量渲染并自动拼接", variant="primary")
            
            gr.Markdown("### 🍿 最终成品预览")
            final_video = gr.Video(label="拼接合成的最终广告片")

    # 绑定事件
    btn_layer1.click(
        fn=run_layer1,
        inputs=[prod_name, prod_usp, prod_images, prod_color, story_template, platform, agent_skill],
        outputs=[status_text, shots_state]
    )
    
    # 修改 layer2 事件，以便更新 shots_state
    def run_layer2_wrapper(is_draft):
        status, _ = run_layer2(is_draft)
        # 重新读取以更新 shots_state
        updated_shots = []
        if os.path.exists(STORYBOARD_PATH):
            with open(STORYBOARD_PATH, 'r', encoding='utf-8') as f:
                d = json.load(f)
                updated_shots = d.get("shots", [])
        return status, updated_shots

    btn_layer2.click(
        fn=run_layer2_wrapper,
        inputs=[is_draft],
        outputs=[status_text, shots_state]
    )
    
    btn_layer3.click(
        fn=run_layer3,
        inputs=[],
        outputs=[status_text]
    )
    
    btn_layer4.click(
        fn=auto_concat_videos,
        inputs=[],
        outputs=[status_text, final_video]
    )
    
    btn_batch_run.click(
        fn=batch_render_with_diffusers,
        inputs=[shots_state, prod_images],
        outputs=[status_text, final_video]
    )

if __name__ == "__main__":
    demo.launch(server_port=7860, share=False)

