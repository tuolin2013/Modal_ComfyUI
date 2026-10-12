"""带货视频画幅：平台比例要传到 H3，否则 H3 一律按 16:9 出图（竖屏平台变横屏）"""

import sys
from pathlib import Path

import pytest

# h3_diffusers_service.py 在项目根目录
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture
def svc():
    return pytest.importorskip("h3_diffusers_service")


@pytest.fixture
def app():
    return pytest.importorskip("app_gradio")


# 期望值按 diffusers minimax_h3.resolve_canvas_size（短边 768、面积上限 768×1344、32 对齐）算出
@pytest.mark.parametrize("ratio, hw", [
    ("9:16", (1344, 768)),
    ("1:1", (768, 768)),
    ("16:9", (768, 1344)),
    ("3:4", (1024, 768)),
    ("9：16", (1344, 768)),      # 全角冒号
    (None, (768, 1344)),         # 旧客户端不传：与 H3 默认 16:9 一致
])
def test_canvas_size(svc, ratio, hw):
    h, w = svc._canvas_size(ratio)
    assert (h, w) == hw
    assert h % 32 == 0 and w % 32 == 0


@pytest.mark.parametrize("bad", ["abc", "9x16", "1:5", "0:1", "16:9:1"])
def test_canvas_size_rejects_bad_ratio(svc, bad):
    with pytest.raises(ValueError):
        svc._canvas_size(bad)


def test_video_aspect_ratio_fallbacks(app):
    assert app.video_aspect_ratio({"meta": {"aspect_ratio": "1:1"}}) == "1:1"
    # 旧分镜只有平台名
    assert app.video_aspect_ratio({"meta": {"platform": "淘宝/京东 详情页（横版 16:9）"}}) == "16:9"
    assert app.video_aspect_ratio({}) == app.PLATFORMS[app.DEFAULT_PLATFORM]["aspect_ratio"]


class _FakeRunner:
    calls: list = []

    def __init__(self, workflow):
        self.workflow = workflow
        runner = self

        class _Method:
            @staticmethod
            def remote(*args, **kwargs):
                _FakeRunner.calls.append((runner.workflow, args, kwargs))
                return b"mp4"

        self.render_single_shot = _Method()


class _FakeCls:
    @staticmethod
    def hydrate():
        pass

    def __call__(self, workflow):
        return _FakeRunner(workflow)


@pytest.fixture
def render_env(app, monkeypatch, tmp_path):
    """把批量渲染的外部依赖（磁盘分镜、H3 提示词、Modal、FFmpeg）都换成替身"""
    sb = {"meta": {"aspect_ratio": "9:16", "seed": 7},
          "shots": [{"id": 1, "mode": "T2VA", "duration_sec": 5, "image_gen_prompt": "a cat"}]}
    _FakeRunner.calls = []
    monkeypatch.setattr(app, "load_storyboard", lambda: sb)
    monkeypatch.setattr(app, "ensure_h3_prompts", lambda: "")
    monkeypatch.setattr(app, "prompt_file_for", lambda s: tmp_path / "none.txt")
    monkeypatch.setattr(app, "VIDEO_DIR", tmp_path / "videos")
    monkeypatch.setattr(app.modal.Cls, "from_name", lambda *a, **k: _FakeCls())
    monkeypatch.setattr(app, "concat_videos", lambda vids: ("ok", str(vids[-1])))
    return sb


def _noop_progress(*a, **k):
    pass


def test_batch_render_passes_aspect_ratio(app, render_env):
    status, _ = app.batch_render_with_diffusers(progress=_noop_progress)
    assert status.startswith("ok"), status
    assert len(_FakeRunner.calls) == 1
    wf, args, kwargs = _FakeRunner.calls[0]
    assert wf == "t2va" and kwargs.get("aspect_ratio") == "9:16"


def test_batch_render_drops_videos_of_other_ratio(app, render_env):
    """修复前渲染的镜头都是 16:9，换成 9:16 后不能被当成已有视频复用"""
    vdir = app.VIDEO_DIR
    vdir.mkdir(parents=True)
    (vdir / "shot_01.mp4").write_bytes(b"old-16x9")      # 没有画幅记录 = 修复前的旧视频
    status, _ = app.batch_render_with_diffusers(progress=_noop_progress)
    assert len(_FakeRunner.calls) == 1, "旧画幅的视频应当重渲"
    assert "16:9 改为 9:16" in status
    assert (vdir / "shot_01.mp4").read_bytes() == b"mp4"

    # 同画幅再跑一次：复用，不再调用 H3
    _FakeRunner.calls = []
    status, _ = app.batch_render_with_diffusers(progress=_noop_progress)
    assert _FakeRunner.calls == [] and "复用已有视频 1" in status
