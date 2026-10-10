import os
import json
import gradio as gr
fro
from pathlib import Path

# 加载环境变量
load_dotenv(Path(__file__).parent / ".env")

# 导入我们的 Pipeline 层
from layer1_planning import generate_storyboard, save_storyboard
from layer2_visual_assets import generate_all_shots, quick_draft
from layer3_h3_prompt import process_all_prompts

# 全局变量保存当前状态
current_storyboard = None
STORYBOARD_PATH = "./pipeline/output/storyboard.json"
ASSETS_DIR = "./pipeline/output/assets/shots"

def run_layer1(product_name, usp_text, brand_color, shot_count, platform):
    global current_storyboard
    if not product_name or not usp_text:
        return "⚠️ 请填写商品名称和核心卖点", None
    
    usps = [u.strip() for u in usp_text.split('\n') if u.strip()]
    
    try:
        storyboard = generate_storyboard(
            product_name=product_name,
            usp=usps,
            brand_color=brand_color,
            shot_count=int(shot_count),
            target_platform=platform
        )
        save_storyboard(storyboard, STORYBOARD_PATH)
        current_storyboard = storyboard
        
        # 格式化预览输出
        preview = ""
        for shot in storyboard["shots"]:
            preview += f"🎬 镜头 {shot['id']} ({shot['duration_sec']}s) | 模式: {shot['mode']} | 节拍: {shot['emotion_beat']}\n"
            preview += f"🗣️ 口播: {shot['voiceover']}\n"
            preview += f"🎥 视觉: {shot['visual_intent']}\n\n"
            
        return "✅ 分镜生成成功！已保存到 " + STORYBOARD_PATH, preview
# MISSING LINE 46
# MISSING LINE 47
# MISSING LINE 48
# MISSING LINE 49
# MISSING LINE 50
# MISSING LINE 51
# MISSING LINE 52
# MISSING LINE 53
# MISSING LINE 54
# MISSING LINE 55
# MISSING LINE 56
# MISSING LINE 57
# MISSING LINE 58
# MISSING LINE 59
# MISSING LINE 60
# MISSING LINE 61
# MISSING LINE 62
# MISSING LINE 63
# MISSING LINE 64
# MISSING LINE 65
# MISSING LINE 66
# MISSING LINE 67
# MISSING LINE 68
# MISSING LINE 69
# MISSING LINE 70
# MISSING LINE 71
# MISSING LINE 72
# MISSING LINE 73
# MISSING LINE 74
# MISSING LINE 75
# MISSING LINE 76
# MISSING LINE 77
# MISSING LINE 78
# MISSING LINE 79
# MISSING LINE 80
# MISSING LINE 81
# MISSING LINE 82
# MISSING LINE 83
# MISSING LINE 84
# MISSING LINE 85
# MISSING LINE 86
# MISSING LINE 87
# MISSING LINE 88
# MISSING LINE 89
# MISSING LINE 90
# MISSING LINE 91
# MISSING LINE 92
# MISSING LINE 93
# MISSING LINE 94
# MISSING LINE 95
# MISSING LINE 96
# MISSING LINE 97
# MISSING LINE 98
# MISSING LINE 99
                    preview_text += f"=== {file} ===\n{f.read()}\n\n"
                    
        return f"✅ H3 提示词已成功生成到 {prompts_dir}!\n\n预览:\n{preview_text}"
    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"❌ 发生错误: {str(e)}\n\n详情:\n{traceback.format_exc()}"


# ─── Gradio UI 构建 ─────────────────────────────────────────────────────────
with gr.Blocks(title="AI 带货视频流水线") as demo:
    gr.Markdown("# 🎬 AI 带货视频全链路流水线 (Antigravity + right.codes + H3)")
    
    with gr.Row():
        # 左侧：输入与控制台
        with gr.Column(scale=1):
            gr.Markdown("### 层 1 · 策划层 (输入商品信息)")
            prod_name = gr.Textbox(label="商品名称", placeholder="例如：玻尿酸原液精华")
            prod_usp = gr.Textbox(label="核心卖点 (每行一条)", lines=4, placeholder="1000万分子玻尿酸\n3秒渗透不油腻\n敏感肌可用")
            with gr.Row():
                prod_color = gr.ColorPicker(label="品牌主色", value="#C8A882")
                shot_num = gr.Slider(minimum=3, maximum=8, step=1, value=5, label="镜头数量")
            platform = gr.Dropdown(choices=["抖音/快手（竖版 9:16）", "视频号（竖版 9:16）", "淘宝/京东（横版/竖版）"], value="抖音/快手（竖版 9:16）", label="目标平台")
            
            btn_layer1 = gr.Button("1️⃣ 生成分镜脚本", variant="primary")
            
            gr.Markdown("---")
            gr.Markdown("### 层 2 · 视觉资产层 (生成基底图)")
            is_draft = gr.Checkbox(label="使用草图模式 (nano-banana-2-lite，又快又便宜)", value=False)
            btn_layer2 = gr.Button("2️⃣ 批量生成视觉基底图", variant="primary")
            
            gr.Markdown("---")
            gr.Markdown("### 层 3 · H3 提示词层 (应用 H3 规范)")
            btn_layer3 = gr.Button("3️⃣ 生成 H3 TXT 控制提示词", variant="primary")
            
            status_text = gr.Textbox(label="系统状态", interactive=False)

        # 右侧：结果展示
        with gr.Column(scale=1):
            gr.Markdown("### 分镜脚本预览")
            storyboard_preview = gr.Textbox(label="镜头脚本与口播词", lines=12, interactive=False)
# MISSING LINE 141
# MISSING LINE 142
# MISSING LINE 143
# MISSING LINE 144
# MISSING LINE 145
# MISSING LINE 146
# MISSING LINE 147
# MISSING LINE 148
# MISSING LINE 149
# MISSING LINE 150
            )

    # 绑定事件
    btn_layer1.click(
        fn=run_layer1,
        inputs=[prod_name, prod_usp, prod_color, shot_num, platform],
        outputs=[status_text, storyboard_preview]
    )
    
    btn_layer2.click(
        fn=run_layer2,
        inputs=[is_draft],
        outputs=[status_text, gallery]
    )
    
    btn_layer3.click(
        fn=run_layer3,
        inputs=[],
        outputs=[status_text]
    )

if __name__ == "__main__":
    demo.launch(server_port=7860, share=False)

