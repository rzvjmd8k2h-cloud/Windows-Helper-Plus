"""系统兼容性、自检与沙箱策略测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from helper_plus.config import AppConfig
from helper_plus.system_compat import classify_windows_build, collect_self_check, get_os_info


def test_classify_windows_build():
    assert classify_windows_build(19045)[1] is True
    assert "Windows 10" in classify_windows_build(19045)[0]
    assert classify_windows_build(22631)[1] is True
    assert "Windows 11" in classify_windows_build(22631)[0]
    assert classify_windows_build(7601)[1] is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 自检")
def test_self_check_reports_windows_and_local_model():
    result = collect_self_check(AppConfig())
    assert result["os"]["build"] >= 10240
    assert result["checks"]
    names = [item["name"] for item in result["checks"]]
    assert "Windows 版本适配" in names
    assert "本地 Qwen 模型" in names
    assert "沙箱后端" in names


def test_sandbox_plan_strategy(tmp_path):
    from helper_plus.sandbox import build_sandbox_plan, create_windows_sandbox_plan

    target = tmp_path / "sample.exe"
    target.write_bytes(b"MZ" + b"\x00" * 100)
    plan = build_sandbox_plan(target, "测试")
    assert plan["strategic_role"]
    assert plan["user_confirmation_required"] is True
    assert plan["auto_start"] is False
    assert "capabilities" in plan
    windows = create_windows_sandbox_plan(target, "测试")
    wsb = Path(windows["wsb_path"])
    assert wsb.exists()
    assert "<MappedFolder>" in wsb.read_text(encoding="utf-8")
    assert Path(windows["script_path"]).exists()
