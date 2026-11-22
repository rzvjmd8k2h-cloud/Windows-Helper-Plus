"""pytest 全局隔离：不触碰真实用户配置/报告目录。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_app_data(tmp_path, monkeypatch):
    monkeypatch.setenv("HELPER_PLUS_DATA_DIR", str(tmp_path / "helper-plus-data"))
    for name in (
        "DEEPSEEK_API_KEY",
        "HELPER_PLUS_API_KEY",
        "DEEPSEEK_BASE_URL",
        "HELPER_PLUS_BASE_URL",
        "HELPER_PLUS_MODEL",
        "DEEPSEEK_MODEL",
        "HELPER_PLUS_REASONING_EFFORT",
    ):
        monkeypatch.delenv(name, raising=False)
    yield tmp_path
