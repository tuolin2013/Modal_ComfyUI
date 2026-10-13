"""
扩展能力：自定义类目手册、视频类型与自定义节拍、H3 提示词结构校验、评测集
（LLM 调用全部用替身，不联网；自定义手册写到临时目录）
"""

import json

import pytest

import category_playbook as cp
import layer1_planning as l1
import layer3_h3_prompt as l3
import product_brief as brief_mod
import quality_check as qc
from evals import run_eval

TEA = {"name": "张家界莓茶", "category": "代用茶", "description": "张家界显齿蛇葡萄嫩茎叶，开水冲泡"}
TEA_PLAYBOOK = {
    "label": "代用茶", "parent": "food", "keywords": ["莓茶", "藤茶"],
    "focus": "产地、原料、口感、冲泡方式、安全性",
    "required": [{"name": "冲泡方式", "pattern": "冲泡|泡", "tip": "写清用量和水温"},
                 {"name": "坏正则", "pattern": "([", "tip": "会被丢掉"}],
    "detail_overrides": {"5": "产地溯源：张家界山区环境", "99": "越界会被丢掉"},
    "compliance": ["降压茶|消炎", "(坏正则"],
}


@pytest.fixture
def custom_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "CUSTOM_DIR", tmp_path / "playbooks")
    return tmp_path / "playbooks"


# ── 内置手册补漏 ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("brief, key", [
    (TEA, "tea"),                                           # 代用茶走商用标准包（parent 是食品）
    ({"category": "花草茶"}, "tea"),
    ({"category": "零食", "name": "奶茶味饼干"}, "food"),     # 只是「茶」字不算代用茶
    ({"category": "家居", "name": "实木茶几"}, "home"),       # 茶几不是食品
])
def test_tea_detected_as_food(custom_dir, brief, key):
    assert cp.resolve(brief) == key
    if key == "tea":
        assert cp.get(key)["parent"] == "food"


def test_food_compliance_covers_tea_claims():
    issues = qc.check_compliance("清热解毒，护肝降三高", "food", placeholders=False)
    assert "类目风险「清热解毒」" in issues and "类目风险「护肝」" in issues and "类目风险「降三高」" in issues


# ── 自定义类目手册 ─────────────────────────────────────────────────────────────
def test_normalize_drops_bad_regex_and_out_of_range_screens():
    pb = cp.normalize_custom(TEA_PLAYBOOK)
    assert [r[0] for r in pb["required"]] == ["冲泡方式"]
    assert pb["detail_overrides"] == {5: "产地溯源：张家界山区环境"}
    assert pb["compliance"] == ["降压茶|消炎"]
    assert pb["keywords"] == ["莓茶", "藤茶", "代用茶"]       # 名称本身也是关键词
    with pytest.raises(ValueError):
        cp.normalize_custom({"label": " "})


def test_custom_save_detect_and_inherit(custom_dir):
    key = cp.save_custom(TEA_PLAYBOOK)
    assert key == "custom:代用茶" and (custom_dir / "代用茶.json").exists()
    # 自定义手册先于内置手册识别（否则会被「茶」归到食品）
    assert cp.resolve(TEA) == key
    assert cp.resolve({"name": "湖南藤茶 100g"}) == key
    assert any(v == key for _, v in cp.choices())

    pb = cp.get(key)
    assert pb["parent"] == "food" and pb["label"] == "代用茶"
    # 合规只增不减：继承食品的风险词（降血糖），再加自己的（降压茶）
    issues = qc.check_compliance("降血糖，还是降压茶", key, placeholders=False)
    assert "类目风险「降血糖」" in issues and "类目风险「降压茶」" in issues
    assert "类目风险「降压茶」" not in qc.check_compliance("降压茶", "food", placeholders=False)
    # 必填项叠加在 parent 之上；空着的文字字段沿用 parent
    assert "冲泡方式" in [n for n, *_ in pb["required"]]
    assert pb["video"] == cp.PLAYBOOKS["food"]["video"]
    assert "代用茶" in cp.prompt_block(key, "video")


def test_custom_roundtrip_and_delete(custom_dir):
    key = cp.save_custom(TEA_PLAYBOOK)
    again = cp.save_custom(json.loads(cp.custom_to_text(key)))   # 界面编辑框的内容能原样存回去
    assert again == key and cp.load_custom()[key]["detail_overrides"] == {5: "产地溯源：张家界山区环境"}
    assert cp.delete_custom(key) and not cp.delete_custom(key)
    assert cp.resolve({**TEA, "playbook": key}) == "tea"          # 被删的手册退回自动识别
    assert not cp.delete_custom("food")


def test_broken_custom_file_is_skipped(custom_dir):
    custom_dir.mkdir(parents=True)
    (custom_dir / "坏文件.json").write_text("{not json", encoding="utf-8")
    assert cp.load_custom() == {} and cp.resolve(TEA) == "tea"


def test_draft_playbook_uses_llm_and_normalizes(monkeypatch, custom_dir):
    seen = {}

    def fake_chat(system, user, **kw):
        seen["system"], seen["user"] = system, user
        return {**TEA_PLAYBOOK, "label": "", "parent": "不存在的类目"}

    monkeypatch.setattr(brief_mod, "chat_json", fake_chat)
    draft = brief_mod.draft_playbook(TEA, ["品牌首屏", "核心卖点"])
    assert draft["label"] == "代用茶" and draft["parent"] == cp.GENERAL   # 名称取类目，非法 parent 退回通用
    assert "food" in seen["system"] and "1. 品牌首屏" in seen["user"]
    assert not custom_dir.exists()                                        # 起草不落盘


# ── 视频类型与自定义节拍 ────────────────────────────────────────────────────────
def test_every_template_has_type_role_and_valid_beats():
    types = set()
    for name, t in l1.STORY_TEMPLATES.items():
        assert t["type"] and t["role"] and t["desc"], name
        assert l1.MIN_BEATS <= len(t["beats"]) <= l1.MAX_BEATS, name
        assert all(roll in l1.ROLL_TYPES for _, roll, _ in t["beats"]), name
        types.add(t["type"])
    assert len(types) >= 7
    for p in l1.PLATFORMS.values():
        assert all(t in l1.STORY_TEMPLATES for t in p["templates"])


def test_parse_custom_beats():
    beats = l1.parse_custom_beats(
        "1. 开场钩子 | A-Roll | 主播问：这层白霜是什么？\n\n"
        "卖点展示｜b-roll｜白霜微距\n"
        "行动召唤 | 说出适合谁喝")                      # 省略 roll：默认 A-Roll，第二段就是任务
    assert beats == [("开场钩子", "A-Roll", "主播问：这层白霜是什么？"),
                     ("卖点展示", "B-Roll", "白霜微距"),
                     ("行动召唤", "A-Roll", "说出适合谁喝")]
    with pytest.raises(ValueError):
        l1.parse_custom_beats("只有一行")
    with pytest.raises(ValueError):
        l1.parse_custom_beats("\n".join(f"节拍{i}" for i in range(7)))


def _storyboard_json(beats):
    return json.dumps({
        "product": {"name": "张家界莓茶", "usp": ["白霜"]},
        "script": {"title": "t", "characters": [], "scenes": [], "full_text": ""},
        "product_refs": [],
        "shots": [{"id": i + 1, "duration_sec": 6, "mode": "T2VA", "voiceover": "白霜看得见",
                   "emotion_beat": b, "visual_intent": "", "camera": "Static Shot", "image_gen_prompt": "",
                   "ref_needed": None, "characters": [], "scene": "S1"} for i, b in enumerate(beats)],
    }, ensure_ascii=False)


def test_generate_storyboard_injects_video_type_and_limits(monkeypatch):
    sent = []

    def fake_chat(messages, temperature=0.7):
        sent.append(messages)
        return _storyboard_json(["开场钩子", "产地溯源", "工艺细节", "卖点展示", "行动召唤"]), "stop"

    monkeypatch.setattr(l1, "_chat", fake_chat)
    sb = l1.generate_storyboard("张家界莓茶", ["白霜"], story_template="产地故事 · 5镜",
                                target_platform="视频号（竖版 9:16）")
    user = sent[0][1]["content"]
    assert "【视频类型】产地 / 品牌故事" in user and "纪录片导演" in user
    assert "不编造历史" in user and "视频模型能力边界" in user
    assert "麦格芬" not in sent[0][0]["content"]                   # 系统提示词不再写死「剧情软广」
    assert sb["meta"]["video_type"] == "产地 / 品牌故事"
    assert not any("节拍" in i for i in sb["quality_issues"])


def test_generate_storyboard_with_custom_beats(monkeypatch):
    monkeypatch.setattr(l1, "_chat", lambda m, temperature=0.7: (_storyboard_json(["问题", "答案"]), "stop"))
    sb = l1.generate_storyboard("张家界莓茶", ["白霜"], story_template=l1.CUSTOM_TEMPLATE,
                                custom_beats="问题 | A-Roll | 提问\n答案 | B-Roll | 特写")
    assert sb["meta"]["story_template"] == "自定义节拍"
    assert sb["meta"]["custom_beats"] == [["问题", "A-Roll", "提问"], ["答案", "B-Roll", "特写"]]


# ── H3 提示词结构校验 ────────────────────────────────────────────────────────────
BASE_OK = ("integrated_multimodal_description: [Shot 1] Live-action, a woman (S1) says: "
           "<d>[Chinese] 凌晨两点，邻居又来敲门了……</d> At 00:03.500 she looks up.\n\n"
           "overall_soundscape: quiet room tone.\n\nnon_diegetic_music: soft piano.")


def test_prompt_structure_ok():
    assert l3.check_prompt_structure(BASE_OK, False, "凌晨两点，邻居又来敲门了...", 5.17) == ""
    # 「留出字幕区」和画幅 9:16 不算问题
    ok = BASE_OK.replace("Live-action,", "Live-action 9:16, leaving space for subtitles,")
    assert l3.check_prompt_structure(ok, False, "", 5.17) == ""


@pytest.mark.parametrize("text, is_ref, vo, expect", [
    (BASE_OK.replace("non_diegetic_music: soft piano.", ""), False, "", "non_diegetic_music"),
    (BASE_OK, True, "", "subject_definitions"),
    (BASE_OK, False, "今天天气很好", "口播"),
    (BASE_OK.replace("she looks up", "on-screen text reads 买一送一"), False, "", "画面里出现文字"),
    (BASE_OK.replace("00:03.500", "00:07.000"), False, "", "超出"),
    ("overall_soundscape: a\n\nintegrated_multimodal_description: b\n\nnon_diegetic_music: c", False, "", "顺序"),
])
def test_prompt_structure_problems(text, is_ref, vo, expect):
    assert expect in l3.check_prompt_structure(text, is_ref, vo, 5.17)


def test_generate_h3_prompt_feeds_problem_back(monkeypatch):
    """不合格时把问题告诉模型重写，而不是原样重试"""
    calls = []

    class Resp:
        status_code = 200

        def __init__(self, content):
            self._content = content

        def json(self):
            return {"choices": [{"message": {"content": self._content}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json["messages"])
        return Resp(BASE_OK.replace("non_diegetic_music: soft piano.", "") if len(calls) == 1 else BASE_OK)

    monkeypatch.setattr(l3.requests, "post", fake_post)
    shot = {"id": 1, "mode": "T2VA", "duration_sec": 5, "voiceover": "凌晨两点，邻居又来敲门了……"}
    assert l3.generate_h3_prompt(shot) == BASE_OK
    assert len(calls) == 2 and "non_diegetic_music" in calls[1][-1]["content"]


def test_saved_prompts_pass_structure_check():
    """已生成的提示词（如果有）应该能通过结构校验：防止校验规则太严、把好的提示词也拦下"""
    sb_path = l3.Path(__file__).resolve().parents[1] / "output" / "storyboard.json"
    prompts = sb_path.parent / "prompts"
    if not sb_path.exists() or not prompts.exists():
        pytest.skip("没有已生成的提示词")
    sb = json.loads(sb_path.read_text(encoding="utf-8"))
    checked = 0
    for s in sb.get("shots", []):
        f = prompts / f"shot_{int(s['id']):02d}_h3_prompt.txt"
        if f.exists():
            text = f.read_text(encoding="utf-8")
            problem = l3.check_prompt_structure(text, s["mode"].upper() == "REF2VA", s.get("voiceover", ""),
                                                l3.rendered_seconds(s["duration_sec"]))
            # 「画面里写字」是真问题（旧提示词里确实有），其它问题说明规则误伤
            assert problem == "" or "画面里出现文字" in problem, (f.name, problem)
            checked += 1
    if not checked:
        pytest.skip("没有已生成的提示词")


# ── 评测集 ─────────────────────────────────────────────────────────────────────────
def test_eval_cases_are_valid():
    cases = run_eval.load_cases()
    assert len(cases) >= 4
    for c in cases:
        assert c["brief"]["name"] and c["brief"]["usp"], c["id"]
        assert c["platform"] in l1.PLATFORMS and c["template"] in l1.STORY_TEMPLATES, c["id"]
    assert run_eval.SKILL_PATH.exists()


def test_parse_judge():
    n = run_eval.RUBRIC_SIZE
    assert n == 7
    r = run_eval.parse_judge({"scores": [5, 9, 0] + [4] * (n - 3), "top_fix": "x"})
    assert r["scores"][:3] == [5, 5, 1] and r["avg"] == round((11 + 4 * (n - 3)) / n, 2)
    assert "error" in run_eval.parse_judge({"scores": [5]})


def test_run_case_offline(monkeypatch):
    case = next(c for c in run_eval.load_cases() if c["id"] == "berry-tea-douyin")
    monkeypatch.setattr(l1, "_chat", lambda m, temperature=0.7: (
        _storyboard_json([b[0] for b in l1.STORY_TEMPLATES[case["template"]]["beats"]]), "stop"))
    monkeypatch.setattr(brief_mod, "chat_json", lambda *a, **k: {"scores": [4] * run_eval.RUBRIC_SIZE})
    r = run_eval.run_case(case, use_judge=True)
    assert "error" not in r and r["judge"]["avg"] == 4.0
    assert "1/1 条成功" in run_eval.summarize([r])

# ── 功效宣称：莓茶资料里常见的说法 ─────────────────────────────────────────────────
TEA_CLAIMS = ["黄酮之王", "血管清道夫", "预防心脑血管疾病", "具药理性", "民族药物", "能治百病的神草", "药食同源",
              "祛病疗伤", "无任何毒副作用", "清咽利嗓", "祛痰止咳", "保肝护肝", "解酒醒酒", "降胆固醇", "清除自由基",
              "抑制肿瘤", "抗病毒", "急慢性咽炎", "改善并提高睡眠质量", "可送服中西药", "长寿藤", "土家神茶"]


@pytest.mark.parametrize("claim", TEA_CLAIMS)
def test_tea_claims_flagged_under_food(claim):
    assert qc.check_compliance(claim, "food", placeholders=False), claim


@pytest.mark.parametrize("text", ["张家界山里采的嫩茎叶", "叶片自带白霜，冲泡看得见", "回味甘甜",
                                  "不含咖啡因", "无需洗茶，第一泡就能喝", "土家族传统茶饮"])
def test_factual_tea_copy_not_flagged(text):
    assert qc.check_compliance(text, "food", placeholders=False) == []


def test_brief_check_warns_about_claims_in_input():
    brief = {**TEA, "description": TEA["description"] + "，降血压、消炎抑菌、抑制肿瘤，被誉为黄酮之王"}
    report = "\n".join(qc.check_brief(brief, "food"))
    assert "功效或医疗宣称" in report and "降血压" in report
    assert "功效或医疗宣称" not in "\n".join(qc.check_brief(TEA, "food"))