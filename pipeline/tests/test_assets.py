"""产品素材库：分类结果清洗、质量分、旧图位迁移、按屏分配素材、出图方式、拍法去重、雷同检查、实拍直出"""

import json

import pytest
from PIL import Image, ImageDraw

import asset_library as al
import ecom_images as ecom
import quality_check as qc

TEA_ROLES = [
    "首屏海报：品牌 + 一句核心利益",
    "痛点共鸣：说出目标用户的困扰",
    "产品亮相：产品就是解决方案",
    "原料产地：张家界山区野生藤茶",
    "核心卖点：一层白霜",
    "冲泡方法：3 步冲泡",
    "叶底特写：舒展的嫩叶",
    "品质证据：检测报告",
    "规格参数：规格、净含量、保质期",
    "售后保障与收口",
]


def _photo(path, color=(120, 160, 90), size=(1200, 1600), sharp=True, white=False):
    img = Image.new("RGB", size, "white" if white else color)
    d = ImageDraw.Draw(img)
    if sharp:   # 画面中间画密集的线条：边缘密度高 = 清晰；四周留边，白底检测看的是边框
        for x in range(size[0] // 8, size[0] * 7 // 8, 12):
            d.line((x, size[1] // 4, x, size[1] * 3 // 4), fill=(20, 20, 20), width=2)
    img.save(path)
    return str(path)


def _lib(tmp_path, types, quality=0.9):
    """直接构造素材库条目（不经过文件读写）"""
    out = []
    for i, t in enumerate(types, 1):
        p = tmp_path / f"P{i:02d}.png"
        if not p.exists():
            Image.new("RGB", (400, 500), (100 + i * 10, 140, 90)).save(p)
        out.append({"id": f"P{i:02d}", "file": p.name, "path": str(p), "type": t, "type_source": "user",
                    "note": "", "quality": {"score": quality, "white_bg": False}})
    return out


# ── 分类结果清洗 ──────────────────────────────────────────────────────────────
def test_normalize_type_aliases():
    assert al.normalize_type("package_front") == "package_front"
    assert al.normalize_type("brewed") == "in_use" and al.normalize_type("茶汤") == "in_use"
    assert al.normalize_type("包装背面 / 标签") == "package_back"
    assert al.normalize_type("BACK") == "package_back"
    assert al.normalize_type("whatever") == al.DEFAULT_TYPE and al.normalize_type(None) == al.DEFAULT_TYPE


def test_normalize_tags_handles_index_and_garbage():
    raw = {"photos": [{"index": 2, "type": "plant", "note": "  山上的   藤茶  "},
                      {"index": "1", "type": "package_front"}, "junk", {"index": 9, "type": "in_use"}]}
    tags = al.normalize_tags(raw, 3)
    assert tags[0] == ("package_front", "") and tags[1] == ("raw_material", "山上的 藤茶") and tags[2] is None
    # 没有 index 时按顺序对应；不是 dict / list 时全部为 None
    assert al.normalize_tags([{"type": "certificate"}], 1) == [("certificate", "")]
    assert al.normalize_tags("oops", 2) == [None, None]


def test_label_fields_cleaned_and_fed_to_planning():
    assert al.normalize_label_fields({" 配 料 ": " 藤茶 ", "空": "", "x" * 20: "v"}) == {"配料": "藤茶", "x" * 10: "v"}
    assert al.normalize_label_fields("不是字典") == {}
    lib = [{"type": "package_back", "label_fields": {"净含量": "100g", "保质期": "24 个月"}},
           {"type": "package_front", "label_fields": {"净含量": "错的"}}]
    ctx = al.label_context(lib)
    assert "净含量：100g" in ctx and "错的" not in ctx and "尚未核对" in ctx
    assert al.label_context([{"type": "in_use"}]) == ""


# ── 质量分 ────────────────────────────────────────────────────────────────────
def test_photo_quality_sharp_vs_blurry_and_white(tmp_path):
    sharp = al.photo_quality(_photo(tmp_path / "a.png"))
    blurry = al.photo_quality(_photo(tmp_path / "b.png", sharp=False, size=(500, 600)))
    assert sharp["score"] > blurry["score"] and blurry["score"] < al.GOOD_QUALITY
    assert sharp["width"] == 1200 and not sharp["white_bg"]
    white = al.photo_quality(_photo(tmp_path / "w.png", white=True))
    assert white["white_bg"] and "白底" in al.quality_note(white)


# ── 素材库读写 ────────────────────────────────────────────────────────────────
def test_add_update_remove_and_dedupe(tmp_path):
    a, b = _photo(tmp_path / "a.png"), _photo(tmp_path / "b.jpg", color=(200, 100, 50))
    added, skipped = al.add_photos([a, b, a, str(tmp_path / "notes.txt")])
    assert [e["id"] for e in added] == ["P01", "P02"]
    assert any("已经在素材库里" in s for s in skipped) and any("不是图片" in s for s in skipped)
    assert all(e["type_source"] == "pending" for e in al.load_library())
    assert len(al.needs_tagging(al.load_library())) == 2

    al.update_photo("P01", type="package_front")
    al.update_photo("P02", type_source="auto", type="in_use", note="茶汤")
    lib = al.load_library()
    assert al.find(lib, "P01")["type_source"] == "user" and al.find(lib, "P02")["note"] == "茶汤"
    # 手动改过的不会被重新识别
    assert [e["id"] for e in al.needs_tagging(lib, force=True)] == ["P02"]

    assert al.remove_photo("P01") and not al.remove_photo("P01")
    assert [e["id"] for e in al.load_library()] == ["P02"]
    # 删除后编号不复用
    added, _ = al.add_photos([_photo(tmp_path / "c.png", color=(10, 10, 200))])
    assert added[0]["id"] == "P03"


def test_library_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "MAX_PHOTOS", 2)
    paths = [_photo(tmp_path / f"{i}.png", color=(i * 40, 90, 90)) for i in range(3)]
    added, skipped = al.add_photos(paths)
    assert len(added) == 2 and "最多 2 张" in skipped[0]


def test_legacy_slots_migrated(tmp_path):
    al.LIB_DIR.mkdir(parents=True)
    _photo(al.LIB_DIR / "product_1.png")
    _photo(al.LIB_DIR / "product_2.jpg", color=(90, 60, 30))
    (al.LIB_DIR / "labels.json").write_text(json.dumps({"2": "背面配料表"}, ensure_ascii=False), encoding="utf-8")
    lib = al.load_library()
    assert [(e["id"], e["type"]) for e in lib] == [("P01", "package_front"), ("P02", "package_back")]
    assert (al.LIB_DIR / "library.json").exists()           # 只迁移一次
    assert ecom.get_product_images() == [lib[0]["path"]]    # 背面不拿来锁外观


def test_package_refs_prefers_front_then_quality(tmp_path):
    lib = _lib(tmp_path, ["in_use", "package_other", "package_front", "package_back"])
    assert [e["id"] for e in al.package_refs(lib)] == ["P03", "P02"]
    # 没有包装图时退回其它照片，但不用背面 / 证书
    lib = _lib(tmp_path, ["certificate", "in_use"])
    assert [e["id"] for e in al.package_refs(lib)] == ["P02"]


# ── 按屏分配素材 ──────────────────────────────────────────────────────────────
TEA_TYPES = ["package_front", "package_back", "raw_material", "product_closeup", "in_use", "texture_detail",
             "certificate", "lifestyle"]


def test_assign_matches_roles_to_photo_types(tmp_path):
    lib = _lib(tmp_path, TEA_TYPES)
    by_role = dict(zip(TEA_ROLES, al.assign(TEA_ROLES, lib)))
    content = {r.split("：")[0]: (a["content"], a["mode"]) for r, a in by_role.items()}
    assert content["原料产地"] == ("P03", "photo")              # 原料照片，证据屏 → 实拍直出
    assert content["冲泡方法"] == ("P05", "photo")
    assert content["叶底特写"] == ("P06", "photo")
    assert content["品质证据"] == ("P07", "photo")              # 证书只能实拍直出
    assert content["规格参数"] == ("P02", "photo")              # 包装背面只能实拍直出
    pain = by_role[TEA_ROLES[1]]
    assert pain["mode"] in ("reference", "generate") and pain["refs"] in ([], ["P08"])   # 痛点屏不出现产品包装
    hero = by_role[TEA_ROLES[0]]
    assert hero["mode"] == "reference" and hero["refs"][0] == "P01"                     # 首屏包装图锁外观


def test_assign_no_neighbor_reuse_and_reuse_cap(tmp_path):
    lib = _lib(tmp_path, ["package_front", "in_use", "in_use"])
    roles = [f"冲泡方法 {i}" for i in range(1, 8)]
    contents = [a["content"] for a in al.assign(roles, lib)]
    assert all(a != b for a, b in zip(contents, contents[1:]) if a), contents
    assert max(contents.count(p) for p in ("P02", "P03")) <= al.MAX_USES


def test_assign_low_quality_falls_back_to_reference(tmp_path):
    lib = _lib(tmp_path, ["package_front", "in_use"], quality=0.2)
    a = al.assign(["冲泡方法：3 步"], lib)[0]
    assert a["mode"] == "reference" and a["refs"] == ["P01", "P02"] and a["content"] == "P02"
    good = _lib(tmp_path, ["package_front", "in_use"])
    assert al.assign(["冲泡方法：3 步"], good, allow_photo=False)[0]["mode"] == "reference"


def test_assign_missing_types_and_empty_library(tmp_path):
    lib = _lib(tmp_path, ["package_front"])
    assets = al.assign(TEA_ROLES, lib)
    missing = al.missing_types(assets)
    assert any("原料 / 产地" in m for m in missing) and any("证书" in m for m in missing)
    origin = assets[3]
    assert origin["mode"] == "generate" and origin["missing"] == "raw_material"   # 没有原料照片：不出现产品
    assert all(a["mode"] == "generate" for a in al.assign(TEA_ROLES, []))


def test_assign_keeps_fixed_screens(tmp_path):
    lib = _lib(tmp_path, TEA_TYPES)
    manual = {"content": "P08", "refs": ["P01", "P08"], "mode": "reference", "missing": "", "source": "user"}
    assets = al.assign(TEA_ROLES, lib, fixed={5: manual})
    assert assets[5] is manual
    assert assets[4]["content"] != "P08" and assets[6]["content"] != "P08"


def test_photo_only_types_never_redrawn(tmp_path):
    lib = _lib(tmp_path, ["package_front", "certificate"])
    need = al.role_need("品质证据：检测报告")
    a = al.plan_one(need, al.find(lib, "P02"), al.package_refs(lib), mode="reference")
    assert "P02" not in a["refs"] and a["refs"] == ["P01"]


# ── 拍法和雷同检查 ────────────────────────────────────────────────────────────
def test_plan_shots_no_adjacent_repeat_and_variety():
    roles = ["场景 1", "场景 2", "场景 3", "核心卖点 1", "核心卖点 2", "叶底特写", "细节特写", "产地"]
    shots = al.plan_shots(roles)
    assert all(a != b for a, b in zip(shots, shots[1:])), shots
    assert len(set(shots)) >= 4
    assert al.plan_shots(roles, [{"mode": "photo"}] + [None] * 7)[0] == "photo"


def test_check_variety_flags_repeats():
    d = [{"id": "D1", "shot": "macro", "layout": "grid", "assets": {"content": "P02"}},
         {"id": "D2", "shot": "macro", "layout": "grid", "assets": {"content": "P02"}},
         {"id": "D3", "shot": "photo", "layout": "standard", "assets": {"content": "P03"}},
         {"id": "D4", "shot": "photo", "layout": "standard", "assets": {"content": "P04"}}]
    issues = "\n".join(al.check_variety(d))
    assert "D1/D2 相邻两屏用了同一张照片 P02" in issues and "都是「微距特写」" in issues and "「grid」信息图" in issues
    assert "D3/D4" not in issues                 # 两张不同的实拍照片不算雷同
    many = [{"id": f"D{i}", "shot": "tabletop" if i % 2 else "hero"} for i in range(10)]
    assert "只用了 2 种拍法" in "\n".join(al.check_variety(many))

# ── 策划：素材分配写进提示词和策划结果 ─────────────────────────────────────────
def _plan_raw(detail_title="一层白霜"):
    return {"theme_color": "#6B8E4E", "art_direction": "soft window light",
            "main_images": [{"headline": "张家界莓茶"} for _ in range(5)],
            "detail_sections": [{"title": detail_title, "scene_prompt_en": "a cup on a table"} for _ in range(15)]}


def test_plan_uses_library_assignment(tmp_path, monkeypatch):
    lib = _lib(tmp_path, TEA_TYPES)
    seen = {}

    def chat(system, user, **kw):
        seen["user"] = user
        return _plan_raw()
    monkeypatch.setattr(ecom, "_chat_json", chat)
    plan = ecom.generate_ecom_plan("张家界莓茶", ["白霜"], "#6B8E4E", playbook="food", library=lib)
    details = plan["detail_sections"]
    assert all(d.get("shot") and d.get("assets") for d in details)
    photo = [d for d in details if d["assets"]["mode"] == "photo"]
    assert photo and all(d["scene_prompt_en"] == "" and d["shot"] == "photo" for d in photo)
    assert "【实拍直出：" in seen["user"] and "拍法：" in seen["user"]
    # 素材库为空时只有拍法，没有素材，并提示上传
    plan2 = ecom.generate_ecom_plan("张家界莓茶", ["白霜"], "#6B8E4E", playbook="food", library=[])
    assert all("assets" not in d and d["shot"] for d in plan2["detail_sections"])
    assert any("素材库是空的" in i for i in plan2["quality_issues"])


def test_real_photo_claim_only_on_photo_screens(tmp_path, monkeypatch):
    lib = _lib(tmp_path, ["package_front"])
    monkeypatch.setattr(ecom, "_chat_json", lambda *a, **k: _plan_raw("产地实拍"))
    plan = ecom.generate_ecom_plan("张家界莓茶", ["白霜"], "#6B8E4E", playbook="food", library=lib)
    assert any(i.startswith(qc.ERR) and "「实拍」" in i for i in plan["quality_issues"])


def test_reassign_after_library_change(tmp_path, monkeypatch):
    lib = _lib(tmp_path, ["package_front"])
    monkeypatch.setattr(ecom, "_chat_json", lambda *a, **k: _plan_raw())
    plan = ecom.generate_ecom_plan("张家界莓茶", ["白霜"], "#6B8E4E", playbook="food", library=lib)
    ecom.set_item_asset(plan["detail_sections"][0], lib, "", "generate")       # 用户手动改了第 1 屏
    res = ecom.reassign_assets(plan, _lib(tmp_path, TEA_TYPES))
    assert res["changed"] and plan["detail_sections"][0]["assets"]["mode"] == "generate"
    usage = next(d for d in plan["detail_sections"] if "使用方法" in d["role"] or "冲泡" in d["role"])
    assert usage["assets"]["content"] in ("P04", "P05")

# ── 出图：实拍直出不调接口；参考重绘的提示词逐张说明参考图 ───────────────────────
def test_photo_mode_composes_locally(tmp_path, monkeypatch):
    lib = _lib(tmp_path, ["package_front", "in_use"])
    monkeypatch.setattr(ecom, "BASE_DIR", tmp_path / "base")
    monkeypatch.setattr(ecom, "FINAL_DIR", tmp_path / "final")
    monkeypatch.setattr(ecom, "RENDERER", "pil")
    monkeypatch.setattr(ecom, "edit_image", lambda *a, **k: pytest.fail("实拍直出不应调用生图接口"))
    item = {"id": "D6", "seq": 6, "ratio": "3:4", "role": "冲泡方法", "title": "三步冲泡", "body": "", "bullets": [],
            "layout": "standard", "assets": {"content": "P02", "refs": ["P02"], "mode": "photo", "missing": ""}}
    plan = {"meta": {"theme_color": "#6B8E4E"}, "detail_sections": [item]}
    ecom.render_item(plan, "detail", item, [], {}, library=lib)
    assert Image.open(item["base_path"]).size == ecom.TARGET_SIZES["3:4"] and item["base_mode"] == "photo"
    assert item["base_key"] == "photo:P02" and not ecom.needs_regen(item, "ai", "detail", lib)
    item["assets"] = {"content": "P01", "refs": ["P01"], "mode": "photo", "missing": ""}
    assert ecom.needs_regen(item, "overlay", "detail", lib)          # 换素材后要重出底图


def _capture_edit(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ecom, "BASE_DIR", tmp_path / "base")
    monkeypatch.setattr(ecom, "compose_item", lambda plan, kind, it: "")

    def fake_edit(prompt, paths, out, ratio, allow_empty=False):
        calls.append((prompt, paths, allow_empty))
        return out
    monkeypatch.setattr(ecom, "edit_image", fake_edit)
    return calls


def test_reference_and_generate_prompts(tmp_path, monkeypatch):
    lib = _lib(tmp_path, ["package_front", "raw_material"])
    calls = _capture_edit(monkeypatch, tmp_path)
    plan = {"meta": {"art_direction": "pale oak tabletop"}}
    ref = {"id": "D4", "ratio": "3:4", "role": "产地", "shot": "landscape", "scene_prompt_en": "misty hills",
           "assets": {"content": "P02", "refs": ["P01", "P02"], "mode": "reference"}}
    ecom.render_item(plan, "detail", ref, [], {}, library=lib)
    prompt, paths, _ = calls[-1]
    assert paths == [lib[0]["path"], lib[1]["path"]]
    assert "Reference image 1 shows the real product packaging" in prompt
    assert "Reference image 2 is a real photo of the raw material" in prompt
    assert "Shot type: wide environmental shot" in prompt and "pale oak tabletop" in prompt
    assert ref["base_key"] == "reference:P01,P02"

    gen = {"id": "D2", "ratio": "3:4", "role": "痛点", "shot": "lifestyle", "scene_prompt_en": "a tired office worker",
           "assets": {"content": "", "refs": [], "mode": "generate"}}
    ecom.render_item(plan, "detail", gen, ["legacy.png"], {}, library=lib)
    prompt, paths, allow_empty = calls[-1]
    assert paths == [] and allow_empty and "Do not show the product" in prompt
    assert "Reproduce this product" not in prompt


def test_legacy_plan_without_assets_uses_product_paths(tmp_path, monkeypatch):
    calls = _capture_edit(monkeypatch, tmp_path)
    item = {"id": "M1", "ratio": "1:1", "role": "点击图", "scene_prompt_en": "x"}
    ecom.render_item({"meta": {}}, "main", item, ["a.png", "b.png"], {}, library=[])
    assert calls[0][1] == ["a.png", "b.png"] and "Reference images 1-2 show the same real product" in calls[0][0]
    assert item["base_key"] == "legacy"


def test_white_main_uses_white_package_photo(tmp_path, monkeypatch):
    lib = _lib(tmp_path, ["package_front"])
    lib[0]["quality"]["white_bg"] = True
    monkeypatch.setattr(ecom, "BASE_DIR", tmp_path / "base")
    monkeypatch.setattr(ecom, "compose_item", lambda plan, kind, it: "")
    monkeypatch.setattr(ecom, "edit_image", lambda *a, **k: pytest.fail("白底实拍不应调用生图接口"))
    item = {"id": "M5", "ratio": "1:1", "role": "白底图", "white_bg": True}
    ecom.render_item({"meta": {}}, "main", item, [], {}, library=lib)
    assert item["base_mode"] == "photo" and Image.open(item["base_path"]).getpixel((5, 5)) == (255, 255, 255)


# ── 视觉模型分类：结果写回素材库 ─────────────────────────────────────────────────
def test_classify_photos_writes_back(tmp_path, monkeypatch):
    added, _ = al.add_photos([_photo(tmp_path / "a.png"), _photo(tmp_path / "b.png", color=(30, 30, 30))])
    sent = []

    def chat(system, content, **kw):
        sent.append(content)
        return {"photos": [{"index": 1, "type": "package_back", "note": "配料表", "label_fields": {"净含量": "100g"}},
                           {"index": 2, "type": "in_use", "note": "茶汤", "label_fields": {"净含量": "不该写"}}]}
    monkeypatch.setattr(ecom, "_chat_json", chat)
    done, errors = ecom.classify_photos(added)
    lib = al.load_library()
    assert done == 2 and not errors
    assert lib[0]["type"] == "package_back" and lib[0]["label_fields"] == {"净含量": "100g"}
    assert lib[1]["type"] == "in_use" and "label_fields" not in lib[1] and lib[1]["type_source"] == "auto"
    assert sum(c.get("type") == "image_url" for c in sent[0]) == 2

    def boom(*a, **k):
        raise Exception("no vision")
    monkeypatch.setattr(ecom, "_chat_json", boom)
    done, errors = ecom.classify_photos(al.load_library())
    assert done == 0 and "识别失败" in errors[0]
