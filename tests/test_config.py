"""配置默认值、环境变量覆盖与持久化测试。"""

from __future__ import annotations

import json

import pytest

from helper_plus.config import AppConfig


def test_defaults_use_latest_deepseek_and_max_thinking():
    config = AppConfig()
    assert config.model == "deepseek-flash"
    assert config.reasoning_effort == "max"
    assert config.thinking is True
    assert config.max_rounds == 0  # 0 = 不限制步数
    assert config.max_tokens == 0  # 0 = 不发送 max_tokens
    assert config.context_budget_chars >= 50_000
    assert "DeepSeek" in config.model_label and "Max" in config.model_label


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("HELPER_PLUS_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("HELPER_PLUS_REASONING_EFFORT", "high")
    monkeypatch.setenv("HELPER_PLUS_BASE_URL", "http://127.0.0.1:9999")
    config = AppConfig()
    assert config.model == "deepseek-v4-pro"
    assert config.reasoning_effort == "high"
    assert config.base_url == "http://127.0.0.1:9999"


def test_bad_base_url_rejected():
    with pytest.raises(ValueError):
        AppConfig(base_url="ftp://example.com")


def test_config_save_load_roundtrip(tmp_path):
    path = tmp_path / "config.json"
    config = AppConfig(
        model="deepseek-v4-pro",
        reasoning_effort="high",
        max_rounds=4,
        max_tokens=4096,
    )
    config.save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "api_key" not in raw
    loaded = AppConfig.load(path)
    assert loaded.model == "deepseek-v4-pro"
    assert loaded.reasoning_effort == "high"
    assert loaded.max_rounds == 4
    assert loaded.max_tokens == 4096


def test_numeric_values_are_normalized():
    config = AppConfig(max_rounds=0, max_tokens=0, timeout_seconds=1)
    assert config.max_rounds == 0
    assert config.max_tokens == 0
    assert config.timeout_seconds >= 30
    huge = AppConfig(max_tokens=999_999, max_rounds=99_999)
    assert huge.max_tokens == 384_000
    assert huge.max_rounds == 10_000
