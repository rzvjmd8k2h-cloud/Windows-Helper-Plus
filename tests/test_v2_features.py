"""v2 功能测试：计划、交互、动作提案、上下文压缩、动作执行与新工具。"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

import pytest

import helper_plus.core.actions as actions_module
from helper_plus.config import AppConfig
from helper_plus.safety import SafetyError
from helper_plus.core.actions import execute_action
from helper_plus.core.context import build_evidence_context
from helper_plus.core.validation import validate_decision
from helper_plus.models import EvidenceStore, ToolResult


def test_plan_decision():
    decision = validate_decision(
        {
            "narration": "基线 [E1] 已就绪，先展示计划。",
            "action": "plan",
            "basis": ["E1"],
            "plan": {
                "goal": "定位签名问题",
                "confirmed_user_goal": "主模型确认：定位目标文件的签名问题。",
                "steps": ["映射文件", "查日志", "验证签名"],
                "tools": ["map_related_files", "get_event_logs"],
                "notes": "必要时联网查官方文档。",
            },
        },
        {"E1"},
    )
    assert decision.action == "plan"
    assert decision.plan and len(decision.plan["steps"]) == 3


def test_ask_user_decision():
    decision = validate_decision(
        {
            "narration": "需要 [E1] 无法提供的时间线。",
            "action": "ask_user",
            "basis": ["E1"],
            "question": {
                "prompt": "问题何时出现？",
                "options": ["更新后", "一直存在"],
                "allow_free_text": True,
            },
        },
        {"E1"},
    )
    assert decision.question["prompt"] == "问题何时出现？"
    assert decision.question["options"][0] == "更新后"


def test_propose_action_decision():
    decision = validate_decision(
        {
            "narration": "根据 [E1] 提出受控修复。",
            "action": "propose_action",
            "basis": ["E1"],
            "actions": [
                {
                    "title": "运行 SFC",
                    "command": "sfc /scannow",
                    "shell": "cmd",
                    "risk": "high",
                    "requires_admin": True,
                    "rationale": "SFC 结果 [E1] 表明损坏。",
                    "rollback": "系统还原点。",
                }
            ],
        },
        {"E1"},
    )
    assert decision.actions[0]["requires_admin"] is True
    assert decision.actions[0]["risk"] == "high"


def test_call_tools_decision():
    decision = validate_decision(
        {
            "narration": "同时读取两个日志 [E1]。",
            "action": "call_tools",
            "basis": ["E1"],
            "tools": [
                {"tool": "get_event_logs", "arguments": {"log": "System"}},
                {"tool": "get_event_logs", "arguments": {"log": "Application"}},
            ],
            "why": "并行补充时间线。",
        },
        {"E1"},
    )
    assert decision.action == "call_tools"
    assert len(decision.tools) == 2


def test_context_compression_keeps_all_ids_and_limits_data():
    store = EvidenceStore()
    for index in range(60):
        store.add(
            "get_event_logs",
            ToolResult(
                ok=True,
                summary=f"evidence {index}",
                data={"events": [{"message": "x" * 5000}] * 10},
            ),
        )
    evidence, stats = build_evidence_context(store, budget_chars=60_000)
    assert len(evidence) == 60
    assert [item["id"] for item in evidence] == [f"E{i}" for i in range(1, 61)]
    assert stats.evidence_compressed > 0
    assert stats.estimated_chars < 500_000


def test_requires_admin_action_uses_uac_branch(monkeypatch):
    monkeypatch.setattr(actions_module, "_is_admin", lambda: False)
    calls: list[str] = []

    def fake_elevated(command, shell, timeout_seconds, cancel_event):
        calls.append(f"{shell}:{command}")
        return {"stdout": "uac-ok", "stderr": "", "returncode": 0, "error": None}

    monkeypatch.setattr(actions_module, "_execute_elevated", fake_elevated)
    outcome = execute_action(
        {
            "title": "需要管理员的修复",
            "command": "sfc /scannow",
            "shell": "cmd",
            "risk": "high",
            "requires_admin": True,
        }
    )
    assert calls == ["cmd:sfc /scannow"]
    assert outcome.elevated is True
    assert outcome.ok is True
    assert outcome.stdout == "uac-ok"


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows 动作执行")
def test_action_runner_echo():
    outcome = execute_action(
        {
            "title": "echo",
            "command": "echo helper-plus-action-test",
            "shell": "cmd",
            "risk": "low",
            "requires_admin": False,
        }
    )
    assert outcome.ok, outcome.error
    assert "helper-plus-action-test" in outcome.stdout
    assert outcome.duration_ms >= 0


def test_web_search_tool_uses_deepseek_search_api(monkeypatch):
    from helper_plus.tools import create_registry
    from helper_plus.tools.base import ToolContext
    from helper_plus.tools.web import validate_public_http_url

    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append({"path": self.path, "body": json.loads(body.decode("utf-8"))})
            payload = json.dumps(
                {
                    "results": [
                        {
                            "title": "官方文档",
                            "url": "https://learn.microsoft.com/example",
                            "snippet": "Windows 官方说明",
                            "summary": "摘要",
                        }
                    ]
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        config = AppConfig(base_url=f"http://127.0.0.1:{server.server_address[1]}")
        result = create_registry().invoke(
            "web_search",
            {"query": "Windows Event ID 41 官方", "count": 3},
            ToolContext(
                target=None,
                config=config,
                cancel_event=threading.Event(),
                api_key="sk-test",
            ),
        )
    finally:
        server.shutdown()
        server.server_close()
    assert result.ok, result.error
    assert requests and requests[0]["path"].endswith("/v1/tools/search")
    assert "HTTP" not in result.summary
    with pytest.raises(SafetyError):
        validate_public_http_url("http://127.0.0.1/secret")
    with pytest.raises(SafetyError):
        validate_public_http_url("file:///C:/Windows/win.ini")


def test_local_context_agent_archives_case_and_markdown(tmp_path):
    from helper_plus.local_context import LocalContextAgent

    class FakeClient:
        def chat_json(self, **kwargs):
            return {
                "user_goal_candidate": "修复打印机",
                "goal_clarity": "clear",
                "goal_notes": "明确",
                "completed": ["已识别设备"],
                "unfinished": ["验证打印"],
                "blocked": [],
                "open_questions": [],
                "related_prior_cases": [],
                "recent_turn_decisions": {"keep_turn_ids": ["T1"], "drop_turn_ids": [], "reason": "目标"},
                "relevant_recent_messages": [],
                "context_summary": "测试上下文",
                "risks": [],
                "recommended_focus": ["验证"],
            }

        def chat(self, **kwargs):
            return "# 本地 Qwen 日志\n\n测试 translation\n"

    agent = LocalContextAgent(FakeClient(), root=tmp_path)
    context = agent.analyze(
        record_id="DIAG-TEST",
        target="本机系统",
        target_kind="system",
        issue="打印机",
        status="running",
        elapsed_seconds=1,
        round_number=1,
        plan=None,
        evidence=[],
        steps=[],
        interactions=[],
        actions=[],
        recent_turns=[{"id": "T1", "role": "user", "kind": "goal", "content": "打印机"}],
    )
    assert context and context["user_goal_candidate"] == "修复打印机"
    record = {
        "record_id": "DIAG-TEST",
        "target": "本机系统",
        "target_kind": "system",
        "issue": "打印机",
        "status": "completed",
        "model": "deepseek-flash",
        "reasoning_effort": "max",
        "started_at": "2026-01-01T00:00:00+08:00",
        "finished_at": "2026-01-01T00:01:00+08:00",
        "elapsed_seconds": 60,
        "plan": {"goal": "修复打印机", "steps": ["检查"]},
        "evidence": [{"id": "E1", "tool": "get_printer_info", "ok": True, "summary": "脱机"}],
        "tool_steps": [],
        "interactions": [],
        "actions": [],
        "report": {"headline": "已修复", "summary": "测试 [E1]"},
        "errors": [],
    }
    case_path = agent.finalize(record=record, local_context=context, main_goal="修复打印机")
    assert case_path.exists()
    assert (tmp_path / "runs" / "DIAG-TEST.md").exists()
    assert (tmp_path / "病历" / "DIAG-TEST.json").exists()
    assert (tmp_path / "病历" / "index.jsonl").read_text(encoding="utf-8").strip()


def test_extended_diagnostics_sandbox_estimate_and_pe():
    from helper_plus.tools import create_registry
    from helper_plus.tools.base import ToolContext
    from pathlib import Path

    registry = create_registry()
    context = ToolContext(target=None, config=AppConfig(), cancel_event=threading.Event())
    sandbox = registry.invoke("extended_diagnostics", {"mode": "sandbox_plan"}, context)
    assert sandbox.ok
    assert sandbox.data["estimated_minutes"]["minimum"] >= 2
    assert sandbox.data["user_confirmation_required"] is True
    kernel = Path(r"C:\Windows\System32\kernel32.dll")
    pe = registry.invoke(
        "extended_diagnostics",
        {"mode": "pe_dependencies", "path": str(kernel)},
        context,
    )
    assert pe.ok
    assert pe.data["import_dlls"]
    assert pe.data["missing_dlls"] == []


def test_sandbox_control_status_and_plan_requires_confirmation(tmp_path):
    from helper_plus.tools import create_registry
    from helper_plus.tools.base import ToolContext

    target = tmp_path / "sample.exe"
    target.write_bytes(b"MZ" + b"\x00" * 100)
    registry = create_registry()
    context = ToolContext(target=target, config=AppConfig(), cancel_event=threading.Event())
    status = registry.invoke("sandbox_control", {"mode": "status"}, context)
    assert status.ok
    assert "backends" in status.data
    plan = registry.invoke("sandbox_control", {"mode": "plan"}, context)
    assert plan.ok
    assert plan.data["user_confirmation_required"] is True
    assert registry.get("sandbox_control").requires_confirmation is True


def test_ui_language_translation_and_config_normalization(tmp_path):
    from helper_plus.i18n import set_language, tr, tr_text

    try:
        set_language("en")
        assert tr("开始分析") == "Start Analysis"
        assert tr_text("证据 3") == "Evidence 3"
        assert tr_text("目标：C:\\temp") == "Target: C:\\temp"
        english = AppConfig(ui_language="EN")
        english.normalize()
        assert english.ui_language == "en"
        path = tmp_path / "config.json"
        english.save(path)
        assert AppConfig.load(path).ui_language == "en"
        invalid = AppConfig(ui_language="fr")
        invalid.normalize()
        assert invalid.ui_language == "zh"
        set_language("zh")
        assert tr("Start Analysis") == "Start Analysis"
        assert tr_text("Start Analysis") == "开始分析"
        assert tr_text("Target: C:\\temp") == "目标：C:\\temp"
    finally:
        set_language("zh")


def test_sandbox_plan_does_not_copy_and_handles_unicode(tmp_path, monkeypatch):
    import helper_plus.sandbox as sandbox_module

    log = tmp_path / "Log"
    monkeypatch.setattr(sandbox_module, "log_root", lambda: log)
    target_dir = tmp_path / "中文目录"
    target_dir.mkdir()
    (target_dir / "说明.txt").write_text("hello", encoding="utf-8")

    plan = sandbox_module.build_sandbox_plan(target_dir, "中文路径", prepare=False)
    assert plan["prepared"] is False
    assert not log.exists(), "只做计划时不得复制目标或写 Log"

    prepared = sandbox_module.create_windows_sandbox_plan(target_dir, "中文路径")
    assert prepared["prepared"] is True
    assert prepared["staged_target_path"]
    assert Path(prepared["staged_target_path"]).is_dir()
    assert Path(prepared["launcher_path"]).suffix == ".ps1"
    launcher_text = Path(prepared["launcher_path"]).read_text(encoding="utf-8-sig")
    assert "中文目录" in launcher_text
    assert prepared["execute_target"] is False  # 目录不执行


def test_sandbox_staging_limit_and_unprepared_run(tmp_path, monkeypatch):
    import helper_plus.sandbox as sandbox_module

    monkeypatch.setattr(sandbox_module, "log_root", lambda: tmp_path / "Log")
    monkeypatch.setattr(sandbox_module, "_STAGING_MAX_BYTES", 10)
    target = tmp_path / "big.bin"
    target.write_bytes(b"x" * 64)
    with pytest.raises(RuntimeError, match="目标过大"):
        sandbox_module.create_windows_sandbox_plan(target)

    controller = sandbox_module.SandboxController()
    unprepared = controller.run({"prepared": False})
    assert unprepared.ok is False
    assert "尚未准备" in (unprepared.error or "")

    missing_staging = controller.run(
        {
            "prepared": True,
            "wsb_path": str(tmp_path / "none.wsb"),
            "output_dir": str(tmp_path / "out"),
            "target_path": str(target),
            "staged_target_path": None,
        }
    )
    assert missing_staging.ok is False
    assert "暂存区" in (missing_staging.error or "")


def test_action_backup_restore_and_failed_backup_is_safe(tmp_path, monkeypatch):
    log = tmp_path / "Log"
    monkeypatch.setattr(actions_module, "log_root", lambda: log)
    target = tmp_path / "config.ini"
    target.write_text("original", encoding="utf-8")

    action = {
        "title": "修改配置",
        "command": f"Set-Content -LiteralPath '{target}' -Value 'changed'",
        "shell": "powershell",
        "risk": "low",
        "requires_admin": False,
        "backup_paths": [str(target)],
        "rollback": "从自动备份恢复",
    }
    outcome = execute_action(action)
    assert outcome.ok, outcome.error
    assert outcome.backup_dir and Path(outcome.backup_dir).is_file()
    assert target.read_text(encoding="utf-8").strip() == "changed"
    assert outcome.rollback_available is True

    restored = actions_module.restore_action_backup(outcome.backup_dir)
    assert restored["ok"], restored["errors"]
    assert target.read_text(encoding="utf-8") == "original"

    # 备份失败必须在执行命令前中止。
    sentinel = tmp_path / "should-not-exist.txt"
    original_copy2 = actions_module.shutil.copy2

    def _fail_copy(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(actions_module.shutil, "copy2", _fail_copy)
    failed = execute_action(
        {
            "title": "不应执行",
            "command": f"Set-Content -LiteralPath '{sentinel}' -Value 'x'",
            "shell": "powershell",
            "risk": "low",
            "backup_paths": [str(target)],
        }
    )
    monkeypatch.setattr(actions_module.shutil, "copy2", original_copy2)
    assert failed.ok is False
    assert "备份失败" in (failed.error or "")
    assert not sentinel.exists(), "备份失败后不得执行动作命令"


def test_action_backup_removes_path_created_by_action(tmp_path, monkeypatch):
    monkeypatch.setattr(actions_module, "log_root", lambda: tmp_path / "Log")
    created = tmp_path / "new-file.txt"
    outcome = execute_action(
        {
            "title": "创建文件",
            "command": f"Set-Content -LiteralPath '{created}' -Value 'new'",
            "shell": "powershell",
            "risk": "low",
            "backup_paths": [str(created)],
        }
    )
    assert outcome.ok, outcome.error
    assert created.exists()
    result = actions_module.restore_action_backup(outcome.backup_dir)
    assert result["ok"], result["errors"]
    assert not created.exists(), "动作前不存在的路径，还原时应删除"


def test_action_validation_accepts_backup_fields():
    decision = validate_decision(
        {
            "narration": "依据 [E1] 提出可回滚修复。",
            "action": "propose_action",
            "basis": ["E1"],
            "actions": [
                {
                    "title": "替换文件",
                    "command": "Copy-Item a b -Force",
                    "shell": "powershell",
                    "risk": "medium",
                    "requires_admin": False,
                    "backup_paths": ["C:\\temp\\b.dll"],
                    "rollback_command": "Copy-Item backup b -Force",
                    "rollback_shell": "powershell",
                }
            ],
        },
        {"E1"},
    )
    action = decision.actions[0]
    assert action["backup_paths"] == ["C:\\temp\\b.dll"]
    assert action["rollback_command"] == "Copy-Item backup b -Force"
    assert action["rollback_shell"] == "powershell"


def test_sandbox_confirmation_is_enforced_in_trusted_mode():
    from helper_plus.tools import create_registry

    spec = create_registry().get("sandbox_control")
    assert spec.requires_confirmation is True
    assert spec.requires_confirmation_in_trusted is True


def test_pnputil_parser_handles_localized_output():
    from helper_plus.tools.system_checks import _parse_pnputil_blocks

    blocks = _parse_pnputil_blocks(
        "发布名称:     oem70.inf\n"
        "原始名称:      hpP1007.inf\n"
        "提供程序名称:      Hewlett-Packard\n"
        "类名称:         Printer\n"
        "类 GUID:         {4d36e979-e325-11ce-bfc1-08002be10318}\n"
        "驱动程序版本:     01/01/2013 1.0.2.2680\n"
        "签名者名称:        Microsoft Windows Hardware Compatibility Publisher\n"
    )
    assert len(blocks) == 1
    assert blocks[0]["发布名称"] == "oem70.inf"
    assert blocks[0]["原始名称"] == "hpP1007.inf"


def test_directory_baseline_plan_has_two_inserted_tasks(tmp_path):
    from helper_plus.core.baseline import build_baseline_plan

    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    tasks = build_baseline_plan(tmp_path, AppConfig())
    names = [task.tool for task in tasks]
    assert "inspect_directory" in names
    assert "inspect_project_manifests" in names
    assert names.index("inspect_directory") < names.index("inspect_project_manifests")


def test_inspect_project_manifests_real(tmp_path):
    from helper_plus.tools import create_registry
    from helper_plus.tools.base import ToolContext

    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "dingtalk-like-app",
                "description": "DingTalk style desktop helper",
                "version": "1.2.3",
                "dependencies": {"electron": "^30.0.0"},
                "scripts": {"start": "electron ."},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("requests==2.32.0\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# DingTalk-like App\n\nA test project.\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "import requests\nimport definitely_missing_helper_xyz\n", encoding="utf-8"
    )
    result = create_registry().invoke(
        "inspect_project_manifests",
        {"max_files": 10},
        ToolContext(target=tmp_path, config=AppConfig(), cancel_event=threading.Event()),
    )
    assert result.ok, result.error
    data = result.data
    assert "node" in data["project_types"]
    assert "python" in data["project_types"]
    assert "electron" in data["declared_dependencies"]
    assert data["manifests"]
    assert data["readme_excerpt"].startswith("# DingTalk-like App")
    assert "definitely_missing_helper_xyz" in data["missing_python_modules"]


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows 工具")
def test_inspect_directory_real(tmp_path):
    from helper_plus.tools import create_registry
    from helper_plus.tools.base import ToolContext

    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    registry = create_registry()
    result = registry.invoke(
        "inspect_directory",
        {"max_entries": 20},
        ToolContext(target=tmp_path, config=AppConfig(), cancel_event=threading.Event()),
    )
    assert result.ok, result.error
    assert result.data["file_count"] == 1
    assert result.data["directory_count"] == 1
