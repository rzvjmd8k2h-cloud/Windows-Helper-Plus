"""故障注入测试：构造突发故障，验证 WHP 的诊断证据与修复闭环。

覆盖场景：
- 依赖 DLL 缺失（PE 导入表）
- 项目依赖/本地引用缺失（requirements / csproj）
- PE 文件损坏
- 配置文件语法损坏
- 修复命令失败后的复核
- 编排器“诊断 → 受控修复 → 复验 → 结论”全链路（脚本化 Planner，真实工具与动作执行器）
- 中高风险动作必须经用户确认的安全闸门

所有写入都发生在 pytest 临时目录；动作执行器只操作临时目录，不修改系统。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

import helper_plus.core.orchestrator as orchestrator_module
from helper_plus.config import AppConfig
from helper_plus.core.actions import execute_action
from helper_plus.core.orchestrator import DiagnosisSink, Orchestrator, OrchestratorOptions
from helper_plus.llm.client import LLMStreamResult
from helper_plus.models import ToolResult, ToolStep
from helper_plus.tools import create_registry
from helper_plus.tools.base import ToolContext

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="故障注入依赖 Windows 工具与动作执行器")

_APP_DIR = "app with space"  # 带空格的目录用于验证修复命令的引号处理


def _ctx(target: Path | None = None) -> ToolContext:
    return ToolContext(target=target, config=AppConfig(), cancel_event=threading.Event())


def _copy_python_exe(destination: Path) -> Path:
    """复制不带 python3xx.dll 的解释器，制造真实的“缺失 DLL”故障。"""
    source = Path(sys.base_prefix) / "python.exe"
    if not source.exists():
        pytest.skip("当前 Python 安装缺少 python.exe，无法构造缺失 DLL 场景")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination


def _missing_python_dll(tmp_path: Path) -> tuple[Path, ToolResult]:
    exe = _copy_python_exe(tmp_path / _APP_DIR / "app.exe")
    result = create_registry().invoke(
        "extended_diagnostics", {"mode": "pe_dependencies"}, _ctx(exe)
    )
    assert result.ok, result.error
    return exe, result


# --------------------------------------------------------------------- 场景 1
def test_missing_dll_detected_and_repaired(tmp_path):
    exe, before = _missing_python_dll(tmp_path)
    missing = [name.lower() for name in before.data["missing_dlls"]]
    assert missing, "构造的 exe 应当缺少至少一个依赖 DLL"
    assert "python314.dll" in missing or any(name.startswith("python") for name in missing)
    # API Set 契约不能被误报为缺失 DLL。
    assert not any(name.startswith(("api-ms-win-", "ext-ms-")) for name in missing)
    assert "缺失" in before.summary and "app.exe" in before.summary
    checks = {item["dll"].lower(): item for item in before.data["dll_checks"]}
    assert checks, "已解析的 DLL 应当带架构/版本校验结果"
    assert all(item["compatible"] is not False for item in checks.values())
    assert any(item["file_version"] for item in checks.values()), "应至少读出一个 DLL 的版本资源"

    dll_name = before.data["missing_dlls"][0]
    source_dll = Path(sys.base_prefix) / dll_name
    assert source_dll.exists(), f"基础 Python 目录应包含 {dll_name}"

    outcome = execute_action(
        {
            "title": f"复制缺失依赖 {dll_name}",
            "command": f'copy /Y "{source_dll}" "{exe.parent / dll_name}"',
            "shell": "cmd",
            "risk": "low",
            "requires_admin": False,
        }
    )
    assert outcome.ok, outcome.error or outcome.stderr
    assert (exe.parent / dll_name).exists()

    after = create_registry().invoke(
        "extended_diagnostics", {"mode": "pe_dependencies"}, _ctx(exe)
    )
    assert after.ok, after.error
    assert dll_name.lower() not in [name.lower() for name in after.data["missing_dlls"]]
    assert "缺失 0 个" in after.summary


# --------------------------------------------------------------------- 场景 2
def test_project_dependency_and_local_reference_fault(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text(
        "requests==2.32.0\nwhp_fault_missing_pkg==9.9.9\n", encoding="utf-8"
    )
    (project / "app.py").write_text(
        "import requests\nimport whp_fault_missing_module_xyz\n", encoding="utf-8"
    )
    (project / "App.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>'
        '<PackageReference Include="Newtonsoft.Json" Version="13.0.3" />'
        '<ProjectReference Include="Lib\\MissingLib.csproj" />'
        "</ItemGroup></Project>",
        encoding="utf-8",
    )
    registry = create_registry()
    before = registry.invoke("inspect_project_manifests", {"max_files": 10}, _ctx(project))
    assert before.ok, before.error
    assert "whp_fault_missing_module_xyz" in before.data["missing_python_modules"]
    assert "whp_fault_missing_pkg" in before.data["declared_dependencies"]
    declared_missing = {(item["name"], item["ecosystem"]) for item in before.data["missing_declared_dependencies"]}
    assert ("whp_fault_missing_pkg", "python") in declared_missing
    assert any("MissingLib.csproj" in item for item in before.data["missing_local_references"])
    assert "python" in before.data["project_types"] and "dotnet" in before.data["project_types"]

    # 修复：补齐本地引用与本地模块（模拟随包分发的本地依赖）。
    lib_dir = project / "Lib"
    lib_dir.mkdir()
    (lib_dir / "MissingLib.csproj").write_text("<Project />", encoding="utf-8")
    (project / "whp_fault_missing_module_xyz.py").write_text("VALUE = 1\n", encoding="utf-8")

    after = registry.invoke("inspect_project_manifests", {"max_files": 10}, _ctx(project))
    assert after.ok, after.error
    assert after.data["missing_local_references"] == []
    assert "whp_fault_missing_module_xyz" not in after.data["missing_python_modules"]


# --------------------------------------------------------------------- 场景 3
def test_declared_dependencies_not_installed_are_reported(tmp_path):
    project = tmp_path / "declared"
    project.mkdir()
    (project / "requirements.txt").write_text(
        "pytest>=8\nwhp_fault_declared_pkg_xyz==1.0.0\n", encoding="utf-8"
    )
    (project / "package.json").write_text(
        json.dumps(
            {
                "name": "fault-app",
                "version": "1.0.0",
                "dependencies": {"whp-fault-node-missing-xyz": "^1.0.0"},
            }
        ),
        encoding="utf-8",
    )
    (project / "App.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>'
        '<PackageReference Include="Whp.Fault.NuGet.Missing.Xyz" Version="1.0.0" />'
        "</ItemGroup></Project>",
        encoding="utf-8",
    )
    (project / "pyproject.toml").write_text(
        '[project]\nname = "fault-app"\nversion = "1.0.0"\n'
        '[project.optional-dependencies]\ndev = ["pytest>=8.0"]\n',
        encoding="utf-8",
    )
    registry = create_registry()
    before = registry.invoke("inspect_project_manifests", {"max_files": 10}, _ctx(project))
    assert before.ok, before.error
    missing = {(item["name"], item["ecosystem"]) for item in before.data["missing_declared_dependencies"]}
    assert ("whp_fault_declared_pkg_xyz", "python") in missing
    assert ("whp-fault-node-missing-xyz", "node") in missing
    assert ("Whp.Fault.NuGet.Missing.Xyz", "dotnet") in missing
    assert "pytest" not in {name for name, ecosystem in missing if ecosystem == "python"}
    assert "dev" not in {name for name, ecosystem in missing if ecosystem == "python"}
    assert before.data["declared_dependency_checked"] >= 5

    # 修复 Node 依赖后，仅该依赖从缺失清单消失。
    node_pkg = project / "node_modules" / "whp-fault-node-missing-xyz"
    node_pkg.mkdir(parents=True)
    (node_pkg / "package.json").write_text('{"name":"whp-fault-node-missing-xyz"}', encoding="utf-8")
    after = registry.invoke("inspect_project_manifests", {"max_files": 10}, _ctx(project))
    assert after.ok, after.error
    after_missing = {(item["name"], item["ecosystem"]) for item in after.data["missing_declared_dependencies"]}
    assert ("whp-fault-node-missing-xyz", "node") not in after_missing
    assert ("whp_fault_declared_pkg_xyz", "python") in after_missing


# --------------------------------------------------------------------- 场景 4
def test_corrupt_pe_is_reported_then_replaced(tmp_path):
    broken = tmp_path / "broken.sys"
    broken.write_bytes(b"MZ" + b"\x00" * 120)
    registry = create_registry()
    before = registry.invoke("extended_diagnostics", {"mode": "pe_dependencies"}, _ctx(broken))
    assert before.ok is False
    assert before.error and "PE" in before.error

    valid_source = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "kernel32.dll"
    if not valid_source.exists():
        pytest.skip("缺少可用于替换的有效 PE 文件")
    shutil.copy2(valid_source, broken)

    after = registry.invoke("extended_diagnostics", {"mode": "pe_dependencies"}, _ctx(broken))
    assert after.ok, after.error
    assert after.data["import_dlls"]


# --------------------------------------------------------------------- 场景 4
def test_broken_json_config_evidence_and_repair(tmp_path):
    config = tmp_path / "appsettings.json"
    config.write_text('{"server": "http://127.0.0.1:8080", "retry": 3,}', encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        json.loads(config.read_text(encoding="utf-8"))

    registry = create_registry()
    evidence = registry.invoke("inspect_file", {"path": str(config)}, _ctx(config))
    assert evidence.ok, evidence.error
    assert evidence.data["content_kind"] == "text"
    assert "127.0.0.1:8080" in (evidence.data["content_preview"] or "")
    assert "配置" in evidence.data["content_role_hint"]

    outcome = execute_action(
        {
            "title": "重写损坏的 JSON 配置",
            "command": (
                "Set-Content -LiteralPath '{}' -Encoding UTF8 -Value "
                "'{{\"server\":\"http://127.0.0.1:8080\",\"retry\":3}}'"
            ).format(config),
            "shell": "powershell",
            "risk": "medium",
            "requires_admin": False,
        }
    )
    assert outcome.ok, outcome.error or outcome.stderr
    repaired = json.loads(config.read_text(encoding="utf-8-sig"))
    assert repaired == {"server": "http://127.0.0.1:8080", "retry": 3}


# --------------------------------------------------------------------- 场景 5
def test_failed_repair_is_visible_and_fault_remains(tmp_path):
    exe, before = _missing_python_dll(tmp_path)
    dll_name = before.data["missing_dlls"][0]
    outcome = execute_action(
        {
            "title": "从不存在的备份复制依赖",
            "command": f'copy /Y "{tmp_path / "backup" / dll_name}" "{exe.parent / dll_name}"',
            "shell": "cmd",
            "risk": "low",
            "requires_admin": False,
        }
    )
    assert outcome.ok is False
    assert outcome.returncode not in (0, None)

    after = create_registry().invoke(
        "extended_diagnostics", {"mode": "pe_dependencies"}, _ctx(exe)
    )
    assert after.ok
    assert dll_name in after.data["missing_dlls"]


# --------------------------------------------------------------------- 场景 6
def test_file_locks_reports_loaded_module():
    target = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "kernel32.dll"
    if not target.exists():
        pytest.skip("缺少 kernel32.dll")
    result = create_registry().invoke(
        "extended_diagnostics", {"mode": "file_locks"}, _ctx(target)
    )
    assert result.ok, result.error
    matches = result.data["matches"]
    assert matches, "运行中的进程应当加载了 kernel32.dll"
    assert all(item.get("process") and item.get("module") for item in matches)


# --------------------------------------------------------------------- 场景 7
def test_wrong_bitness_dll_is_flagged(tmp_path):
    exe = _copy_python_exe(tmp_path / _APP_DIR / "app.exe")
    dll_name = "python314.dll"
    wow64_dll = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "SysWOW64" / "kernel32.dll"
    if not wow64_dll.exists():
        pytest.skip("缺少 32 位系统 DLL")
    shutil.copy2(wow64_dll, exe.parent / dll_name)  # 32 位 DLL 冒充 64 位依赖

    result = create_registry().invoke(
        "extended_diagnostics", {"mode": "pe_dependencies"}, _ctx(exe)
    )
    assert result.ok, result.error
    incompatible = {item["dll"].lower(): item for item in result.data["incompatible_dlls"]}
    assert dll_name in incompatible
    item = incompatible[dll_name]
    assert item["actual_machine"] == "x86"
    assert item["expected_machine"] == result.data["machine"]
    assert dll_name not in [name.lower() for name in result.data["missing_dlls"]]
    assert "架构不匹配" in result.summary
    checks = {entry["dll"].lower(): entry for entry in result.data["dll_checks"]}
    assert checks[dll_name]["compatible"] is False


# ------------------------------------------------ 编排器全链路（诊断→修复→复验）
def _fake_baseline(registry, store, ctx, *, start_sequence=1, on_step=None, on_phase=None, on_progress=None):
    result = ToolResult(
        ok=True,
        summary="Windows 测试基线：构建版本 22631",
        data={"os_caption": "Microsoft Windows 11 Pro", "build_number": "22631"},
        source="test",
    )
    evidence = store.add("get_system_info", result, target=None)
    step = ToolStep(
        sequence=start_sequence,
        tool="get_system_info",
        arguments={},
        basis=["Q1"],
        narration="测试基线",
        why="测试",
        evidence_id=evidence.id,
        ok=True,
        origin="baseline",
    )
    if on_phase:
        on_phase("测试基线完成")
    if on_step:
        on_step(step, evidence)
    return [step]


class _FakePlanner:
    responses: list[str] = []
    instances: list["_FakePlanner"] = []

    def __init__(self, config, api_key, cancel_event):
        self.requests: list[list[dict[str, Any]]] = []
        _FakePlanner.instances.append(self)

    def stream(self, messages, *, on_reasoning=None, on_content=None, on_attempt=None):
        self.requests.append(messages)
        text = _FakePlanner.responses.pop(0)
        if on_content:
            for index in range(0, len(text), 90):
                on_content(text[index : index + 90])
        return LLMStreamResult(
            content=text, reasoning="测试思考", finish_reason="stop", usage={"total_tokens": 10}
        )


class _InteractiveSink(DiagnosisSink):
    def __init__(self, approve: bool = True) -> None:
        self.approve = approve
        self.proposals: list[dict[str, Any]] = []
        self.finals: list[dict[str, Any]] = []
        self.plan: dict[str, Any] | None = None
        self.phases: list[str] = []

    def on_phase(self, text: str) -> None:
        self.phases.append(text)

    def on_plan(self, plan: dict[str, Any]) -> None:
        self.plan = plan

    def confirm_actions(self, proposal: dict[str, Any]) -> bool:
        self.proposals.append(proposal)
        return self.approve

    def on_final(self, report: dict[str, Any]) -> None:
        self.finals.append(report)

    def ask_user(self, question: dict[str, Any]) -> str:
        return "（测试环境无交互）"


def _plan_json(tools: list[str]) -> str:
    return json.dumps(
        {
            "narration": "基线 [E1] 已就绪，先展示执行计划。",
            "action": "plan",
            "basis": ["E1"],
            "plan": {
                "goal": "定位并修复缺失的依赖 DLL。",
                "confirmed_user_goal": "主模型确认：定位程序启动失败原因并验证缺失 DLL 修复。",
                "steps": ["解析 PE 依赖", "补齐缺失 DLL", "复查依赖是否完整", "给出结论"],
                "tools": tools,
                "notes": "修复动作需用户确认。",
            },
        },
        ensure_ascii=False,
    )


def _call_tool_json(exe: Path, basis: list[str]) -> str:
    return json.dumps(
        {
            "narration": "解析目标 PE 的导入表，确认缺失 DLL。",
            "action": "call_tool",
            "basis": basis,
            "tool": "extended_diagnostics",
            "arguments": {"mode": "pe_dependencies", "path": str(exe)},
            "why": "PE 导入表可直接证明依赖缺失。",
        },
        ensure_ascii=False,
    )


def _propose_copy_json(exe: Path, dll_name: str, basis: list[str]) -> str:
    source = Path(sys.base_prefix) / dll_name
    return json.dumps(
        {
            "narration": "从同版本 Python 安装目录补齐缺失依赖。",
            "action": "propose_action",
            "basis": basis,
            "actions": [
                {
                    "title": f"复制缺失依赖 {dll_name}",
                    "command": f'copy /Y "{source}" "{exe.parent / dll_name}"',
                    "shell": "cmd",
                    "risk": "low",
                    "requires_admin": False,
                    "rationale": "缺失 DLL 来自同版本 Python 安装目录，属于可回滚的文件复制。",
                    "rollback": f"删除 {exe.parent / dll_name} 即可回滚。",
                }
            ],
        },
        ensure_ascii=False,
    )


def _final_json(refs: list[str], headline: str) -> str:
    head, tail = refs[0], refs[-1]
    cited = "".join(f"[{ref}]" for ref in refs)
    return json.dumps(
        {
            "narration": "证据已闭环，给出结论。",
            "action": "final",
            "basis": refs,
            "final": {
                "headline": headline,
                "summary": f"PE 依赖扫描与修复结果均已记录 {cited}。",
                "findings": [
                    {
                        "text": "程序依赖表中存在缺失 DLL，修复后缺失项消失。",
                        "evidence": [head, tail],
                        "confidence": 0.95,
                    }
                ],
                "candidate_causes": [
                    {
                        "cause": "依赖 DLL 未随程序一起分发。",
                        "confidence": 0.9,
                        "evidence": [head],
                        "supporting": "PE 导入表列出缺失文件。",
                        "against": "尚未验证程序完整启动路径。",
                    }
                ],
                "next_steps": [
                    {
                        "title": "再次启动程序确认不再报缺少 DLL。",
                        "why": "需要运行期验证。",
                        "risk": "read-only",
                        "executed": False,
                        "evidence": [tail],
                    }
                ],
                "limitations": ["测试使用复制文件模拟修复。"],
            },
        },
        ensure_ascii=False,
    )


def _run_orchestrator(monkeypatch, tmp_path, responses, sink, mode="strict"):
    _FakePlanner.responses = responses
    _FakePlanner.instances = []
    monkeypatch.setattr(orchestrator_module, "collect_baseline", _fake_baseline)
    monkeypatch.setattr(orchestrator_module, "DeepSeekClient", _FakePlanner)
    orchestrator = Orchestrator(
        AppConfig(),
        target=tmp_path / _APP_DIR / "app.exe",
        issue="程序启动提示缺少 DLL，请定位并验证修复。",
        sink=sink,
        cancel_event=threading.Event(),
        registry=create_registry(),
        options=OrchestratorOptions(
            api_key="sk-test",
            permission_mode=mode,
        ),
    )
    return orchestrator.run()


def test_orchestrator_diagnose_repair_verify_closed_loop(monkeypatch, tmp_path):
    exe, before = _missing_python_dll(tmp_path)
    dll_name = before.data["missing_dlls"][0]
    sink = _InteractiveSink(approve=True)
    record = _run_orchestrator(
        monkeypatch,
        tmp_path,
        [
            _plan_json(["extended_diagnostics", "propose_action"]),
            _call_tool_json(exe, ["E1"]),
            _propose_copy_json(exe, dll_name, ["E1", "E2"]),
            _call_tool_json(exe, ["E2", "E3"]),
            _final_json(["E2", "E3", "E4"], "缺失 DLL 已修复并通过复验。"),
        ],
        sink,
    )
    assert record.status == "completed", record.errors
    assert sink.plan and sink.plan["confirmed_user_goal"].startswith("主模型确认")
    assert sink.proposals and sink.proposals[0]["actions"][0]["risk"] == "low"

    pe_evidence = [item for item in record.evidence if item["tool"] == "extended_diagnostics"]
    assert len(pe_evidence) == 2
    assert dll_name in pe_evidence[0]["data"]["missing_dlls"]
    assert dll_name not in pe_evidence[1]["data"]["missing_dlls"]
    action_evidence = [item for item in record.evidence if item["tool"] == "action_execute"]
    assert len(action_evidence) == 1 and action_evidence[0]["ok"] is True
    assert (exe.parent / dll_name).exists()
    assert record.report and "已修复" in record.report["headline"]
    assert record.actions and record.actions[0]["ok"] is True


def test_orchestrator_medium_risk_requires_user_confirmation(monkeypatch, tmp_path):
    exe, before = _missing_python_dll(tmp_path)
    dll_name = before.data["missing_dlls"][0]
    propose = json.loads(_propose_copy_json(exe, dll_name, ["E1", "E2"]))
    propose["actions"][0]["risk"] = "medium"
    sink = _InteractiveSink(approve=False)
    record = _run_orchestrator(
        monkeypatch,
        tmp_path,
        [
            _plan_json(["extended_diagnostics", "propose_action"]),
            _call_tool_json(exe, ["E1"]),
            json.dumps(propose, ensure_ascii=False),
            _final_json(["E2"], "中风险动作等待用户确认，未执行。"),
        ],
        sink,
        mode="trusted",
    )
    assert record.status == "completed", record.errors
    assert sink.proposals, "中风险动作必须调用用户确认"
    assert record.actions and record.actions[0]["approved"] is False
    assert not [item for item in record.evidence if item["tool"] == "action_execute"]
    assert not (exe.parent / dll_name).exists()
