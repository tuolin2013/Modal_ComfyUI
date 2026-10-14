"""
类目生成策略的契约测试：新加一个类目（categories/xxx.py）时，这里全绿才算接上。

检查内容：
  - 注册表能发现它，key / parent 正确，手册能在 parent 之上继承
  - 手册齐全：15 屏屏序、标签必备项、照片清单、合规词
  - 生成策略的引用都能对上号：照片类型、拍法、主图角色名、图标名、正则能编译
  - 覆盖性：每屏「必须照实拍画」的屏，它要的照片类型都在照片清单里（清单是给商家看的补拍建议，
    对不上的话那屏永远等不到照片）
"""

import re

import pytest

import asset_library as al
import categories
import category_playbook as cp
import ecom_images as ecom
import positioning as pos


ALL = sorted(categories.REGISTRY)


def test_registry_has_the_two_categories():
    assert ALL == ["pet_supplement", "tea"]
    for key, parent in (("tea", "food"), ("pet_supplement", "pet")):
        cat = categories.get(key)
        assert cat.key == key and cat.parent == parent and cat.playbook
        assert key in cp.BUILTIN_KEYS and cp.is_standard(key)
        assert cp.get(key)["parent"] == parent
        assert cp.DETECT_ORDER.index(key) < cp.DETECT_ORDER.index(parent)     # 更专门的排在前面


def test_unknown_category_falls_back_to_generic():
    """没注册的类目（通用、食品、美妆……）行为与重构前一致：不套任何类目专属规则"""
    for key in (None, "", "general", "food", "beauty", "custom:不存在"):
        cat = cp.category(key)
        assert cat is categories.GENERIC
        assert cat.main_roles("拼多多", ecom.ECOM_PLATFORMS["拼多多"]["main_roles"]) == \
            ecom.ECOM_PLATFORMS["拼多多"]["main_roles"]
        assert cat.role_rules == () and cat.image_rules == () and cat.icon_rules == ()


@pytest.mark.parametrize("key", ALL)
def test_playbook_is_complete(key):
    pb = cp.get(key)
    assert pb["label"] and pb["keywords"] and pb["compliance"]
    assert set(pb["positioning_plans"]) == set(pos.POSITIONINGS)
    for plan in pb["positioning_plans"].values():
        assert len(plan) == 15 and len(set(plan)) == 15
    assert pb["photo_checklist"], "照片清单不能为空，商家照它补拍"
    assert all(t in al.PHOTO_TYPES for t, _, _ in pb["photo_checklist"])
    assert any(required for _, _, required in pb["photo_checklist"])


@pytest.mark.parametrize("key", ALL)
def test_strategy_references_are_valid(key):
    cat = categories.get(key)
    for pattern, need in cat.role_rules:
        re.compile(pattern)
        assert set(need) <= {"types", "product", "photo", "real", "shots"}, (key, pattern, set(need))
        assert need.get("types"), (key, pattern)
        assert all(t in al.PHOTO_TYPES for t in need["types"]), (key, pattern, need["types"])
        assert all(s in al.SHOTS for s in need.get("shots") or []), (key, pattern, need.get("shots"))
    assert all(t in al.PHOTO_TYPES for t in cat.photo_descs), key
    assert cat.keep_scene_types <= set(al.PHOTO_TYPES), key
    for pattern, name in cat.icon_rules:
        re.compile(pattern)
        assert name in ecom.ICONS, (key, name)
    assert all(isinstance(r, str) and r.strip() for r in cat.image_rules), key
    assert cat.scene_example and cat.action_example and cat.art_example and cat.note_example, key


@pytest.mark.parametrize("key", ALL)
def test_main_overrides_match_platform_roles(key):
    """主图角色名要能在平台的方案里找到（改了平台的叫法这里会报错），且不动白底图"""
    cat = categories.get(key)
    heads = {r.split("：", 1)[0] for spec in ecom.ECOM_PLATFORMS.values() for r in spec["main_roles"]}
    for platform, over in cat.main_overrides.items():
        assert platform == "*" or platform in ecom.ECOM_PLATFORMS, (key, platform)
        for head, desc in over.items():
            assert head in heads, (key, head)
            assert "白底" not in head and desc.strip(), (key, head)
    # 替换后：张数、顺序和角色名都不变，只有说明变了
    for platform, spec in ecom.ECOM_PLATFORMS.items():
        roles = cat.main_roles(platform, spec["main_roles"])
        assert [r.split("：")[0] for r in roles] == [r.split("：")[0] for r in spec["main_roles"]]
        assert len(roles) == len(spec["main_roles"])


@pytest.mark.parametrize("key", ALL)
def test_real_screens_have_a_photo_to_ask_for(key):
    """
    画面主体是真实产品内容的屏（role_rules 里 real=True），它首选的素材类型必须在照片清单里，
    否则程序会一直等一张清单里根本没让商家拍的照片
    """
    cat = categories.get(key)
    checklist = {t for t, _, _ in cat.playbook["photo_checklist"]}
    missing = []
    for plan in cat.playbook["positioning_plans"].values():
        for role in plan:
            need = al.role_need(role, cat=cat)
            if need.get("real") and need["types"][0] not in checklist:
                missing.append((role, need["types"][0]))
    assert not missing, f"{key} 这些屏要的照片不在清单里：{missing}"


def test_categories_differ_where_it_matters():
    """两个类目的出图逻辑确实不一样（不然解耦就没意义了）"""
    tea, pet = categories.get("tea"), categories.get("pet_supplement")
    head = "细节/证据图：材质、成分、工艺特写"
    assert tea.main_roles("淘宝/天猫", [head])[0] != pet.main_roles("淘宝/天猫", [head])[0]
    # 同一句屏的作用文字，两个类目挑的素材不一样：茶要原料植株，宠物补充剂要包装背面的成分表
    assert al.role_need("成分与配方：原料组成、添加剂组成", cat=tea)["types"][0] == "raw_material"
    assert al.role_need("成分与配方：原料组成、添加剂组成", cat=pet)["types"][0] == "package_back"
    assert al.role_need("茶汤展示：茶汤颜色", cat=tea)["types"][0] == "in_use"
    assert al.role_need("适口性：宠物进食画面", cat=pet)["types"][0] == "in_use"
    # 参考图说明也跟着类目变
    assert "liquor" in tea.photo_desc("in_use", "")
    assert "pet eating" in pet.photo_desc("in_use", "")
    assert tea.photo_desc("product_closeup", "FALLBACK") != pet.photo_desc("product_closeup", "FALLBACK")
    # 画面要求只进自己那一版提示词（策划输入用 category_prompt_block，示例用 plan_system）
    assert "veterinary" not in ecom.category_prompt_block(tea)
    assert "veterinary" in ecom.category_prompt_block(pet)
    assert ecom.category_prompt_block(categories.GENERIC) == ""
    assert "amber tea" in ecom.plan_system(tea) and "amber tea" not in ecom.plan_system(pet)
    assert "cat bowl" in ecom.plan_system(pet) and "cat bowl" not in ecom.PLAN_SYSTEM


def test_same_overrides_route_per_category():
    """同一屏文字在两个类目下选到的素材类型不同（用的是各自手册里真实存在的屏名）"""
    tea, pet = categories.get("tea"), categories.get("pet_supplement")
    # 「产品特写」两个手册都有，但茶手册没有专属规则 → 落到通用规则（纹理特写）
    # 宠物补充剂手册有专属规则 → 片剂 / 粉末实拍
    role = "产品特写：片剂 / 粉末的形态和大小"
    assert al.role_need(role, cat=tea)["types"][0] == "texture_detail"
    assert al.role_need(role, cat=pet)["types"][0] == "product_closeup"
    assert al.role_need(role, cat=pet)["real"] is True


def test_custom_playbook_inherits_parent_strategy(tmp_path, monkeypatch):
    """自定义手册不写代码，沿用 parent 的生成策略（parent 选 tea 就是茶的出图逻辑）"""
    monkeypatch.setattr(cp, "CUSTOM_DIR", tmp_path / "playbooks")
    key = cp.save_custom({"label": "菊花茶", "parent": "tea", "keywords": ["菊花茶"]})
    assert cp.category(key) is categories.get("tea")
    other = cp.save_custom({"label": "薯片", "parent": "food", "keywords": ["薯片"]})
    assert cp.category(other) is categories.GENERIC


def test_ecom_plan_uses_category_main_roles(monkeypatch):
    """策划出来的主图角色跟着类目走（同一平台，茶和宠物补充剂不一样）"""
    raw = {"theme_color": "#6B8E4E", "art_direction": "x",
           "main_images": [{"headline": "x", "scene_prompt_en": "y"} for _ in range(5)],
           "detail_sections": [{"title": "t", "scene_prompt_en": "z"} for _ in range(15)]}
    monkeypatch.setattr(ecom, "_chat_json", lambda *a, **k: raw)
    tea = ecom.generate_ecom_plan("张家界莓茶", ["白霜"], "#6B8E4E", "拼多多", playbook="tea", library=[])
    pet = ecom.generate_ecom_plan("猫静宝", ["适口"], "#6B8E4E", "拼多多",
                                  playbook="pet_supplement", library=[])
    assert "干茶" in tea["main_images"][2]["role"]
    assert "宠物进食" in pet["main_images"][2]["role"]
    assert [d["role"] for d in tea["detail_sections"]] == categories.get("tea").playbook["positioning_plans"]["value"]
