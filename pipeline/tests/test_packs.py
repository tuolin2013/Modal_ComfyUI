"""产品定位（平台 × 定位决定屏序、语气、画面、价格）+ 商用标准包（代用茶、宠物营养补充剂）"""

import json

import pytest

import asset_library as al
from categories import pet_supplement as pet_pack
from categories import tea as tea_pack
import category_playbook as cp
import ecom_images as ecom
import layer1_planning as l1
import positioning as pos
import quality_check as qc

TEA = {"name": "张家界莓茶", "category": "代用茶",
       "description": "产自张家界武陵山区。原料为显齿蛇葡萄嫩茎叶，95℃ 开水直接冲泡，一次 3-5 克。净含量 100 克/罐。"}
PET = {"name": "猫静宝", "category": "宠物营养补充剂",
       "description": "宠物营养补充剂（猫）。原料组成：纯粮提取物、鸡肝粉。猫每天 2 片。生产许可证：鄂饲预（2023）12001"}


# ── 定位 ──────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("choice, platform, key", [
    ("auto", "拼多多", "value"), ("auto", "微信小店", "premium"), ("auto", "视频号（竖版 9:16）", "premium"),
    ("auto", "淘宝/天猫", "daily"), ("", "抖音/快手（竖版 9:16）", "daily"),
    ("premium", "拼多多", "premium"),                     # 手动指定优先于平台默认
    ("不存在", "拼多多", "value"),
])
def test_resolve_positioning(choice, platform, key):
    assert pos.resolve(choice, platform) == key


@pytest.mark.parametrize("key", list(pos.POSITIONINGS))
def test_generic_orders_have_15_screens(key):
    roles = pos.detail_roles(ecom.DETAIL_ROLES, key)
    assert len(roles) == 15 and len(set(roles)) == 15


def test_orders_differ_by_positioning():
    value, daily, premium = (pos.detail_roles(ecom.DETAIL_ROLES, k) for k in ("value", "daily", "premium"))
    assert daily == ecom.DETAIL_ROLES
    assert value[1].startswith("价值量感") and premium[1].startswith("品牌故事")
    assert not any(r.startswith(("痛点", "对比")) for r in premium)      # 高端不靠痛点和比较


def test_prompt_block_and_price_policy():
    assert "【券后价】" in pos.prompt_block("value", "ecom") and "不放价格" in pos.prompt_block("premium", "ecom")
    assert "纪录片" in pos.prompt_block("premium", "video")
    mains = [{"price": "【券后价】", "price_label": "券后价"}]
    assert pos.apply_price_policy([dict(m) for m in mains], "premium")[0]["price"] == ""
    assert pos.apply_price_policy([dict(m) for m in mains], "value")[0]["price"] == "【券后价】"


def _plan_raw():
    return {"theme_color": "#6B8E4E", "art_direction": "pale oak tabletop",
            "main_images": [{"headline": "张家界莓茶", "price": "【券后价】", "price_label": "券后价"}
                            for _ in range(5)],
            "detail_sections": [{"title": f"第{i}屏"} for i in range(15)]}


def test_ecom_plan_follows_platform_positioning(monkeypatch):
    seen = []
    monkeypatch.setattr(ecom, "_chat_json", lambda system, user, **k: seen.append(user) or _plan_raw())
    pdd = ecom.generate_ecom_plan("张家界莓茶", ["白霜"], "#6B8E4E", "拼多多", playbook="tea", library=[])
    wx = ecom.generate_ecom_plan("张家界莓茶", ["白霜"], "#6B8E4E", "微信小店", playbook="tea", library=[])
    assert pdd["meta"]["positioning"] == "value" and wx["meta"]["positioning"] == "premium"
    assert [d["role"] for d in pdd["detail_sections"]] == tea_pack.PLANS["value"]
    assert [d["role"] for d in wx["detail_sections"]] == tea_pack.PLANS["premium"]
    assert "【产品定位：性价比】" in seen[0] and "【产品定位：高端礼赠】" in seen[1]
    # 高端：程序清掉主图价格，并把定位的画面方向拼进整套视觉设定
    assert all(m["price"] == "" for m in wx["main_images"]) and pdd["main_images"][0]["price"] == "【券后价】"
    assert "pale oak tabletop" in wx["meta"]["art_direction"] and "negative space" in wx["meta"]["art_direction"]
    manual = ecom.generate_ecom_plan("张家界莓茶", ["白霜"], "#6B8E4E", "拼多多", playbook="tea",
                                     library=[], positioning="premium")
    assert manual["meta"]["positioning"] == "premium"          # 手动指定覆盖平台默认


def test_ecom_plan_generic_category_uses_generic_order(monkeypatch):
    monkeypatch.setattr(ecom, "_chat_json", lambda *a, **k: _plan_raw())
    plan = ecom.generate_ecom_plan("某精华", ["保湿"], "#C8A882", "拼多多", playbook="beauty", library=[])
    roles = [d["role"] for d in plan["detail_sections"]]
    assert roles == pos.detail_roles(cp.detail_roles(ecom.DETAIL_ROLES, "beauty"), "value")


def test_storyboard_gets_positioning_and_evidence_check(monkeypatch):
    shots = [{"id": 1, "duration_sec": 6, "mode": "T2VA", "ref_needed": None, "emotion_beat": "钩子",
              "voiceover": "有机野生莓茶，黄酮含量 30%", "image_gen_prompt": "a cup of tea"}]
    seen = []

    def chat(messages, temperature=0.7):
        seen.append(messages[1]["content"])
        return json.dumps({"product_refs": [], "shots": shots}, ensure_ascii=False), "stop"
    monkeypatch.setattr(l1, "_chat", chat)
    sb = l1.generate_storyboard("张家界莓茶", ["白霜"], target_platform="视频号（竖版 9:16）", playbook="tea",
                                product_context=TEA["description"])
    assert sb["meta"]["positioning"] == "premium" and "【产品定位：高端礼赠】" in seen[0]
    text = "\n".join(sb["quality_issues"])
    assert "口播写了「有机」" in text and "口播写了「野生」" in text and "检测报告" in text

# ── 商用标准包：注册、继承、识别 ─────────────────────────────────────────────────
def test_packs_registered_and_inherit_parent():
    for key, parent in (("tea", "food"), ("pet_supplement", "pet")):
        pb = cp.get(key)
        assert key in cp.BUILTIN_KEYS and pb["parent"] == parent and cp.is_standard(key)
        assert set(cp.PLAYBOOKS[parent]["compliance"]) <= set(pb["compliance"])     # 合规只增不减
        assert {r[0] for r in cp.PLAYBOOKS[parent]["required"]} <= {r[0] for r in pb["required"]}
        assert any(v == key for _, v in cp.choices())
    assert cp.DETECT_ORDER.index("tea") < cp.DETECT_ORDER.index("food")
    assert cp.DETECT_ORDER.index("pet_supplement") < cp.DETECT_ORDER.index("pet")
    assert not cp.is_standard("food")


@pytest.mark.parametrize("key", list(cp.STANDARD_KEYS))
def test_pack_plans_are_complete(key):
    pb = cp.get(key)
    assert set(pb["positioning_plans"]) == set(pos.POSITIONINGS)
    for plan in pb["positioning_plans"].values():
        assert len(plan) == 15 and len(set(plan)) == 15
    assert all(t in al.PHOTO_TYPES for t, _, _ in pb["photo_checklist"])
    assert any(required for _, _, required in pb["photo_checklist"])


EXPECTED_PHOTO = {"冲泡方法": "in_use", "喂养方法": "in_use", "冲泡仪式": "in_use", "资质证据": "certificate",
                  "品质证据": "certificate", "规格参数": "package_back", "原料与产地": "raw_material",
                  "叶底特写": "texture_detail", "适口性": "texture_detail", "产品特写": "texture_detail"}


@pytest.mark.parametrize("role", sorted({r for plans in (tea_pack.PLANS, pet_pack.PLANS)
                                         for plan in plans.values() for r in plan}))
def test_pack_screens_route_to_right_photos(role):
    """标准包的屏用词要能被素材分配规则认出来：冲泡屏找冲泡照片，资质屏找证书，规格屏找包装背面"""
    head = role.split("：")[0]
    if head in EXPECTED_PHOTO:
        assert al.role_need(role)["types"][0] == EXPECTED_PHOTO[head], role
    if head.startswith("首屏"):
        assert al.role_need(role)["types"][0] == "package_front", role

# ── 标签必备项、照片清单、需要依据的说法 ───────────────────────────────────────────
def test_tea_label_items_and_photos():
    issues = "\n".join(qc.check_standard_inputs(TEA, "tea", photo_types={"package_front"}))
    assert "GB 7718-2025" in issues
    for missing in ("配料表", "食品生产许可证", "产品标准代号", "贮存条件"):
        assert missing in issues
    assert "净含量 / 规格（" not in issues                       # 描述里写了净含量
    assert "包装背面" in issues and "冲泡中" in issues           # 必拍照片还缺
    full = {**TEA, "description": TEA["description"] + "配料：显齿蛇葡萄嫩茎叶。湖南某某茶业有限公司生产。"
                                                       "生产日期见罐底，保质期 24 个月。阴凉干燥处贮存。"
                                                       "食品生产许可证 SC11443080000001，执行标准 GB/T 31748"}
    assert not any("标签必备项" in i for i in qc.check_standard_inputs(full, "tea"))
    # 包装背面识别出的标签也算
    label = "配料：藤茶\n贮存：阴凉干燥\n生产许可证：SC123456789\n执行标准：Q/ABC 1-2024\n生产商：某公司\n保质期：24个月"
    assert not any("标签必备项" in i for i in qc.check_standard_inputs(TEA, "tea", label_text=label))
    assert qc.check_standard_inputs(TEA, "food") == []          # 普通食品手册不查


def test_pet_label_items_follow_feed_rules():
    issues = "\n".join(qc.check_standard_inputs(PET, "pet_supplement"))
    assert "宠物饲料标签规定" in issues
    for missing in ("成分分析保证值", "注意事项", "卫生规定字样", "产品标准编号"):
        assert missing in issues
    assert "饲料生产许可证（" not in issues and "通用名称（" not in issues


def test_check_brief_includes_standard_inputs():
    report = "\n".join(qc.check_brief(TEA, "tea", photo_types=set()))
    assert "标签必备项" in report and "建议上传的实拍还缺" in report


@pytest.mark.parametrize("claim", ["降压茶", "减肥茶", "无添加", "不添加防腐剂", "不使用任何添加剂",
                                   "调理肠胃", "调节血糖", "喝出健康"])
def test_tea_extra_compliance(claim):
    assert any(i.startswith("类目风险") for i in qc.check_compliance(claim, "tea", placeholders=False)), claim


@pytest.mark.parametrize("text", ["开封后不使用时请密封", "开水直接冲，不用洗茶", "回味甘甜", "叶片自带白霜"])
def test_tea_factual_copy_not_flagged(text):
    assert qc.check_compliance(text, "tea", placeholders=False) == [], text


@pytest.mark.parametrize("claim", ["驱虫", "缓解关节炎", "增强免疫力", "抗焦虑", "兽医推荐", "预防尿结石", "安神助眠"])
def test_pet_supplement_compliance(claim):
    assert qc.check_compliance(claim, "pet_supplement", placeholders=False), claim


def test_unsupported_claims_need_source():
    hits = [h for h, _ in cp.unsupported_claims("有机野生莓茶，黄酮含量 30%，特级", TEA["description"], "tea")]
    assert {"有机", "野生", "特级", "黄酮"} <= set(hits) and any("30" in h for h in hits)
    # 产品信息里有依据（如有机认证）就不提醒
    assert "有机" not in [h for h, _ in cp.unsupported_claims("有机莓茶", "有机产品认证证书编号 123", "tea")]
    assert cp.unsupported_claims("天然原料", "", "pet_supplement")
    assert cp.unsupported_claims("有机野生", "", "food") == []          # 普通手册不查


def test_prompt_block_lists_label_items():
    block = cp.prompt_block("tea", "ecom")
    assert "规格参数屏按标签必备项列" in block and "食品生产许可证" in block and "【占位】" in block
    assert "检测报告" in cp.prompt_block("tea", "video")
    assert "规格参数屏" not in cp.prompt_block("tea", "video")

# ── 自定义手册也能写标准包字段（以后加类目） ─────────────────────────────────────────
def test_custom_playbook_with_pack_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "CUSTOM_DIR", tmp_path / "playbooks")
    plan = [f"第{i}屏：产品亮相" for i in range(1, 16)]
    key = cp.save_custom({
        "label": "蜂蜜", "parent": "food", "keywords": ["蜂蜜", "土蜂蜜"],
        "label_items": [["配料表", "配料", "配料表"], ["坏正则", "([", "丢掉"]],
        "photo_checklist": [["package_front", "包装正面", True]],
        "evidence": [["成熟蜜", "要有检测依据"]],
        "positioning_plans": {"premium": plan, "value": plan[:3]},   # 不够 15 屏的整套丢掉
    })
    pb = cp.get(key)
    assert cp.is_standard(key) and [n for n, *_ in pb["label_items"]] == ["配料表"]
    assert set(pb["positioning_plans"]) == {"premium"}
    assert pos.detail_roles(ecom.DETAIL_ROLES, "premium", pb["positioning_plans"]) == plan
    assert cp.unsupported_claims("成熟蜜", "", key)
    again = cp.save_custom(json.loads(cp.custom_to_text(key)))       # 界面编辑框能原样存回去
    assert again == key and cp.get(key)["label_items"] == pb["label_items"]
    # 以标准包为 parent：继承标签必备项；自己改了屏序时不沿用 parent 的整套定位屏序
    child = cp.save_custom({"label": "菊花茶", "parent": "tea", "keywords": ["菊花茶"],
                            "detail_overrides": {"5": "花朵特写：一朵朵完整的菊花"}})
    cpb = cp.get(child)
    assert cpb["label_items"] == cp.get("tea")["label_items"] and cpb["positioning_plans"] == {}
    assert set(cp.get("tea")["compliance"]) <= set(cpb["compliance"])


# ── 评测集：主图 / 详情页用例（LLM 用替身） ─────────────────────────────────────────
def test_ecom_eval_cases_run(monkeypatch):
    from evals import run_eval
    cases = run_eval.load_cases(key="ecom_cases")
    assert {c["id"] for c in cases} >= {"berry-tea-pdd", "berry-tea-wechat", "cat-pdd", "cat-wechat"}
    assert all(c["brief"]["name"] for c in cases)
    assert {cp.resolve(c["brief"]) for c in cases} == {"tea", "pet_supplement"}
    monkeypatch.setattr(ecom, "_chat_json", lambda *a, **k: _plan_raw())
    results = [run_eval.run_ecom_case(c) for c in cases]
    assert all("error" not in r for r in results), results
    by_id = {r["id"]: r for r in results}
    assert by_id["berry-tea-pdd"]["positioning"] == "value" and by_id["cat-wechat"]["positioning"] == "premium"
    # 定位、高端无价格、标准包屏序都对上，程序不会额外加 ❌
    assert not any("定位是" in i or "出现了价格" in i or "屏序与类目标准包" in i
                   for r in results for i in r["issues"])
    assert "4/4 条成功" in run_eval.summarize(results)
