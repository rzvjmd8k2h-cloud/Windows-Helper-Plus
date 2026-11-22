"""工具安全边界、参数校验与纯解析函数测试。"""

from __future__ import annotations

import hashlib
import sys

import pytest

from helper_plus.safety import SafetyError, validate_inspect_path, validate_user_target
from helper_plus.tools import create_registry
from helper_plus.tools.setupapi import extract_setupapi_errors
from helper_plus.tools.wer import _parse_wer_text


def test_registry_is_readonly_and_complete():
    registry = create_registry()
    assert len(registry.names) == 24
    expected = {
        "get_system_info",
        "inspect_file",
        "inspect_directory",
        "inspect_project_manifests",
        "extended_diagnostics",
        "get_event_logs",
        "get_reliability_records",
        "get_driver_info",
        "map_related_files",
        "analyze_dump",
        "get_wer_reports",
        "get_setupapi_log",
        "get_windows_updates",
        "get_device_info",
        "get_printer_info",
        "get_network_info",
        "run_sfc_verify",
        "run_dism_scan",
        "enum_drivers_pnputil",
        "verify_signature_signtool",
        "wevtutil_query",
        "sandbox_control",
        "web_search",
        "web_fetch",
    }
    assert set(registry.names) == expected
    assert all(item["read_only"] for item in registry.catalog())


def test_registry_rejects_unknown_tool_and_bad_args():
    registry = create_registry()
    with pytest.raises(SafetyError, match="白名单"):
        registry.validate_call("run_sfc", {})
    with pytest.raises(SafetyError):
        registry.validate_call("get_event_logs", {"log": "Security"})
    with pytest.raises(SafetyError):
        registry.validate_call("get_event_logs", {"log": "System", "hours": 9999})
    with pytest.raises(SafetyError):
        registry.validate_call("get_event_logs", {"log": "System", "unexpected": 1})
    with pytest.raises(SafetyError):
        registry.validate_call("get_system_info", {"x": 1})


def test_inspect_file_trusted_path_scope(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_text("hello", encoding="utf-8")
    assert validate_inspect_path(None, target) == target.resolve()
    assert validate_inspect_path(str(target), target) == target.resolve()
    other = tmp_path / "other.txt"
    other.write_text("nope", encoding="utf-8")
    # 最高权限/信任模式：只读工具调用不被路径隐私边界自我拦截。
    assert validate_inspect_path(str(other), target) == other.resolve()
    with pytest.raises(SafetyError, match="没有目标"):
        validate_inspect_path(None, None)


def test_user_target_supports_directory_in_v2(tmp_path):
    directory = tmp_path / "folder"
    directory.mkdir()
    assert validate_user_target(str(directory), allow_directory=True) == directory.resolve()
    with pytest.raises(SafetyError, match="单个文件"):
        validate_user_target(str(directory))
    assert validate_user_target("") is None


def test_setupapi_error_extraction_filters_noise():
    text = "\n".join(
        [
            "dvi: {Create Device - exit(0x00000000)}",
            "!!! sig: Verifying file against catalog 'foo.cat' failed.",
            "sig: Error 2: The system cannot find the file specified.",
            "inf: Flags: 0x000007af",
            "dvi: Device required reboot: Query remove failed (install) 0x33: CR_ACCESS_DENIED.",
        ]
    )
    lines = extract_setupapi_errors(text, 10)
    joined = "\n".join(lines)
    assert "!!! sig" in joined
    assert "CR_ACCESS_DENIED" in joined
    assert "Create Device" not in joined
    assert "Flags:" not in joined


def test_parse_wer_text():
    text = "EventType=APPCRASH\nAppName=test.exe\nAppPath=C:\\Users\\x\\test.exe\nSig[0].Name=ExceptionCode\nSig[0].Value=0xc0000005\n"
    parsed = _parse_wer_text(text)
    assert parsed["EventType"] == "APPCRASH"
    assert parsed["AppName"] == "test.exe"
    assert parsed["Sig[0].Value"] == "0xc0000005"


def test_file_content_profile_distinguishes_notes_from_faulty_binary(tmp_path):
    from helper_plus.tools.file_inspect import _content_profile

    note = tmp_path / "问题说明.txt"
    note.write_text("打开桌面文件时弹出这些文件可能有害", encoding="utf-8")
    profile = _content_profile(note)
    assert profile["content_kind"] == "text"
    assert "问题描述" in profile["content_role_hint"]
    assert "打开桌面文件" in profile["content_preview"]

    exe = tmp_path / "sample.exe"
    exe.write_bytes(b"MZ" + b"\x00" * 64)
    binary = _content_profile(exe)
    assert binary["content_kind"] == "executable"
    assert binary["content_preview"] is None


@pytest.mark.skipif(sys.platform != "win32", reason="只读 PowerShell 工具仅在 Windows 运行")
def test_inspect_file_real(tmp_path):
    import threading

    from helper_plus.config import AppConfig
    from helper_plus.tools.base import ToolContext

    sample = tmp_path / "helper_plus_test.bin"
    sample.write_bytes(b"Windows Helper Plus test payload")
    ctx = ToolContext(target=sample, config=AppConfig(), cancel_event=threading.Event())
    registry = create_registry()
    result = registry.invoke("inspect_file", {"path": str(sample)}, ctx)
    assert result.ok, result.error
    assert result.data["sha256"] == hashlib.sha256(sample.read_bytes()).hexdigest()
    assert result.data["signature_code"] in {
        "valid_microsoft",
        "valid_third_party",
        "not_signed",
        "hash_mismatch",
        "not_trusted",
        "check_failed",
    }
