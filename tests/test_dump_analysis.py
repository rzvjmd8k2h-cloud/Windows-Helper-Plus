from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import helper_plus.tools.dump_analysis as dump_module
from helper_plus.config import AppConfig
from helper_plus.tools import create_registry
from helper_plus.tools.base import ToolContext

KERNEL_OUTPUT = """
*******************************************************************************
*                        Bugcheck Analysis                                    *
*******************************************************************************

DRIVER_IRQL_NOT_LESS_OR_EQUAL (d1)
An attempt was made to access a pageable address at an IRQL that was too high.

Arguments:
Arg1: 0000000000000020, memory referenced
Arg2: 0000000000000002, IRQL
Arg3: 0000000000000000, (reserved)
Arg4: fffff80512345678, address which referenced memory

BUGCHECK_CODE:  d1
BUGCHECK_P1: 20
BUGCHECK_P2: 2
BUGCHECK_P3: 0
BUGCHECK_P4: fffff80512345678
PROCESS_NAME:  System

STACK_TEXT:
fffff805`11111111 fffff805`22222222 nt!KeBugCheckEx
fffff805`11111112 fffff805`33333333 nvlddmkm+0x123456
fffff805`11111113 fffff805`44444444 nt!KiDispatchException

SYMBOL_NAME:  nvlddmkm+123456
MODULE_NAME: nvlddmkm
IMAGE_NAME:  nvlddmkm.sys
FAILURE_BUCKET_ID:  0xD1_nvlddmkm!unknown_function
OS_VERSION:  10.0.22621.1
Probably caused by : nvlddmkm.sys ( nvlddmkm+0x123456 )
"""

USER_OUTPUT = """
This is a user dump.
User Mini Dump File: Only registers, stack and portions of memory are available

ExceptionCode: c0000005
EXCEPTION_CODE: (NTSTATUS) 0xc0000005 - The instruction at 0x%p referenced memory at 0x%p.
PROCESS_NAME:  demo.exe
MODULE_NAME: demo
IMAGE_NAME:  demo.exe

STACK_TEXT:
00007ff6`11111111 00007ff6`22222222 demo!crash+0x10
00007ff6`11111112 00007ff6`33333333 demo!main+0x20
"""


def _ctx(target: Path) -> ToolContext:
    return ToolContext(
        target=target,
        config=AppConfig(),
        cancel_event=threading.Event(),
    )


def test_parse_kernel_dump_output():
    parsed = dump_module.parse_analyze_output(KERNEL_OUTPUT, max_stack_frames=10)
    assert parsed["dump_type"] == "kernel"
    assert parsed["bugcheck_code"] == "d1"
    assert parsed["bugcheck_name"] == "DRIVER_IRQL_NOT_LESS_OR_EQUAL"
    assert parsed["bugcheck_arguments"][0]["value"] == "0000000000000020"
    assert parsed["process_name"] == "System"
    assert parsed["module_name"] == "nvlddmkm"
    assert parsed["image_name"] == "nvlddmkm.sys"
    assert "nvlddmkm.sys" in parsed["probably_caused_by"]
    assert len(parsed["stack_text"]) == 3
    assert parsed["stack_text"][1].startswith("fffff805")
    assert parsed["has_bugcheck"] is True


def test_parse_user_dump_output():
    parsed = dump_module.parse_analyze_output(USER_OUTPUT, max_stack_frames=10)
    assert parsed["dump_type"] == "user"
    assert parsed["process_name"] == "demo.exe"
    assert parsed["image_name"] == "demo.exe"
    assert parsed["exception_code"] == "0xc0000005"
    assert parsed["has_bugcheck"] is True
    assert len(parsed["stack_text"]) == 2


def test_collect_dump_candidates(tmp_path, monkeypatch):
    target = tmp_path / "dumps"
    target.mkdir()
    first = target / "a.dmp"
    second = target / "b.mdmp"
    first.write_bytes(b"x")
    second.write_bytes(b"y")
    (target / "ignore.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(dump_module, "_dump_sources", lambda _target: [target])
    candidates = dump_module.collect_dump_candidates(target)
    names = {item["name"] for item in candidates}
    assert names == {"a.dmp", "b.mdmp"}


def test_analyze_dump_with_fake_cdb(tmp_path, monkeypatch):
    dump = tmp_path / "MEMORY.DMP"
    dump.write_bytes(b"fake dump")
    monkeypatch.setattr(dump_module, "find_cdb", lambda: tmp_path / "cdb.exe")
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return SimpleNamespace(
            stdout=KERNEL_OUTPUT,
            stderr="",
            returncode=0,
            duration_ms=42,
            timed_out=False,
            cancelled=False,
        )

    monkeypatch.setattr(dump_module, "run_process", fake_run)
    result = create_registry().invoke(
        "analyze_dump", {"path": str(dump)}, _ctx(dump)
    )
    assert result.ok, result.error
    parsed = result.data["parsed"]
    assert parsed["bugcheck_code"] == "d1"
    assert parsed["image_name"] == "nvlddmkm.sys"
    assert "BugCheck 0xd1" in result.summary
    assert "nvlddmkm.sys" in result.summary
    assert captured["argv"][0].endswith("cdb.exe")
    assert "-z" in captured["argv"]
    assert "!analyze -v" in captured["argv"][-1]


def test_analyze_dump_without_cdb_returns_candidates(tmp_path, monkeypatch):
    dump = tmp_path / "app.dmp"
    dump.write_bytes(b"fake dump")
    monkeypatch.setattr(dump_module, "find_cdb", lambda: None)
    result = create_registry().invoke(
        "analyze_dump", {"path": str(dump)}, _ctx(dump)
    )
    assert result.ok is False
    assert "未找到 cdb.exe" in result.summary
    assert "Debugging Tools" in (result.error or "")
    assert result.data["analyzer_available"] is False
    assert any(item["name"] == "app.dmp" for item in result.data["candidates"])


def test_analyze_dump_without_any_dump(tmp_path, monkeypatch):
    monkeypatch.setattr(dump_module, "collect_dump_candidates", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(dump_module, "find_cdb", lambda: None)
    result = create_registry().invoke("analyze_dump", {}, _ctx(tmp_path))
    assert result.ok is True
    assert result.data["analyzed"] is None
    assert "未发现可分析的 dump" in result.summary


def test_analyze_dump_rejects_non_dump_path(tmp_path):
    other = tmp_path / "notes.txt"
    other.write_text("x", encoding="utf-8")
    result = create_registry().invoke(
        "analyze_dump", {"path": str(other)}, _ctx(other)
    )
    assert result.ok is False
    assert "需要 .dmp" in (result.error or "")
