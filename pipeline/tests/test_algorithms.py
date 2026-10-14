"""算法改进：画面合规、自检按严重程度采纳、两步剧本（先创意后分镜）、层 2 提示词拼接、
电商视觉设定 / AI 写字信息图 / 留白区检查 / 产品还原度核对"""

import json

import pytest
from PIL import Image, ImageDraw

import ecom_images as ecom
import layer1_planning as l1
import layer2_visual_assets as l2
import quality_check as qc


# ── 画面合规 ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "a doctor in a white lab coat hands her the tea", "hospital corridor, cold light",
    "she clutches her chest, then relaxes", "a man coughing at his desk", "an IV drip next to the bed",
    "feeling sick on the sofa", "medical supplies on the shelf", "医生递给她一杯茶", "捂着喉咙，表情难受",
])
def test_visual_risk_flagged(text):
    assert qc.check_visual(text), text


@pytest.mark.parametrize("text", [
    "a cluttered office desk at 3 pm, overcast window light, a woman blowing on a glass mug",
    "macro shot of white frost on dried tea leaves, soft side light",
    "the cat chews a chicken-liver flavored tablet from her palm",       # 片剂本身不算医疗画面
    "the person holds the product close to the camera",                 # hold + the + product 不是捂胸口
    "a healthy-looking green hillside at dawn",                          # healthy 不是 heal
])
def test_visual_safe_copy_not_flagged(text):
    assert qc.check_visual(text) == [], text


def test_storyboard_check_reports_visual_risk():
    sb = {"script": {"scenes": [{"id": "S1", "description_en": "a bright hospital ward"}]},
          "shots": [{"id": 1, "voiceover": "下午三点，来一杯", "visual_intent": "",
                     "image_gen_prompt": "a nurse pours the tea"}]}
    text = "\n".join(qc.check_storyboard(sb, []))
    assert "镜头 1 画面风险「nurse」" in text and "场景 S1 画面风险「hospital」" in text


# ── 自检改稿按严重程度采纳 ─────────────────────────────────────────────────────
def test_accept_revision_weights_errors():
    err, warn = f"{qc.ERR} 口播风险宣称「治疗」", f"{qc.WARN} 卖点没有在剧本里体现：白霜"
    assert qc.issue_score([err, warn, f"{qc.INFO} 提示"]) == 6
    # 消掉两个提醒，却新增一个违规：条数变少，但不能采纳
    assert not qc.accept_revision([warn, warn + "2"], [err])
    assert qc.accept_revision([err], [warn, warn + "2"])              # 违规改成提醒：分数 5 → 2
    assert qc.accept_revision([err, warn], [err])                     # 原有的 ❌ 不算新引入
    assert not qc.accept_revision([warn], [warn, warn + "2"])


# ── 两步剧本：先创意后分镜 ─────────────────────────────────────────────────────
def _concepts(chosen=0, hooks=("这层白霜是什么？", "三点犯困来一杯", "喝了降血糖")):
    return {"concepts": [{"angle": f"角度{i}", "hook_visual": "a woman at her desk", "hook_line": h,
                          "conflict": "c", "payoff": "p", "score": 9 - i, "risk": ""}
                         for i, h in enumerate(hooks)], "chosen": chosen, "reason": "r"}


def test_normalize_concepts_skips_risky_choice():
    out = l1.normalize_concepts(_concepts(chosen=2), "food")
    assert out["chosen"] == 0 and "合规风险" in out["reason"]
    assert out["concepts"][2]["compliance"]
    out = l1.normalize_concepts(_concepts(chosen=1), "food")
    assert out["chosen"] == 1 and out["reason"] == "r"


def test_normalize_concepts_cleans_bad_input():
    assert l1.normalize_concepts({"concepts": "x"}) == {"concepts": []}
    raw = {"concepts": [{"angle": "a", "hook_line": "h", "score": "99"}, {"angle": "", "hook_line": "h"}, 3],
           "chosen": 7}
    out = l1.normalize_concepts(raw)
    assert len(out["concepts"]) == 1 and out["concepts"][0]["score"] == 10 and out["chosen"] == 0
    visual = l1.normalize_concepts({"concepts": [{"angle": "a", "hook_line": "h",
                                                  "hook_visual": "a doctor at the clinic"}]})
    assert visual["concepts"][0]["compliance"]


def _storyboard_json(vo="白霜看得见"):
    beats = [b[0] for b in l1.STORY_TEMPLATES[l1.DEFAULT_TEMPLATE]["beats"]]
    return json.dumps({"script": {"title": "t", "full_text": vo}, "product_refs": [], "shots": [
        {"id": i, "voiceover": vo, "emotion_beat": b, "mode": "T2VA", "duration_sec": 5}
        for i, b in enumerate(beats, 1)]}, ensure_ascii=False)


def test_concept_first_injects_chosen_direction(monkeypatch):
    calls = []

    def chat(messages, temperature=0.7):
        calls.append((messages, temperature))
        return (json.dumps(_concepts(chosen=1), ensure_ascii=False) if len(calls) == 1
                else _storyboard_json()), "stop"
    monkeypatch.setattr(l1, "_chat", chat)
    sb = l1.generate_storyboard("张家界莓茶", ["白霜"], playbook="food", concept_first=True)
    assert len(calls) == 2
    assert calls[0][0][0]["content"] == l1.CONCEPT_SYSTEM and calls[0][1] == 0.9
    assert "单镜时长" in calls[0][0][1]["content"]                     # 创意这一步也看得到平台和节拍
    story_prompt = calls[1][0][1]["content"]
    assert "【创意方向（已选定" in story_prompt and "三点犯困来一杯" in story_prompt
    assert "这层白霜是什么" not in story_prompt                       # 没选中的方向不写进分镜提示词
    assert sb["meta"]["concepts"]["chosen"] == 1


def test_concept_failure_falls_back_to_storyboard(monkeypatch):
    calls = []

    def chat(messages, temperature=0.7):
        calls.append(messages)
        if len(calls) == 1:
            raise Exception("网关 502")
        return _storyboard_json(), "stop"
    monkeypatch.setattr(l1, "_chat", chat)
    sb = l1.generate_storyboard("张家界莓茶", ["白霜"], concept_first=True)
    assert len(calls) == 2 and "concepts" not in sb["meta"] and len(sb["shots"]) == 4
    assert "【创意方向" not in calls[1][1]["content"]


def test_concept_first_off_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr(l1, "_chat", lambda m, temperature=0.7: calls.append(m) or (_storyboard_json(), "stop"))
    l1.generate_storyboard("张家界莓茶", ["白霜"])
    assert len(calls) == 1


def test_self_review_rejects_revision_that_adds_violation(monkeypatch):
    replies = iter([_storyboard_json("白霜看得见"), _storyboard_json("喝了能治疗咽炎")])
    monkeypatch.setattr(l1, "_chat", lambda m, temperature=0.7: (next(replies), "stop"))
    sb = l1.generate_storyboard("张家界莓茶", ["白霜"], playbook="food", self_review=True)
    assert sb["meta"]["self_reviewed"] is False and sb["shots"][0]["voiceover"] == "白霜看得见"


# ── 层 2：提示词由程序拼接 ────────────────────────────────────────────────────
SB = {"meta": {"aspect_ratio": "9:16"}, "script": {
    "characters": [{"id": "C1", "name": "小林", "appearance_en": "a woman in her 30s, grey cardigan, short black hair"}],
    "scenes": [{"id": "S1", "description_en": "a cluttered office desk at 3 pm, overcast window light."}]}}


def test_compose_shot_prompt_injects_scene_character_and_framing():
    shot = {"emotion_beat": "使用场景", "characters": ["C1"], "scene": "S1",
            "image_gen_prompt": "she blows on a glass mug, medium close-up"}
    p = l2.compose_shot_prompt(shot, SB)
    assert p.startswith(l2.LIFESTYLE_PREFIX.strip()[:20])
    assert "vertical 9:16 composition" in p
    assert "Setting: a cluttered office desk at 3 pm, overcast window light." in p
    assert "小林: a woman in her 30s, grey cardigan, short black hair." in p
    assert p.endswith("she blows on a glass mug, medium close-up")


def test_compose_shot_prompt_skips_settings_already_copied():
    own = "a cluttered office desk at 3 pm, overcast window light, a woman in her 30s, grey cardigan, short black hair"
    p = l2.compose_shot_prompt({"emotion_beat": "使用场景", "characters": ["C1"], "scene": "S1",
                                "image_gen_prompt": own}, SB)
    assert "Setting:" not in p and "小林:" not in p
    assert l2.compose_shot_prompt({"emotion_beat": "卖点展示", "image_gen_prompt": "macro"}).endswith("macro")


@pytest.mark.parametrize("shot", [{"emotion_beat": b, "characters": c} for b in
                                  ("卖点展示", "信任背书", "对比实验", "开箱", "工艺细节", "开场钩子")
                                  for c in ([], ["C1"])])
def test_style_prefix_and_beat_style_never_conflict(shot):
    p = l2.compose_shot_prompt(shot)
    if p.startswith(l2.LIFESTYLE_PREFIX.strip()[:20]):
        assert "studio" not in p.replace("no studio", "") and "clean background" not in p
    else:
        assert "lived-in" not in p


def test_fallback_size():
    assert l2.fallback_size("1008x1792") == "1024x1536"
    assert l2.fallback_size("1792x1008") == "1536x1024"
    assert l2.fallback_size("1024x1024") == "1024x1024" and l2.fallback_size("bad") == "1024x1024"
    for plat in l1.PLATFORMS.values():                         # 自定义尺寸必须是 16 的倍数
        assert all(int(v) % 16 == 0 for v in plat["image_size"].split("x"))


def test_generate_image_falls_back_on_size_error(monkeypatch, tmp_path):
    sizes = []

    class Resp:
        def __init__(self, code, text="", body=None):
            self.status_code, self.text, self._body = code, text, body

        def json(self):
            return self._body

    def post(url, headers, json, timeout):
        sizes.append(json["size"])
        if len(sizes) == 1:
            return Resp(400, '{"error": "Invalid size 1008x1792"}')
        return Resp(200, body={"data": [{"b64_json": "aGk="}]})
    monkeypatch.setattr(l2.requests, "post", post)
    out = l2._generate_image("p", str(tmp_path / "a.png"), "gpt-image-2.5", "1008x1792", "t")
    assert sizes == ["1008x1792", "1024x1536"] and (tmp_path / "a.png").read_bytes() == b"hi"


# ── 电商：视觉设定、AI 写字信息图、留白区、产品核对 ───────────────────────────
def test_art_direction_reaches_plan_and_prompt(monkeypatch):
    raw = {"theme_color": "#6B8E4E",
           "art_direction": {"light": "soft morning window light", "props": ["glass mug", "bamboo tray"]},
           "main_images": [{"headline": "白霜看得见", "scene_prompt_en": "tea on a desk"} for _ in range(5)],
           "detail_sections": [{"title": f"第{i}屏", "scene_prompt_en": "a doctor pours tea" if i == 3 else ""}
                               for i in range(15)]}
    monkeypatch.setattr(ecom, "_chat_json", lambda *a, **k: raw)
    plan = ecom.generate_ecom_plan("张家界莓茶", ["白霜"], "#6B8E4E", playbook="food")
    art = plan["meta"]["art_direction"]
    assert art == "light: soft morning window light; props: glass mug, bamboo tray"
    assert any("D4 画面风险「doctor」" in i for i in plan["quality_issues"])
    main = plan["main_images"][1]
    assert f"Shared art direction for the whole image set (keep consistent): {art}." in \
        ecom.build_edit_prompt(main, "main", 1, [], art_direction=art)
    white = plan["main_images"][-1]
    assert white["white_bg"] and "Shared art direction" not in ecom.build_edit_prompt(white, "main", 1, [],
                                                                                         art_direction=art)


def test_ecom_self_review_uses_weighted_acceptance(monkeypatch):
    def plan_json(headline):
        return {"main_images": [{"headline": headline} for _ in range(5)],
                "detail_sections": [{"title": f"第{i}屏"} for i in range(15)]}
    replies = iter([plan_json("白霜看得见"), plan_json("治疗咽炎")])
    monkeypatch.setattr(ecom, "_chat_json", lambda *a, **k: next(replies))
    plan = ecom.generate_ecom_plan("张家界莓茶", ["白霜"], "#6B8E4E", playbook="food", self_review=True)
    assert plan["meta"]["self_reviewed"] is False and plan["main_images"][0]["headline"] == "白霜看得见"


@pytest.mark.parametrize("item, expect, absent", [
    ({"layout": "steps", "title": "冲泡", "bullets": ["①取茶", "②冲水", "③出汤"]},
     ['3-step process infographic', 'step 1: "取茶"', 'step 3: "出汤"', "top 55%"], ["bullet list", "①"]),
    ({"layout": "grid", "title": "卖点", "bullets": ["云雾山地", "白霜", "回甘"]},
     ["icon feature grid with 3", "3 columns", '"白霜"'], ["bullet list"]),
    ({"layout": "compare", "title": "对比", "compare": {"headers": ["普通茶叶", "本品"],
                                                       "rows": [{"name": "洗茶", "left": "要", "right": "不用"}]}},
     ["comparison table", '"洗茶 | 要 | 不用"', 'the "本品" column is highlighted'], ["bullet list"]),
    ({"layout": "standard", "title": "参数", "specs": [{"name": "规格", "value": "100克"}]},
     ["specification table", "top 50%"], ["infographic"]),
])
def test_ai_text_block_follows_layout(item, expect, absent):
    text = "\n".join(ecom._ai_text_block(item, "detail", "#6B8E4E"))
    for s in expect:
        assert s in text, s
    for s in absent:
        assert s not in text, s


def _busy(size=(1440, 1920)):
    img = Image.new("RGB", size, (236, 230, 220))
    d = ImageDraw.Draw(img)
    for x in range(0, size[0], 24):
        d.line([(x, 0), (x + 300, size[1])], fill=(30, 30, 30), width=4)
    return img


def test_clutter_warning():
    item = {"title": "三步泡好一杯"}
    clean = Image.new("RGB", (1440, 1920), (236, 230, 220))
    assert ecom.text_zone_clutter("detail", item, clean) < 1
    assert ecom.clutter_warning("detail", item, clean) == ""
    assert "文字区背景较杂" in ecom.clutter_warning("detail", item, _busy())
    assert ecom.clutter_warning("detail", {"title": "t", "base_mode": "ai"}, _busy()) == ""
    assert ecom.clutter_warning("main", {"white_bg": True}, _busy()) == ""
    gradient = Image.linear_gradient("L").resize((1440, 1920)).convert("RGB")
    assert ecom.clutter_warning("detail", item, gradient) == ""


def test_compose_adds_clutter_warning(monkeypatch, tmp_path):
    base = tmp_path / "D1_base.png"
    _busy().save(base)
    monkeypatch.setattr(ecom, "RENDERER", "pil")
    item = {"id": "D1", "title": "三步泡好一杯", "base_path": str(base), "ratio": "3:4"}
    try:
        ecom.compose_detail(item, (107, 142, 78), tmp_path / "D1.jpg")
    except FileNotFoundError:
        pytest.skip("没有中文字体")
    assert any("文字区背景较杂" in w for w in item["layout_warnings"])


def test_product_fidelity_check(monkeypatch, tmp_path):
    ref, final = tmp_path / "ref.png", tmp_path / "final.jpg"
    Image.new("RGB", (64, 64), (200, 30, 30)).save(ref)
    Image.new("RGB", (64, 64), (30, 30, 200)).save(final)
    sent = []

    def chat(system, user, **kw):
        sent.append((system, user, kw))
        return {"match": False, "problems": ["包装主色是蓝色，实拍图是红色"]}
    monkeypatch.setattr(ecom, "_chat_json", chat)
    item = {"id": "M1", "final_path": str(final)}
    result = ecom.check_product_fidelity(item, [str(ref)])
    assert result == {"match": False, "problems": ["包装主色是蓝色，实拍图是红色"]} and item["fidelity"] == result
    images = [c for c in sent[0][1] if c["type"] == "image_url"]
    assert len(images) == 2 and images[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert sent[0][2]["model"] == ecom.CHECK_MODEL and sent[0][2]["temperature"] == 0.0
    assert "可能不一致" in ecom.fidelity_note(item)

    monkeypatch.setattr(ecom, "_chat_json", lambda *a, **k: {"match": True, "problems": []})
    assert ecom.check_product_fidelity(item, [str(ref)])["match"] is True and ecom.fidelity_note(item) == ""

    def boom(*a, **k):
        raise Exception("model does not support images")
    monkeypatch.setattr(ecom, "_chat_json", boom)
    failed = ecom.check_product_fidelity({"id": "M2", "final_path": str(final)}, [str(ref)])
    assert failed["match"] is None and "核对失败" in failed["problems"][0]
    assert ecom.check_product_fidelity({"id": "M3"}, [str(ref)])["match"] is None