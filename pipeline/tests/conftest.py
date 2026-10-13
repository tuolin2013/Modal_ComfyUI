import sys
from pathlib import Path

import pytest

# pipeline 里的模块按脚本目录互相 import（如 import ecom_images），测试时把 pipeline/ 放进 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _isolated_asset_library(tmp_path, monkeypatch):
    """素材库指到临时目录：测试不读、不写真实的 output/ecom/product"""
    import asset_library
    monkeypatch.setattr(asset_library, "LIB_DIR", tmp_path / "asset_library")

