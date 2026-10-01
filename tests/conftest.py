"""所有測試只使用 pytest 暫存資料，不接觸真實搜尋根目錄。"""

import json
from pathlib import Path

import pytest

from backend import Config, Root


@pytest.fixture
def project(tmp_path):
    roots = tuple(Root(f"測試根目錄 {index}", tmp_path / f"root{index}") for index in range(3))
    for root in roots:
        root.path.mkdir()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"roots": [{"name": root.name, "path": str(root.path)} for root in roots]}, ensure_ascii=False), encoding="utf-8")
    return Config(roots), config_path
