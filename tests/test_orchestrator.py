"""编排器集成测试：用假证据 + 假 Planner 验证引用校验、工具循环与降级。"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any

import helper_plus.core.orchestrator as orchestrator_module
from helper_plus.config import AppConfig
from helper_plus.core.orchestrator import (
    DiagnosisSink,
    Orchestrator,
    OrchestratorOptions,
)
from helper_plus.llm.client import LLMStreamResult
from helper_plus.models import ToolResult, ToolStep
from helper_plus.targets import DiagnosisTarget
from helper_plus.tools import create_registry


def _fake_baseline(registry, store, ctx, *, start_sequence=1, on_step=None, on_phase=None):
    result = ToolResult(
        ok=True,
        summary="Windows 11 Build 22631，测试基线",
        data={"os_caption": "Microsoft Windows 11 Pro", "build_number": "22631"},
        source="test",
    )
    evidence = store.add("get_system_info", result, target=None)
    step = ToolStep(
        sequence=start_sequence,
        tool="get_system_info",
        arguments={},
        basis=["Q1"],
        narration="读取测试基线",
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


def _valid_plan() -> str:
    return json.dumps(
        {
            "narration": "先基于 [E1] 给出执行计划。",
            "action": "plan",
            "basis": ["E1"],
            "plan": {
                "goal": "收集证据并给出测试结论。",
                "confirmed_user_goal": "主模型确认：收集证据并给出测试结论。",
                "steps": ["读取 WER 证据", "生成候选原因"],
                "tools": ["get_wer_reports", "ask_user"],
                "notes": "这是测试计划。",
            },
        },
        ensure_ascii=False,
    )


def _valid_final() -> str:
    return json.dumps(
        {
            "narration": "已有基线证据 [E1]，可以给出保守结论。",
            "action": "final",
            "basis": ["E1"],
            "final": {
                "headline": "测试结论：证据有限。",
                "summary": "系统信息已读取 [E1]，尚不足以定位根因。",
                "findings": [
                    {"text": "系统为 Windows 11 Build 22631。", "evidence": ["E1"], "confidence": 0.95}
                ],
                "candidate_causes": [
                    {
                        "cause": "证据不足，暂不能定位。",
                        "confidence": 0.1,
                        "evidence": ["E1"],
                        "supporting": "只有系统基线证据。",
                        "against": "尚未读取更多日志。",
                    }
                ],
                "next_steps": [
                    {
                        "title": "继续只读检查事件日志。",
                        "why": "补充时间线。",
                        "risk": "read-only",
                        "executed": False,
                        "evidence": ["E1"],
                    }
                ],
                "limitations": ["测试模式。"],
            },
        },
        ensure_ascii=False,
    )


class _CaptureSink(DiagnosisSink):
    def __init__(self) -> None:
        self.phases: list[str] = []
        self.warnings: list[str] = []
        self.finals: list[dict[str, Any]] = []
        self.evidence: list[dict[str, Any]] = []
        self.steps: list[dict[str, Any]] = []
        self.attempts: list[int] = []

    def on_phase(self, text: str) -> None:
        self.phases.append(text)

    def on_warning(self, text: str) -> None:
        self.warnings.append(text)

    def on_final(self, report: dict[str, Any]) -> None:
        self.finals.append(report)

    def on_evidence(self, evidence: dict[str, Any]) -> None:
        self.evidence.append(evidence)

    def on_tool_step(self, step: dict[str, Any]) -> None:
        self.steps.append(step)

    def on_attempt_start(self, attempt: int) -> None:
        self.attempts.append(attempt)


class _FakePlanner:
    instances: list["_FakePlanner"] = []
    responses: list[str] = []
    close_calls: int = 0

    def __init__(self, config, api_key, cancel_event):
        self.requests: list[list[dict[str, Any]]] = []
        _FakePlanner.instances.append(self)

    def stream(self, messages, *, on_reasoning=None, on_content=None, on_attempt=None):
        self.requests.append(messages)
        text = _FakePlanner.responses.pop(0)
        if on_reasoning:
            on_reasoning("测试思考。")
        if on_content:
            for index in range(0, len(text), 5):
                on_content(text[index : index + 5])
        return LLMStreamResult(
            content=text,
            reasoning="测试思考。",
            finish_reason="stop",
            usage={"total_tokens": 12},
        )


class _InteractiveSink(_CaptureSink):
    def __init__(self) -> None:
        super().__init__()
        self.questions: list[dict[str, Any]] = []
        self.answers: list[str] = []
        self.proposals: list[dict[str, Any]] = []
        self.plan: dict[str, Any] | None = None

    def on_plan(self, plan: dict[str, Any]) -> None:
        self.plan = plan

    def ask_user(self, question: dict[str, Any]) -> str:
        self.questions.append(question)
        return "用户补充：故障出现在最近一次更新之后。"

    def confirm_actions(self, proposal: dict[str, Any]) -> bool:
        self.proposals.append(proposal)
        return True


def _make_orchestrator(
    monkeypatch,
    responses: list[str],
    *,
    api_key="sk-test",
    target=None,
    broker_client=None,
    sink=None,
    local_context=None,
) -> tuple[Orchestrator, Any]:
    _FakePlanner.instances = []
    _FakePlanner.responses = responses
    monkeypatch.setattr(orchestrator_module, "collect_baseline", _fake_baseline)
    monkeypatch.setattr(orchestrator_module, "DeepSeekClient", _FakePlanner)
    config = AppConfig(model="deepseek-flash", reasoning_effort="max")
    sink = sink or _CaptureSink()
    orchestrator = Orchestrator(
        config,
        target=target,
        issue="测试问题",
        sink=sink,
        cancel_event=threading.Event(),
        registry=create_registry(),
        options=OrchestratorOptions(
            api_key=api_key,
            broker_client=broker_client,
            local_context=local_context,
        ),
    )
    return orchestrator, sink


class _FakeLocalAgent:
    class _Archive:
        def related_cases(self, **kwargs):
            return []

    def __init__(self) -> None:
        self.archive = self._Archive()
        self.calls: list[dict[str, Any]] = []
        self.finalized = False
        self.last_error = None

    def analyze(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "user_goal_candidate": "测试目标",
            "goal_clarity": "clear",
            "completed": ["基线已取证"],
            "unfinished": ["继续验证"],
            "context_summary": "本地 Qwen 测试上下文",
            "recent_turn_decisions": {"keep_turn_ids": [], "drop_turn_ids": [], "reason": "测试"},
        }

    def finalize(self, **kwargs):
        self.finalized = True
        from pathlib import Path
        import tempfile

        path = Path(tempfile.gettempdir()) / "helper-plus-test-case.md"
        path.write_text("# 测试病历\n", encoding="utf-8")
        return path


class _FakeBroker:
    def probe(self):
        return SimpleNamespace(available=True, detail="SYSTEM 代理在线", identity=r"NT AUTHORITY\SYSTEM")

    def collect_privileged_evidence(self, target):
        return {
            "version": 1,
            "nonce": "a" * 32,
            "ok": True,
            "broker": {"identity": r"NT AUTHORITY\SYSTEM", "sid": "S-1-5-18", "pid": 1},
            "results": [
                {
                    "tool": "system_broker_inspect",
                    "ok": True,
                    "summary": "SYSTEM broker inspected target",
                    "source": "Windows Helper Plus SYSTEM Broker",
                    "native_command": "Get-FileHash / Get-AuthenticodeSignature / Get-Acl",
                    "error": None,
                    "data": {
                        "name": "test.dll",
                        "signature_status": "Valid",
                        "sha256": "b" * 64,
                        "owner": r"NT SERVICE\TrustedInstaller",
                    },
                }
            ],
        }


def test_orchestrator_final_and_audit(monkeypatch, tmp_path):
    orchestrator, sink = _make_orchestrator(monkeypatch, [_valid_plan(), _valid_final()])
    record = orchestrator.run()
    assert record.status == "completed"
    assert record.report is not None
    assert record.report["headline"] == "测试结论：证据有限。"
    assert record.plan and record.plan["goal"]
    assert len(record.evidence) == 1
    assert record.tool_steps[0]["origin"] == "baseline"
    assert sink.finals and sink.finals[0]["summary"]
    assert record.usage["total_tokens"] == 24
    assert record.elapsed_seconds >= 0
    assert record.started_at and record.finished_at
    assert record.plan and record.plan["goal"] == "收集证据并给出测试结论。"
    assert orchestrator.last_record_path is not None
    assert orchestrator.last_record_path.exists()
    requests = _FakePlanner.instances[0].requests
    assert len(requests) == 2
    assert "工具白名单" in requests[0][0]["content"]
    assert "只有结论/原因推理必须有依据" in requests[0][0]["content"]
    assert "测试基线" in requests[0][1]["content"]
    assert '"plan"' in requests[1][1]["content"]


def test_orchestrator_collects_printer_target_evidence(monkeypatch):
    target = DiagnosisTarget.printer("Fax")
    orchestrator, sink = _make_orchestrator(
        monkeypatch,
        [_valid_plan(), _valid_final()],
        target=target,
    )
    record = orchestrator.run()
    assert record.status == "completed", record.errors
    assert record.target_kind == "printer"
    assert any(item["tool"] == "get_printer_info" for item in record.evidence)


def test_orchestrator_collects_system_broker_evidence(monkeypatch, tmp_path):
    target = tmp_path / "test.dll"
    target.write_bytes(b"broker evidence target")
    orchestrator, sink = _make_orchestrator(
        monkeypatch,
        [_valid_plan(), _valid_final()],
        target=target,
        broker_client=_FakeBroker(),
    )
    record = orchestrator.run()
    assert record.status == "completed", record.errors
    broker_evidence = [item for item in record.evidence if item["tool"] == "system_broker_inspect"]
    assert len(broker_evidence) == 1
    broker_steps = [item for item in record.tool_steps if item["origin"] == "system_broker"]
    assert len(broker_steps) == 1
    assert broker_steps[0]["evidence_id"] == broker_evidence[0]["id"]
    assert broker_steps[0]["basis"] == ["Q1"]
    assert any("SYSTEM" in phase for phase in sink.phases)
    assert any(item["tool"] == "system_broker_inspect" for item in sink.evidence)


def test_ask_user_and_confirmed_action_closed_loop(monkeypatch):
    ask_user = json.dumps(
        {
            "narration": "证据 [E1] 不足，需要用户补充时间线。",
            "action": "ask_user",
            "basis": ["E1"],
            "question": {
                "prompt": "问题是在什么时候出现的？",
                "options": ["最近更新后", "一直存在", "不确定"],
                "allow_free_text": True,
            },
        },
        ensure_ascii=False,
    )
    propose = json.dumps(
        {
            "narration": "根据 [E1] 与用户回答，提出一个受控验证动作。",
            "action": "propose_action",
            "basis": ["E1"],
            "actions": [
                {
                    "title": "测试动作：回显标记",
                    "command": "echo helper-plus-phase3-closed-loop",
                    "shell": "cmd",
                    "risk": "low",
                    "requires_admin": False,
                    "rationale": "仅用于验证用户确认与动作/证据闭环。",
                    "rollback": "无副作用。",
                }
            ],
        },
        ensure_ascii=False,
    )
    final = _valid_final()
    sink = _InteractiveSink()
    orchestrator, sink = _make_orchestrator(
        monkeypatch,
        [_valid_plan(), ask_user, propose, final],
        sink=sink,
    )
    record = orchestrator.run()
    assert record.status == "completed", record.errors
    assert record.plan and record.plan["goal"]
    assert sink.plan and sink.plan["goal"]
    assert sink.questions
    assert record.interactions
    assert record.interactions[0]["answer"].startswith("用户补充")
    assert sink.proposals
    assert record.actions
    action_evidence = [item for item in record.evidence if item["tool"] == "action_execute"]
    assert len(action_evidence) == 1
    assert "helper-plus-phase3-closed-loop" in action_evidence[0]["data"]["outcome"]["stdout"]
    action_steps = [item for item in record.tool_steps if item["origin"] == "action"]
    assert len(action_steps) == 1
    # 动作结果之后，Planner 继续验证并 final。
    assert record.planner and record.planner[-1]["action"] == "final"


def test_orchestrator_planner_can_call_tool(monkeypatch):
    call_tool = json.dumps(
        {
            "narration": "根据 [E1]，需要补充 WER 证据。",
            "action": "call_tool",
            "basis": ["E1"],
            "tool": "get_wer_reports",
            "arguments": {"limit": 3},
            "why": "检查近期崩溃报告。",
        },
        ensure_ascii=False,
    )
    final = json.dumps(
        {
            "narration": "现在有 WER 证据 [E1][E2]。",
            "action": "final",
            "basis": ["E1", "E2"],
            "final": {
                "headline": "测试结论。",
                "summary": "系统信息 [E1] 与 WER 结果 [E2] 已读取。",
                "findings": [
                    {"text": "WER 检查已执行。", "evidence": ["E2"], "confidence": 0.8}
                ],
                "candidate_causes": [
                    {
                        "cause": "证据不足。",
                        "confidence": 0.1,
                        "evidence": ["E1", "E2"],
                        "supporting": "数据有限。",
                        "against": "尚未深入分析。",
                    }
                ],
                "next_steps": [
                    {
                        "title": "复核事件查看器。",
                        "why": "只读复核。",
                        "risk": "read-only",
                        "executed": False,
                        "evidence": ["E2"],
                    }
                ],
                "limitations": [],
            },
        },
        ensure_ascii=False,
    )
    orchestrator, sink = _make_orchestrator(monkeypatch, [_valid_plan(), call_tool, final])
    record = orchestrator.run()
    assert record.status == "completed", record.errors
    assert len(record.evidence) == 2
    assert len(record.tool_steps) == 2
    assert record.tool_steps[1]["tool"] == "get_wer_reports"
    assert record.tool_steps[1]["basis"] == ["E1"]
    assert len(_FakePlanner.instances[0].requests) == 3
    second_payload = _FakePlanner.instances[0].requests[2][1]["content"]
    assert '"id": "E2"' in second_payload
    # 录制的 Planner 往返与证据引用都能用于审计。
    assert record.planner[-1]["action"] == "final"
    assert record.planner[-1]["raw_output"]


def test_local_context_is_injected_and_archived(monkeypatch):
    local = _FakeLocalAgent()
    orchestrator, sink = _make_orchestrator(
        monkeypatch,
        [_valid_plan(), _valid_final()],
        local_context=local,
    )
    record = orchestrator.run()
    assert record.status == "completed", record.errors
    assert local.calls
    assert local.finalized
    assert record.local_context and record.local_context["context_summary"] == "本地 Qwen 测试上下文"
    assert record.confirmed_user_goal.startswith("主模型确认")
    assert record.case_path
    assert '"local_context"' in _FakePlanner.instances[0].requests[1][1]["content"]


def test_first_decision_must_be_plan(monkeypatch):
    orchestrator, sink = _make_orchestrator(
        monkeypatch, [_valid_final(), _valid_plan(), _valid_final()]
    )
    record = orchestrator.run()
    assert record.status == "completed", record.errors
    assert len(_FakePlanner.instances[0].requests) == 3
    assert "第一轮必须先用 action=plan" in (
        record.planner[0].get("error") or ""
    )
    assert record.plan is not None


def test_invalid_planner_json_is_retried(monkeypatch):
    orchestrator, sink = _make_orchestrator(
        monkeypatch, ["这不是 JSON", _valid_plan(), _valid_final()]
    )
    record = orchestrator.run()
    assert record.status == "completed", record.errors
    assert len(_FakePlanner.instances[0].requests) == 3
    assert sink.attempts == [1, 2, 1]


def test_missing_api_key_is_error_without_local_analysis(monkeypatch):
    orchestrator, sink = _make_orchestrator(monkeypatch, [], api_key="")
    record = orchestrator.run()
    assert record.status == "error"
    assert record.report is None
    assert sink.warnings
    assert "API Key" in sink.warnings[0]
    assert _FakePlanner.instances == []
