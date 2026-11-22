"""Qt 工作线程：把 Orchestrator 的同步事件转换为 QObject 信号。"""

from __future__ import annotations

from pathlib import Path
import threading
import uuid
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from ..config import AppConfig
from ..core.orchestrator import DiagnosisSink, Orchestrator, OrchestratorOptions
from ..local_context import LocalContextAgent, log_root
from ..local_model import LocalModelClient
from ..system_broker_client import SystemBrokerClient
from ..targets import DiagnosisTarget, coerce_target


class DiagnosisWorker(QObject):
    phase = Signal(str)
    reasoning = Signal(str)
    content = Signal(str)
    narration = Signal(str)
    plan_ready = Signal(dict)
    attempt_started = Signal(int)
    evidence_added = Signal(dict)
    step_added = Signal(dict)
    warning = Signal(str)
    usage = Signal(dict)
    final_report = Signal(dict)
    completed = Signal(dict)
    failed = Signal(str)

    # 交互信号：payload 内含 request_id，UI 处理后调用 answer_* / resolve_*。
    question_asked = Signal(dict)
    tool_confirmation_requested = Signal(dict)
    actions_proposed = Signal(dict)

    def __init__(
        self,
        *,
        config: AppConfig,
        target: Any,
        issue: str,
        api_key: str,
    ) -> None:
        super().__init__()
        self._config = config
        self._target = target
        self._issue = issue
        self._api_key = api_key
        self.cancel_event = threading.Event()
        self._reply_lock = threading.Lock()
        self._reply_events: dict[str, threading.Event] = {}
        self._replies: dict[str, Any] = {}

    # ------------------------------------------------------------- interactions
    def _request(self, kind: str, payload: dict[str, Any], default: Any) -> Any:
        request_id = uuid.uuid4().hex
        event = threading.Event()
        with self._reply_lock:
            self._reply_events[request_id] = event
        envelope = {"request_id": request_id, **payload}
        try:
            if kind == "question":
                self.question_asked.emit(envelope)
            elif kind == "tool":
                self.tool_confirmation_requested.emit(envelope)
            else:
                self.actions_proposed.emit(envelope)
            while not event.wait(0.2):
                if self.cancel_event.is_set():
                    return default
            with self._reply_lock:
                return self._replies.pop(request_id, default)
        finally:
            with self._reply_lock:
                self._reply_events.pop(request_id, None)

    def _reply(self, request_id: str, value: Any) -> None:
        with self._reply_lock:
            self._replies[request_id] = value
            event = self._reply_events.get(request_id)
        if event:
            event.set()

    @Slot(str, str)
    def answer_question(self, request_id: str, answer: str) -> None:
        self._reply(request_id, answer)

    @Slot(str, bool)
    def resolve_tool_confirmation(self, request_id: str, approved: bool) -> None:
        self._reply(request_id, bool(approved))

    @Slot(str, bool)
    def resolve_actions(self, request_id: str, approved: bool) -> None:
        self._reply(request_id, bool(approved))

    # -------------------------------------------------------------------- run
    @Slot()
    def run(self) -> None:
        sink = _QtSink(self)
        try:
            broker_client = SystemBrokerClient(cancel_event=sink.cancel_event)
            local_agent = None
            if getattr(self._config, "local_model_enabled", True):
                try:
                    local_agent = LocalContextAgent(
                        LocalModelClient(self._config, log_dir=log_root() / "local-model"),
                        root=log_root(),
                    )
                except Exception as exc:  # noqa: BLE001
                    self.warning.emit(f"本地 Qwen 上下文官初始化失败：{type(exc).__name__}: {exc}")
            orchestrator = Orchestrator(
                self._config,
                target=self._target,
                issue=self._issue,
                sink=sink,
                cancel_event=sink.cancel_event,
                options=OrchestratorOptions(
                    api_key=self._api_key,
                    broker_client=broker_client,
                    context_budget_chars=getattr(self._config, "context_budget_chars", 700_000),
                    action_timeout_seconds=getattr(self._config, "action_timeout_seconds", 3600.0),
                    local_context=local_agent,
                    local_async=True,
                    permission_mode=getattr(self._config, "permission_mode", "trusted"),
                ),
            )
            record = orchestrator.run()
            self.completed.emit(record.to_dict())
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class _QtSink(DiagnosisSink):
    def __init__(self, worker: DiagnosisWorker) -> None:
        self.worker = worker
        self.cancel_event = worker.cancel_event

    def on_phase(self, text: str) -> None:
        self.worker.phase.emit(text)

    def on_reasoning(self, text: str) -> None:
        self.worker.reasoning.emit(text)

    def on_content(self, text: str) -> None:
        self.worker.content.emit(text)

    def on_narration(self, text: str) -> None:
        self.worker.narration.emit(text)

    def on_plan(self, plan: dict[str, Any]) -> None:
        self.worker.plan_ready.emit(plan)

    def on_attempt_start(self, attempt: int) -> None:
        self.worker.attempt_started.emit(attempt)

    def on_evidence(self, evidence: dict[str, Any]) -> None:
        self.worker.evidence_added.emit(evidence)

    def on_tool_step(self, step: dict[str, Any]) -> None:
        self.worker.step_added.emit(step)

    def on_warning(self, text: str) -> None:
        self.worker.warning.emit(text)

    def on_usage(self, usage: dict[str, Any]) -> None:
        self.worker.usage.emit(usage)

    def on_final(self, report: dict[str, Any]) -> None:
        self.worker.final_report.emit(report)

    def ask_user(self, question: dict[str, Any]) -> str:
        return str(
            self.worker._request("question", {"question": question}, "（用户未回答）") or "（用户未回答）"
        )

    def confirm_tool(self, tool: str, arguments: dict[str, Any], reason: str) -> bool:
        return bool(
            self.worker._request(
                "tool",
                {"tool": tool, "arguments": arguments, "reason": reason},
                False,
            )
        )

    def confirm_actions(self, proposal: dict[str, Any]) -> bool:
        return bool(self.worker._request("actions", {"proposal": proposal}, False))
