"""生成质量：类目手册、平台联动、输入体检、硬校验和模型自检（LLM 调用全部用替身，不联网）"""

import json

import pytest

import category_playbook as cp
import ecom_images as ecom
import layer1_planning as l1
import product_brief as brief_mod
import quality_check as qc

PET = {"name": "猫静宝", "category": "宠物营养补充剂",
       "description": "原料组成：纯粮提取物、鸡肝粉。猫：口服，每天 2 片。生产许可证：鄂饲预（2023）12001"}


# ── 类目手册 ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("brief, key", [
    (PET, "pet_supplement"),                                                # 营养补充剂走商用标准包，优先于保健
    ({"category": "宠物用品", "name": "猫砂 10L"}, "pet"),
    ({"category": "美妆护肤 · 精华"}, "beauty"),
    ({"category": "3C · 剃须刀"}, "digital"),
    ({"category": "", "name": "婴儿纸尿裤 L 码"}, "baby"),
    ({"category": "休闲零食", "description": "坚果"}, "food"),
    ({"category": "工业轴承"}, cp.GENERAL),
    ({"category": "美妆护肤", "playbook": "food"}, "food"),                  # 手动选择优先
])
def test_resolve_playbook(brief, key):
    assert cp.resolve(brief) == key


def test_detail_roles_keep_length_and_override():
    roles = cp.detail_roles(ecom.DETAIL_ROLES, "pet")
    assert len(roles) == len(ecom.DETAIL_ROLES)
    assert roles[11].startswith("喂养方法") and roles[0] == ecom.DETAIL_ROLES[0]
    assert cp.detail_roles(ecom.DETAIL_ROLES, cp.GENERAL) == ecom.DETAIL_ROLES


def test_prompt_block_general_is_empty():
    assert cp.prompt_block(cp.GENERAL, "video") == ""
    block = cp.prompt_block("pet", "ecom")
    assert "宠物" in block and "图文要点" in block and "类目合规" in block


def test_missing_required():
    assert cp.missing_required(PET, "pet") == []
    names = [n for n, _ in cp.missing_required({"category": "宠物"}, "pet")]
    assert {"用法用量", "资质"} <= set(names)


# ── 合规初筛 ──────────────────────────────────────────────────────────────────
def test_category_compliance_only_with_playbook():
    text = "中药成分，预防疾病，安神助眠"
    assert not any(i.startswith("类目风险") for i in qc.check_compliance(text))
    issues = qc.check_compliance(text, "pet")
    assert "类目风险「中药成分」" in issues and "类目风险「预防」" in issues


def test_compliance_dedup_and_placeholders():
    issues = qc.check_compliance("第一 第一 【券后价】")
    assert issues == ["禁用词「第一」", "未填写占位【券后价】"]
    assert qc.check_compliance("【券后价】", placeholders=False) == []
    # ecom 的旧接口仍可用
    assert ecom.check_compliance("第一") == ["禁用词「第一」"]


# ── 平台 ──────────────────────────────────────────────────────────────────────
def test_platform_duration_and_voiceover_limit():
    assert l1.platform_duration("视频号（竖版 9:16）") == (6.0, 8.0)
    assert l1.voiceover_limit("抖音/快手（竖版 9:16）") == 20
    assert l1.voiceover_limit("视频号（竖版 9:16）") > l1.voiceover_limit("抖音/快手（竖版 9:16）")


def test_template_conflict():
    assert l1.template_conflict("淘宝/京东 主图视频（方形 1:1）", "经典反转 · 4镜")
    assert l1.template_conflict("淘宝/京东 主图视频（方形 1:1）", "商品展示 · 4镜") == ""
    for p in l1.PLATFORMS.values():
        assert all(t in l1.STORY_TEMPLATES for t in p["templates"])


def test_finalize_clamps_to_platform_duration():
    sb = {"shots": [{"duration_sec": 12, "voiceover": "短"}, {"duration_sec": 5, "voiceover": "字" * 40}]}
    out = l1._finalize(sb, 5.0, 6.0)
    assert out["shots"][0]["duration_sec"] == 6.0               # 平台上限
    assert out["shots"][1]["duration_sec"] == 10.8              # 口播太长：按字数拉长，不截断台词


# ── 输入体检 ──────────────────────────────────────────────────────────────────
def test_check_brief():
    issues = qc.check_brief({"name": "", "description": "好用"}, cp.GENERAL)
    assert any("商品名称" in i for i in issues) and any("产品描述只有" in i for i in issues)
    pet = qc.check_brief({**PET, "reviews": "很好", "audience": "养猫人"}, "pet")
    assert not any(i.startswith(qc.ERR) for i in pet)
    assert not any("类目建议写明" in i for i in pet)


def test_brief_reviews_and_playbook_reach_prompts():
    brief = {**PET, "reviews": "我家猫晚上一直叫", "playbook": cp.AUTO}
    text = brief_mod._input_text(brief)
    assert "我家猫晚上一直叫" in text and "【类目手册：宠物营养补充剂" in text
    assert "买家原话" in brief_mod.brief_context(brief)
    assert set(brief_mod.INPUT_FIELDS) >= {"reviews", "playbook"}


# ── 剧本硬校验 ────────────────────────────────────────────────────────────────
def _sb(vos, beats=("开场钩子", "行动召唤"), full_text=""):
    return {"script": {"full_text": full_text},
            "shots": [{"id": i, "voiceover": v, "emotion_beat": b, "duration_sec": 5.5, "visual_intent": ""}
                      for i, (v, b) in enumerate(zip(vos, beats), 1)]}


def test_check_storyboard_flags_problems():
    sb = _sb(["吃了它预防疾病", "吃了它预防疾病"], beats=("开场钩子", "卖点展示"))
    issues = qc.check_storyboard(sb, ["鸡肝风味不用掰嘴"], beats=["开场钩子", "行动召唤", "卖点展示"],
                                 vo_limit=6, duration_range=(5.0, 6.0), playbook="pet")
    text = "\n".join(issues)
    assert "模板要求 3 个镜头，实际 2 个" in text
    assert "模板要求「行动召唤」" in text
    assert "超过平台建议的 6 字" in text
    assert "口播重复" in text
    assert "卖点没有在剧本里体现：鸡肝风味不用掰嘴" in text
    assert f"{qc.ERR} 口播类目风险「预防」" in issues


def test_check_storyboard_clean():
    sb = _sb(["半夜又嚎了一整晚", "鸡肝风味，它自己吃不用掰嘴"])
    assert qc.check_storyboard(sb, ["鸡肝风味，不用掰嘴"], beats=["开场钩子", "行动召唤"], vo_limit=20,
                               duration_range=(5.0, 6.0), playbook="pet") == []


@pytest.mark.parametrize("usp, hay, ok", [
    ("鸡肝风味，喂片不用掰嘴", "鸡肝风味它自己吃，不用掰嘴", True),
    ("持证生产可查（鄂饲预）", "生产许可证可查", False),
    ("持证生产可查（鄂饲预）", "持证生产，随时可查", True),            # 括号里的补充说明不参与比对
])
def test_covers(usp, hay, ok):
    assert qc.covers(usp, hay) is ok


# ── 图文硬校验 ────────────────────────────────────────────────────────────────
def test_check_ecom_plan():
    plan = {"main_images": [
        {"id": "M1", "headline": "发情期陪它安稳过渡了呀", "subline": "", "badges": ["植物配伍超级安心"]},
        {"id": "M2", "headline": "发情期陪它安稳过渡了呀", "badges": []},
        {"id": "M5", "white_bg": True, "headline": ""},
    ], "detail_sections": [{"id": "D1", "title": "", "body": "正文" * 30, "bullets": []}]}
    text = "\n".join(qc.check_ecom_plan(plan, ["配料表透明"]))
    assert "M1 主标题 11 字，建议 ≤ 10 字" in text
    assert "卖点标签「植物配伍超级安心」超过 6 字" in text
    assert "主图标题重复：M1/M2" in text
    assert "D1 没有标题" in text and "D1 正文 60 字" in text
    assert "M5" not in text                                         # 白底图不检查文案
    assert "卖点没有出现在任何一张图上：配料表透明" in text


# ── 模型自检（LLM 用替身）────────────────────────────────────────────────────
def _storyboard_json(vo2):
    shots = [("半夜又嚎了", "开场钩子"), (vo2, "使用场景"), ("鸡肝风味好入口", "卖点展示"), ("今晚安静了", "行动召唤")]
    return json.dumps({"script": {"title": "t", "full_text": "鸡肝风味"}, "product_refs": [], "shots": [
        {"id": i, "voiceover": v, "emotion_beat": b, "mode": "T2VA", "duration_sec": 5}
        for i, (v, b) in enumerate(shots, 1)]}, ensure_ascii=False)


@pytest.fixture
def fake_chat(monkeypatch):
    """替换 layer1 的 LLM 调用：依次返回 replies，记录每次的 messages"""
    calls = []

    def install(*replies):
        def chat(messages, temperature=0.7):
            calls.append(messages)
            return replies[len(calls) - 1], "stop"
        monkeypatch.setattr(l1, "_chat", chat)
        return calls
    return install


def test_storyboard_self_review_accepts_better_revision(fake_chat):
    calls = fake_chat(_storyboard_json("吃了预防疾病"), _storyboard_json("喂片不再像打仗"))
    sb = l1.generate_storyboard("猫静宝", ["鸡肝风味"], playbook="pet", self_review=True)
    assert len(calls) == 2
    review = calls[1][-1]["content"]
    assert "【评分标准】" in review and "类目风险「预防」" in review     # 程序发现的问题交给模型改
    assert calls[1][-2]["role"] == "assistant"
    assert sb["meta"]["self_reviewed"] is True and sb["quality_issues"] == []
    assert sb["shots"][1]["voiceover"] == "喂片不再像打仗"


def test_storyboard_self_review_keeps_draft_when_worse(fake_chat):
    worse = json.dumps({"shots": [{"id": 1, "voiceover": "第一名"}]}, ensure_ascii=False)
    fake_chat(_storyboard_json("喂片不再像打仗"), worse)
    sb = l1.generate_storyboard("猫静宝", ["鸡肝风味"], self_review=True)
    assert sb["meta"]["self_reviewed"] is False and len(sb["shots"]) == 4


def test_storyboard_self_review_failure_keeps_draft(monkeypatch):
    replies = iter([(_storyboard_json("喂片不再像打仗"), "stop")])

    def chat(messages, temperature=0.7):
        try:
            return next(replies)
        except StopIteration:
            raise Exception("网关 502") from None
    monkeypatch.setattr(l1, "_chat", chat)
    sb = l1.generate_storyboard("猫静宝", ["鸡肝风味"], self_review=True)
    assert sb["meta"]["self_reviewed"] is False and len(sb["shots"]) == 4


def test_storyboard_platform_and_playbook_reach_prompt(fake_chat):
    calls = fake_chat(_storyboard_json("喂片不再像打仗"))
    sb = l1.generate_storyboard("猫静宝", ["鸡肝风味"], target_platform="视频号（竖版 9:16）", playbook="pet")
    assert len(calls) == 1                                          # 不开自检只调用一次
    prompt = calls[0][1]["content"]
    assert "单镜时长：6–8 秒" in prompt and "【类目手册：宠物用品" in prompt
    assert sb["meta"]["playbook"] == "pet" and sb["shots"][0]["duration_sec"] == 6.0


def test_ecom_plan_uses_playbook_and_self_review(monkeypatch):
    def plan_json(headline):
        return {"theme_color": "#B5651D",
                "main_images": [{"headline": headline, "subline": "鸡肝风味"} for _ in range(5)],
                "detail_sections": [{"title": f"第{i}屏", "body": "正文"} for i in range(15)]}

    calls = []

    def chat(system, user, max_tokens=12000, history=None, temperature=0.7):
        calls.append((user, history))
        return plan_json("预防疾病" if len(calls) == 1 else "安稳过渡")
    monkeypatch.setattr(ecom, "_chat_json", chat)
    plan = ecom.generate_ecom_plan("猫静宝", ["鸡肝风味"], "#C8A882", playbook="pet", self_review=True)
    assert "喂养方法" in calls[0][0] and "【类目手册：宠物用品" in calls[0][0]
    assert plan["detail_sections"][11]["role"].startswith("喂养方法")
    assert "类目风险「预防」" in calls[1][1][-1]["content"]
    assert plan["meta"]["self_reviewed"] is True and plan["meta"]["playbook"] == "pet"
    assert plan["main_images"][0]["headline"] == "安稳过渡"
