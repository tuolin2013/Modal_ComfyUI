"""参考图分配（主图也用素材库、未分类照片、缺实拍不出图、补拍后自动重新分配）+ 并行出图 + 网络请求"""

import os
import threading
import time

import pytest
from PIL import Image

import asset_library as al
import ecom_images as ecom


def _lib(tmp_path, types, quality=0.9, source="user"):
    out = []
    for i, t in enumerate(types, 1):
        p = tmp_path / f"P{i:02d}.png"
        if not p.exists():
            Image.new("RGB", (400, 500), (100 + i * 10, 140, 90)).save(p)
        out.append({"id": f"P{i:02d}", "file": p.name, "path": str(p), "type": t, "type_source": source,
                    "note": "", "quality": {"score": quality, "white_bg": False}})
    return out


def _plan_raw():
    return {"theme_color": "#6B8E4E", "art_direction": "soft window light",
            "main_images": [{"headline": "张家界莓茶", "scene_prompt_en": "tea on a desk"} for _ in range(5)],
            "detail_sections": [{"title": "一层白霜", "scene_prompt_en": "a cup on a table"} for _ in range(15)]}


TEA_TYPES = ["package_front", "package_back", "raw_material", "product_closeup", "in_use", "texture_detail",
             "certificate", "lifestyle"]


def _capture_edit(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ecom, "BASE_DIR", tmp_path / "base")
    monkeypatch.setattr(ecom, "compose_item", lambda plan, kind, it, cat=None: "")

    def fake_edit(prompt, paths, out, ratio, allow_empty=False):
        calls.append((prompt, paths, allow_empty))
        return out
    monkeypatch.setattr(ecom, "edit_image", fake_edit)
    return calls


# ── 主图也按素材库分配参考图 ───────────────────────────────────────────────────
def test_main_images_get_library_refs(tmp_path, monkeypatch):
    lib = _lib(tmp_path, TEA_TYPES)
    seen = {}

    def chat(system, user, **kw):
        seen["user"] = user
        return _plan_raw()
    monkeypatch.setattr(ecom, "_chat_json", chat)
    plan = ecom.generate_ecom_plan("张家界莓茶", ["白霜"], "#6B8E4E", playbook="food", library=lib)
    mains = plan["main_images"]
    white = [m for m in mains if m["white_bg"]]
    assert white and all("assets" not in m for m in white)
    others = [m for m in mains if not m["white_bg"]]
    assert all(m["assets"]["mode"] == "reference" and m["assets"]["refs"][0] == "P01" for m in others)
    # 主图不用背面 / 证书（文字多，重绘会画花），也不实拍直出
    used = {r for m in others for r in m["assets"]["refs"]}
    assert not used & {"P02", "P07"}
    scene = next(m for m in others if "场景" in m["role"])
    assert scene["assets"]["content"] in ("P05", "P08")          # 场景图带上使用中 / 生活场景的实拍
    assert "【参考重绘" in seen["user"].split("屏详情页")[0]        # 策划时 LLM 也知道主图用了哪张素材
    assert plan["meta"]["library_sig"] == al.signature(lib)

    calls = _capture_edit(monkeypatch, tmp_path)
    ecom.render_item(plan, "main", scene, ["legacy.png"], {}, library=lib)
    prompt, paths, _ = calls[-1]
    assert "legacy.png" not in paths and paths[0] == lib[0]["path"]
    assert al.find(lib, scene["assets"]["content"])["path"] in paths
    assert "Reproduce this product exactly" in prompt


def test_untagged_photos_still_lock_the_product(tmp_path):
    """回归：照片还没识别类型（other）时被当成包装图带上，提示词却写「不要出现产品」"""
    lib = _lib(tmp_path, ["other", "other"], source="pending")
    a = al.assign(["产品亮相：产品就是解决方案"], lib)[0]
    assert a["mode"] == "reference" and a["refs"] == ["P01", "P02"]
    item = {"id": "D3", "ratio": "3:4", "role": "产品亮相", "scene_prompt_en": "x", "assets": a}
    mode, refs = ecom._resolve_refs(item, "detail", lib)
    prompt = ecom.build_edit_prompt(item, "detail", len(refs), [], refs=refs, content_id=a["content"])
    assert "Do not show the product" not in prompt
    assert "Reproduce this product exactly" in prompt and "real photo of the product as sold" in prompt


def test_content_photos_are_faithful(tmp_path):
    lib = _lib(tmp_path, ["package_front", "raw_material", "in_use"])
    origin = ecom._ref_lines([lib[1]], "P02")[0]
    assert "real place" in origin and "do not invent a different place" in origin
    brew = ecom._ref_lines([lib[0], lib[2]], "P03")
    assert "packaging, front view" in brew[0] and "do not invent a different look" in brew[1]


def test_old_main_not_redrawn_just_because_assets_were_added(tmp_path):
    lib = _lib(tmp_path, ["package_front", "lifestyle"])
    base = tmp_path / "M1_base.png"
    Image.new("RGB", (10, 10)).save(base)
    old = {"id": "M1", "role": "点击图", "base_path": str(base), "base_mode": "overlay", "base_key": "legacy",
           "assets": {"content": "", "refs": ["P01"], "mode": "reference"}}
    assert not ecom.needs_regen(old, "overlay", "main", lib)
    old["assets"] = {"content": "P02", "refs": ["P01", "P02"], "mode": "reference"}
    assert ecom.needs_regen(old, "overlay", "main", lib)          # 用上了新的实拍：要重画


# ── 缺实拍不凭空画 ────────────────────────────────────────────────────────────
def test_real_content_screen_without_photo_is_blocked(tmp_path, monkeypatch):
    lib = _lib(tmp_path, ["package_front"])
    monkeypatch.setattr(ecom, "_chat_json", lambda *a, **k: _plan_raw())
    plan = ecom.generate_ecom_plan("张家界莓茶", ["白霜"], "#6B8E4E", playbook="food", library=lib)
    origin = next(d for d in plan["detail_sections"] if "产地" in d["role"])
    assert origin["assets"]["needs_photo"] and origin["assets"]["missing"] == "raw_material"
    assert any(origin["id"] in i and "不出图" in i for i in plan["quality_issues"])
    assert "⛔" in al.asset_note(origin["assets"], lib)
    pain = next(d for d in plan["detail_sections"] if "痛点" in d["role"])
    assert not pain["assets"]["needs_photo"]                       # 氛围屏照常纯生成

    calls = _capture_edit(monkeypatch, tmp_path)
    with pytest.raises(ecom.NeedsPhoto):
        ecom.render_item(plan, "detail", origin, [], {}, library=lib)
    assert not calls
    # 商家明确选了出图方式就照办
    ecom.set_item_asset(origin, lib, "", "generate")
    ecom.render_item(plan, "detail", origin, [], {}, library=lib)
    assert calls and calls[-1][2] is True


def test_sync_assets_after_new_photos(tmp_path, monkeypatch):
    lib = _lib(tmp_path, ["package_front"])
    monkeypatch.setattr(ecom, "_chat_json", lambda *a, **k: _plan_raw())
    plan = ecom.generate_ecom_plan("张家界莓茶", ["白霜"], "#6B8E4E", playbook="food", library=lib)
    assert ecom.sync_assets(plan, lib) is None                      # 素材库没变：不动
    origin = next(d for d in plan["detail_sections"] if "产地" in d["role"])
    manual = plan["detail_sections"][1]
    ecom.set_item_asset(manual, lib, "", "generate")
    bigger = _lib(tmp_path, ["package_front", "raw_material", "lifestyle"])
    res = ecom.sync_assets(plan, bigger)
    assert origin["id"] in res["changed"] and origin["assets"]["content"] == "P02"
    assert not origin["assets"]["needs_photo"]
    assert manual["assets"]["source"] == "user" and manual["assets"]["mode"] == "generate"
    assert ecom.sync_assets(plan, bigger) is None


# ── 并行出图 ──────────────────────────────────────────────────────────────────
def _jobs(n):
    plan = {"meta": {"theme_color": "#6B8E4E"},
            "main_images": [{"id": f"M{i}", "seq": i, "ratio": "1:1", "role": "点击图", "headline": "x",
                             "scene_prompt_en": "x"} for i in range(1, n + 1)],
            "detail_sections": []}
    return plan, [("main", it) for it in plan["main_images"]]


def test_render_many_runs_in_parallel_and_writes_back(tmp_path, monkeypatch):
    plan, jobs = _jobs(8)
    active, peak, lock = [0], [0], threading.Lock()

    def fake_render(plan_view, kind, item, *a, **k):
        assert plan_view is not plan and item is not next(it for _, it in jobs if it["id"] == item["id"])
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.15)
        with lock:
            active[0] -= 1
        item["base_path"] = f"{item['id']}.png"
        return "x"
    monkeypatch.setattr(ecom, "render_item", fake_render)
    t0 = time.time()
    results = list(ecom.render_many(plan, jobs, ["p.png"], {}, library=[], force=True, workers=4))
    assert time.time() - t0 < 0.15 * 8 * 0.6                     # 明显快于串行
    assert peak[0] == 4
    assert sorted(it["id"] for _, it, _, _ in results) == [f"M{i}" for i in range(1, 9)]
    assert all(s == "generated" for _, _, s, _ in results)
    assert all(it["base_path"] == f"{it['id']}.png" for it in plan["main_images"])   # 结果写回 plan


def test_render_many_failure_does_not_touch_plan(tmp_path, monkeypatch):
    plan, jobs = _jobs(3)

    def fake_render(plan_view, kind, item, *a, **k):
        item["base_path"] = "half-done.png"
        if item["id"] == "M2":
            raise ValueError("boom")
        if item["id"] == "M3":
            raise ecom.NeedsPhoto("缺照片")
        return "x"
    monkeypatch.setattr(ecom, "render_item", fake_render)
    out = {it["id"]: (s, info) for _, it, s, info in
           ecom.render_many(plan, jobs, ["p.png"], {}, library=[], force=True, workers=2)}
    assert out["M1"][0] == "generated" and out["M2"] == ("failed", "boom") and out["M3"][0] == "needs_photo"
    m2, m3 = plan["main_images"][1], plan["main_images"][2]
    assert "base_path" not in m2 and "base_path" not in m3        # 失败的那张 plan 不被改半截


def test_render_many_stops_after_service_errors(monkeypatch):
    plan, jobs = _jobs(6)
    calls = []

    def fake_render(plan_view, kind, item, *a, **k):
        calls.append(item["id"])
        raise ecom.ServiceError("502")
    monkeypatch.setattr(ecom, "render_item", fake_render)
    statuses = [s for _, _, s, _ in ecom.render_many(plan, jobs, ["p.png"], {}, library=[], force=True, workers=2)]
    assert len(statuses) == 6 and len(calls) <= 3
    assert statuses.count("skipped") == 6 - len(calls)


def test_render_many_reuses_base(tmp_path, monkeypatch):
    plan, jobs = _jobs(2)
    for _, it in jobs:
        p = tmp_path / f"{it['id']}.png"
        Image.new("RGB", (10, 10)).save(p)
        it.update(base_path=str(p), base_mode="overlay", base_key="legacy")
    monkeypatch.setattr(ecom, "render_item", lambda *a, **k: pytest.fail("能复用的底图不该重画"))
    monkeypatch.setattr(ecom, "compose_item", lambda plan, kind, it, cat=None: it.update(final_path="f.jpg"))
    out = list(ecom.render_many(plan, jobs, ["p.png"], {}, library=[], workers=2))
    assert [s for _, _, s, _ in out] == ["reused", "reused"]
    assert all(it["final_path"] == "f.jpg" for it in plan["main_images"])


# ── 网络请求：不改进程级环境变量；429 重试；下载有超时 ──────────────────────────
class _Resp:
    def __init__(self, code, body=None, content=b""):
        self.status_code, self._body, self.content, self.text = code, body, content, ""

    def json(self):
        return self._body


def test_request_keeps_no_proxy_and_retries_429(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost")
    monkeypatch.setattr(ecom.time, "sleep", lambda s: None)
    seen = []

    def fake(method, url, **kw):
        seen.append((os.environ.get("NO_PROXY"), "proxies" in kw))
        return _Resp(429 if len(seen) == 1 else 200)
    monkeypatch.setattr(ecom.requests, "request", fake)
    assert ecom._request("GET", "https://example.invalid/x", timeout=1).status_code == 200
    assert seen == [("localhost", True), ("localhost", True)]


def test_url_result_is_downloaded_with_timeout(tmp_path, monkeypatch):
    kw_seen = {}

    def fake(method, url, **kw):
        kw_seen.update(kw)
        return _Resp(200, content=b"png-bytes")
    monkeypatch.setattr(ecom.requests, "request", fake)
    out = tmp_path / "a.png"
    ecom._save_image_response({"data": [{"url": "https://cdn.example.invalid/a.png"}]}, out)
    assert out.read_bytes() == b"png-bytes" and kw_seen["timeout"] > 0


# ── 卡片上传这一屏的参考照片 ───────────────────────────────────────────────────
def test_card_upload_assigns_photo(tmp_path, monkeypatch):
    app = pytest.importorskip("app_gradio")
    monkeypatch.setattr(ecom, "PLAN_PATH", tmp_path / "plan.json")
    monkeypatch.setattr(ecom, "ECOM_DIR", tmp_path)
    lib = al.load_library()
    assert not lib
    al.add_photos([_lib(tmp_path, ["package_front"])[0]["path"]])
    al.update_photo("P01", type="package_front")
    item = {"id": "D8", "seq": 8, "ratio": "3:4", "role": "原料产地：原料、产地、工艺",
            "assets": {"content": "", "refs": [], "mode": "generate", "missing": "raw_material",
                       "needs_photo": True, "source": "auto"}}
    ecom.save_plan({"meta": {"main_ratio": "1:1"}, "main_images": [], "detail_sections": [item]})
    photo = tmp_path / "field.png"
    Image.new("RGB", (600, 800), (40, 120, 60)).save(photo)
    msg, plan, lib, _ = app.ecom_item_upload("detail", "D8", str(photo))
    d = plan["detail_sections"][0]
    new = al.find(lib, d["assets"]["content"])
    assert new and new["type"] == "raw_material" and new["type_source"] == "user"
    # 纯色测试图清晰度为 0，不够实拍直出，退为参考重绘；关键是这张照片进了参考图
    assert d["assets"]["source"] == "user" and not d["assets"]["needs_photo"] and new["id"] in d["assets"]["refs"]
    # 同一张再传一次：不重复入库，直接用已有的
    _, plan2, lib2, _ = app.ecom_item_upload("detail", "D8", str(photo))
    assert len(lib2) == len(lib) and plan2["detail_sections"][0]["assets"]["content"] == new["id"]
