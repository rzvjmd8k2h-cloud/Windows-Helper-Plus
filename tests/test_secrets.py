"""DPAPI 密钥保存测试（仅在 Windows 上运行）。"""

from __future__ import annotations

import sys

import pytest

from helper_plus.secrets import (
    clear_api_key,
    get_api_key,
    has_stored_api_key,
    load_stored_api_key,
    save_api_key,
)


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI 仅 Windows")


def test_dpapi_roundtrip_and_clear(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    save_api_key("sk-test-not-a-real-key")
    assert has_stored_api_key()
    assert load_stored_api_key() == "sk-test-not-a-real-key"
    assert get_api_key() == "sk-test-not-a-real-key"
    # 会话内 Key 优先级高于磁盘。
    assert get_api_key("sk-session") == "sk-session"
    clear_api_key()
    assert not has_stored_api_key()
    assert load_stored_api_key() == ""


def test_empty_save_clears(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    save_api_key("sk-test")
    save_api_key("   ")
    assert not has_stored_api_key()
