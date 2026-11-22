"""Agent–Planner–Tools 编排器 v2（AI-only 分析 + 交互 + 受控修复）。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import threading
import time
import traceback
from typing import Any

from ..audit import AuditLog
from ..config import AppConfig, reports_dir
from ..llm.client import DeepSeekClient, LLMConfigurationError, LLMError
from ..llm.narration import NarrationExtractor
from ..llm.prompts import build_system_prompt, build_turn_payload, payload_to_message
from ..models import DiagnosticRecord, Evidence, EvidenceStore, ToolResult, ToolStep
from ..safety import SafetyError, validate_inspect_path
from ..targets import DiagnosisTarget, coerce_target
from ..tools import ToolRegistry, create_registry
from ..tools.base import ToolContext
from .actions import execute_action
from .baseline import collect_baseline
from .context import build_diagnosis_journal, build_evidence_context
from .validation import (
    Decision,
    PlannerProtocolError,
    extract_json_object,
    validate_decision,
)


class _Cancelled(Exception):
    pass


class DiagnosisSink:
    """编排器 → UI 事件接口。交互方法必须阻塞直到用户作答/确认。"""

    def on_phase(self, text: str) -> None:
        pass

    def on_reasoning(self, text: str) -> None:
        pass

    def on_content(self, text: str) -> None:
        pass

    def on_narration(self, text: str) -> None:
        pass

    def on_plan(self, plan: dict[str, Any]) -> None:
        pass

    def on_attempt_start(self, attempt: int) -> None:
        pass

    def on_evidence(self, evidence: dict[str, Any]) -> None:
        pass

    def on_tool_step(self, step: dict[str, Any]) -> None:
        pass

    def on_warning(self, text: str) -> None:
        pass

    def on_usage(self, usage: dict[str, Any]) -> None:
        pass

    def on_final(self, report: dict[str, Any]) -> None:
        pass

    def ask_user(self, question: dict[str, Any]) -> str:
        return "（无交互界面，用户未回答）"

    def confirm_tool(self, tool: str, arguments: dict[str, Any], reason: str) -> bool:
        return False

    def confirm_actions(self, proposal: dict[str, Any]) -> bool:
        return False


@dataclass
class OrchestratorOptions:
    api_key: str = ""
    broker_client: Any = None
    context_budget_chars: int = 700_000
    action_timeout_seconds: float = 3600.0
    # 本地 Qwen 上下文官；为 None 时（测试/无本地模型）不影响主流程。
    local_context: Any = None
    # True：本地上下文/日志病历在后台线程执行，不阻塞主 Planner；GUI 使用。
    # 默认 False 便于测试同步断言。
    local_async: bool = False
    # trusted：调用默认放行，低风险动作自动执行；中/高风险仍由用户确认/UAC。
    # 默认 strict 以保护测试/嵌入调用；GUI 会传入 config.permission_mode（默认 trusted）。
    permission_mode: str = "strict"


class Orchestrator:
    def __init__(
        self,
        config: AppConfig,
        *,
        target: Any = None,
        issue: str = "",
        sink: DiagnosisSink | None = None,
        cancel_event: threading.Event | None = None,
        registry: ToolRegistry | None = None,
        options: OrchestratorOptions | None = None,
    ) -> None:
        self.config = config
        self.target_spec: DiagnosisTarget = coerce_target(target)
        self.target = self.target_spec.path
        self.issue = (issue or "").strip()[:2000]
        self.sink = sink or DiagnosisSink()
        self.cancel_event = cancel_event or threading.Event()
        self.registry = registry or create_registry()
        self.options = options or OrchestratorOptions()
        self.record = DiagnosticRecord(
            target=self.target_spec.display,
            target_kind=self.target_spec.kind,
            issue=self.issue,
            mode="ai",
            model=config.model,
            reasoning_effort=config.reasoning_effort,
        )
        self.store = EvidenceStore()
        self.steps: list[ToolStep] = []
        self.interactions: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []
        self.audit = AuditLog(self.record.record_id)
        self._seen_calls: set[str] = set()
        self._ctx = ToolContext(
            target=self.target if self.target_spec.is_filesystem else None,
            config=config,
            cancel_event=self.cancel_event,
            api_key=self.options.api_key,
            target_spec=self.target_spec,
        )
        self.last_record_path = None
        self._started_monotonic = time.monotonic()
        self.plan: dict[str, Any] | None = None
        self.confirmed_user_goal = ""
        self.local_context: dict[str, Any] | None = None
        self.local_context_error: str | None = None
        self.prior_cases: list[dict[str, Any]] = []
        self.turns: list[dict[str, Any]] = []
        self._turn_counter = 0
        self._last_local_round = -100
        self._last_local_interactions = -1
        self._last_local_actions = -1
        self._local_refresh_count = 0
        self._local_thread: threading.Thread | None = None
        self._local_busy = False
        self._finalize_thread: threading.Thread | None = None
        self._state_lock = threading.RLock()
        self._append_turn(
            role="user",
            kind="goal",
            content=f"目标：{self.target_spec.display}；问题：{self.issue or '（未填写）'}",
        )

    # ------------------------------------------------------------------ public
    def run(self) -> DiagnosticRecord:
        self.audit.write(
            "session_start",
            target=self.record.target,
            target_kind=self.record.target_kind,
            model=self.record.model,
            reasoning_effort=self.record.reasoning_effort,
            permission_mode=self.options.permission_mode,
            local_context_enabled=self.options.local_context is not None,
        )
        try:
            self._collect_baseline()
            self._check_cancel()
            self._run_planner()
        except _Cancelled:
            self.record.finish("cancelled")
            self.audit.write("session_cancelled")
            self.sink.on_phase("分析已取消；已完成部分的证据与审计记录已保留。")
        except Exception as exc:  # noqa: BLE001
            message = self._friendly_error(exc)
            self.record.errors.append(message)
            trace_text = traceback.format_exc()
            self.audit.write("session_error", error=message, traceback=trace_text[-12000:])
            self.sink.on_warning(message)
            self.record.finish("error")
        finally:
            self.record.elapsed_seconds = round(time.monotonic() - self._started_monotonic, 2)
            self._sync_record()
            self._save_record()
            self._finalize_local_history()
            self.audit.write(
                "session_end",
                status=self.record.status,
                evidence_count=len(self.store),
                error_count=len(self.record.errors),
                elapsed_seconds=self.record.elapsed_seconds,
            )
        return self.record

    # --------------------------------------------------------------- baseline
    def _collect_baseline(self) -> None:
        def _on_step(step: ToolStep, evidence: Evidence) -> None:
            self._register_step(step, evidence)
            self.audit.write(
                "tool_call",
                origin="baseline",
                tool=step.tool,
                arguments=step.arguments,
                basis=step.basis,
                evidence_id=evidence.id,
                ok=evidence.ok,
                duration_ms=step.duration_ms,
            )

        self.sink.on_phase("开始全面基线取证：系统、事件日志、可靠性、WER、更新与目标相关路径…")
        collect_baseline(
            self.registry,
            self.store,
            self._ctx,
            start_sequence=1,
            on_step=_on_step,
            on_phase=self.sink.on_phase,
        )
        self._sync_record()
        self._collect_special_target_evidence()
        self._collect_broker_evidence()

    def _register_step(self, step: ToolStep, evidence: Evidence) -> None:
        self.steps.append(step)
        self._seen_calls.add(self._call_key(step.tool, step.arguments))
        self.record.tool_steps.append(step.to_dict())
        self._sync_record()
        self.sink.on_evidence(evidence.to_dict())
        self.sink.on_tool_step(step.to_dict())

    def _collect_special_target_evidence(self) -> None:
        if self.target_spec.kind == "device":
            query = self.target_spec.device_id or self.target_spec.display
            self._run_baseline_tool(
                "get_device_info",
                {"query": query, "limit": 20},
                "查询所选设备的 PnP 状态、错误码、驱动与 INF",
            )
        elif self.target_spec.kind == "printer":
            query = self.target_spec.printer_name or self.target_spec.display
            self._run_baseline_tool(
                "get_printer_info",
                {"query": query, "limit": 20},
                "查询所选打印机的驱动、端口、队列与离线状态",
            )

    def _run_baseline_tool(self, tool: str, arguments: dict[str, Any], narration: str) -> None:
        self._check_cancel()
        try:
            normalized = self.registry.validate_call(tool, arguments)
            result = self.registry.invoke(tool, normalized, self._ctx)
        except Exception as exc:  # noqa: BLE001
            result = ToolResult.failure("目标对象取证失败", f"{type(exc).__name__}: {exc}")
        evidence = self.store.add(tool, result, target=self.record.target)
        step = ToolStep(
            sequence=len(self.steps) + 1,
            tool=tool,
            arguments=arguments,
            basis=["Q1"],
            narration=narration,
            why="系统根据诊断对象类型自动执行的相关性取证",
            evidence_id=evidence.id,
            ok=result.ok,
            origin="target_profile",
            duration_ms=result.duration_ms,
            error=result.error,
        )
        self._register_step(step, evidence)
        self.audit.write(
            "tool_call",
            origin="target_profile",
            tool=tool,
            arguments=arguments,
            basis=["Q1"],
            evidence_id=evidence.id,
            ok=evidence.ok,
        )

    def _collect_broker_evidence(self) -> None:
        client = self.options.broker_client
        if client is None or not self.target_spec.is_file or not self.target:
            return
        try:
            info = client.probe()
        except Exception as exc:  # noqa: BLE001
            self.audit.write("broker_probe_error", error=str(exc))
            return
        if not getattr(info, "available", False):
            self.audit.write("broker_unavailable", detail=getattr(info, "detail", ""))
            return
        self.sink.on_phase("SYSTEM 特权只读代理在线，正在对目标文件进行独立校验…")
        try:
            response = client.collect_privileged_evidence(str(self.target))
        except Exception as exc:  # noqa: BLE001
            # SYSTEM 代理是可选增强；请求目录 ACL/服务偶发不可写时静默降级，
            # 不把用户吓成“分析失败”，也保留审计。
            self.audit.write("broker_error", error=str(exc))
            self.sink.on_phase("SYSTEM 特权代理本次不可用，已跳过；普通权限证据继续。")
            return
        broker = response.get("broker") or {}
        broker_identity = str(broker.get("identity") or "未知")
        for item in response.get("results") or []:
            if not isinstance(item, dict):
                continue
            data = item.get("data") if isinstance(item.get("data"), dict) else {}
            result = ToolResult(
                ok=bool(item.get("ok")),
                summary=self._broker_summary(item),
                data=data,
                source=str(item.get("source") or "Windows Helper Plus SYSTEM Broker"),
                native_command=str(item.get("native_command") or ""),
                error=item.get("error"),
            )
            evidence = self.store.add("system_broker_inspect", result, target=self.record.target)
            step = ToolStep(
                sequence=len(self.steps) + 1,
                tool="system_broker_inspect",
                arguments={"target": str(self.target)},
                basis=["Q1"],
                narration="SYSTEM 特权代理对目标文件进行独立只读校验",
                why=(
                    f"以 {broker_identity} 身份读取用户权限下可能受限的元数据、SHA-256、"
                    "签名和 ACL；只读，不修改系统。"
                ),
                evidence_id=evidence.id,
                ok=result.ok,
                origin="system_broker",
                error=result.error,
            )
            self._register_step(step, evidence)
            self.audit.write(
                "tool_call",
                origin="system_broker",
                tool=step.tool,
                arguments=step.arguments,
                basis=step.basis,
                evidence_id=evidence.id,
                ok=evidence.ok,
                broker_identity=broker_identity,
            )
        self.sink.on_phase(f"SYSTEM 特权只读代理已完成 {len(response.get('results') or [])} 项证据采集。")

    def _broker_summary(self, item: dict[str, Any]) -> str:
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        name = data.get("name") or data.get("path") or "目标文件"
        signature = data.get("signature_status") or "未知"
        sha256 = str(data.get("sha256") or "")
        owner = data.get("owner") or "未知"
        hash_text = f"{sha256[:16]}…" if sha256 else "未计算"
        return f"SYSTEM 特权校验 {name}：签名 {signature}；SHA-256 {hash_text}；所有者 {owner}"

    # ----------------------------------------------------------------- planner
    def _run_planner(self) -> None:
        if not self.options.api_key.strip():
            raise LLMConfigurationError(
                "尚未配置 DeepSeek API Key。本版本所有分析都必须经过 AI，请先在“设置”中保存 API Key。"
            )
        client = DeepSeekClient(self.config, self.options.api_key, self.cancel_event)
        system_prompt = build_system_prompt(self.registry.catalog())
        self.audit.write(
            "planner_start",
            model=self.config.model,
            reasoning_effort=self.config.reasoning_effort,
            tool_count=len(self.registry.names),
            system_prompt_chars=len(system_prompt),
        )
        round_number = 1
        while True:
            self._check_cancel()
            self._refresh_local_context(round_number)
            agent = self.options.local_context
            if agent is not None and hasattr(agent, "archive"):
                try:
                    self.prior_cases = agent.archive.related_cases(
                        record_id=self.record.record_id,
                        target=self.record.target,
                        issue=self.issue,
                    )
                except Exception:  # noqa: BLE001
                    pass
            correction: str | None = None
            decision: Decision | None = None
            for attempt in range(1, 3):
                self._check_cancel()
                recent_refs = {step.evidence_id for step in self.steps[-6:]}
                payload = build_turn_payload(
                    config=self.config,
                    target=self.target_spec,
                    issue=self.issue,
                    store=self.store,
                    steps=self.steps,
                    context_budget_chars=self.options.context_budget_chars,
                    validation_feedback=correction,
                    round_number=round_number,
                    interactions=self.interactions,
                    action_history=self.actions,
                    recent_refs=recent_refs,
                    session_elapsed_seconds=time.monotonic() - self._started_monotonic,
                    plan=self.plan,
                    local_context=self.local_context,
                    prior_cases=self.prior_cases,
                    recent_turns=self._filtered_turns(),
                    confirmed_user_goal=self.confirmed_user_goal,
                )
                user_message = payload_to_message(payload)
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ]
                self.sink.on_phase(
                    f"Planner 第 {round_number} 步：结合 {len(self.store)} 条证据规划"
                    + ("（修正无效输出）" if attempt > 1 else "…")
                )
                self.sink.on_attempt_start(attempt)
                self._audit_planner_request(round_number, attempt, user_message)
                extractor = NarrationExtractor()

                def _on_content(chunk: str) -> None:
                    self.sink.on_content(chunk)
                    narration = extractor.feed(chunk)
                    if narration:
                        self.sink.on_narration(narration)

                started = time.monotonic()
                try:
                    stream_result = client.stream(
                        messages,
                        on_reasoning=self.sink.on_reasoning,
                        on_content=_on_content,
                    )
                except LLMError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise LLMError(f"模型调用异常：{type(exc).__name__}: {exc}") from exc
                raw = stream_result.content
                duration_ms = int((time.monotonic() - started) * 1000)
                self._accumulate_usage(stream_result.usage)
                self.audit.write(
                    "planner_response",
                    round=round_number,
                    attempt=attempt,
                    duration_ms=duration_ms,
                    content_chars=len(raw),
                    reasoning_chars=len(stream_result.reasoning),
                    finish_reason=stream_result.finish_reason,
                    sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
                )
                try:
                    obj = extract_json_object(raw)
                    decision = validate_decision(
                        obj,
                        self.store.ids,
                        require_evidence_reference=bool(self.store.ids),
                        require_final=False,
                    )
                    if self.plan is None and decision.action != "plan":
                        raise PlannerProtocolError(
                            "第一轮必须先用 action=plan 向用户展示执行计划；计划通过后才可执行工具或修复。"
                        )
                    if decision.action == "plan":
                        for tool_name in (decision.plan or {}).get("tools") or []:
                            name = str(tool_name)
                            # 计划里可以列 ask_user/propose_action 等协议动作；只校验真正的工具名。
                            if name in {"ask_user", "propose_action", "call_tool", "call_tools", "plan", "final"}:
                                continue
                            self.registry.get(name)
                    if decision.action in {"call_tool", "call_tools"}:
                        self._validate_decision_tools(decision)
                except (PlannerProtocolError, SafetyError) as exc:
                    feedback = (
                        exc.feedback() if isinstance(exc, PlannerProtocolError) else f"工具参数未通过安全校验：{exc}"
                    )
                    self._record_planner_attempt(
                        round_number, attempt, "invalid", raw, error=feedback, duration_ms=duration_ms
                    )
                    if attempt < 2:
                        correction = feedback
                        continue
                    raise PlannerProtocolError([feedback]) from exc
                self._record_planner_attempt(
                    round_number, attempt, decision.action, raw, duration_ms=duration_ms
                )
                break
            if decision is None:
                raise PlannerProtocolError("模型连续两次没有返回可用决策。")

            if decision.action == "plan":
                assert decision.plan is not None
                self.plan = decision.plan
                self.confirmed_user_goal = str(decision.plan.get("confirmed_user_goal") or "").strip()
                self.record.plan = decision.plan
                self.record.confirmed_user_goal = self.confirmed_user_goal
                self._append_turn(
                    role="assistant",
                    kind="plan",
                    content=(
                        f"主模型确认目标：{self.confirmed_user_goal}；计划："
                        + "；".join(str(item) for item in (decision.plan.get("steps") or []))
                    ),
                )
                self._sync_record()
                self.audit.write("plan", plan=decision.plan, basis=decision.basis)
                self.sink.on_plan(decision.plan)
                self.sink.on_phase(
                    "主模型已确认用户目标，执行计划已展示；开始按计划执行。"
                )
                round_number += 1
                continue

            if decision.action == "final":
                assert decision.final is not None
                self.record.report = decision.final
                self.record.finish("completed")
                self._sync_record()
                self.audit.write(
                    "report",
                    mode="ai",
                    headline=decision.final.get("headline"),
                    round=round_number,
                    citations=decision.final.get("summary", "")[:200],
                )
                self.sink.on_phase("分析完成：已生成有证据引用的诊断结论。")
                self.sink.on_final(decision.final)
                return

            if decision.action == "ask_user":
                assert decision.question is not None
                answer = self.sink.ask_user(decision.question)
                interaction = {
                    "id": f"A{len(self.interactions) + 1}",
                    "round": round_number,
                    "question": decision.question.get("prompt"),
                    "options": decision.question.get("options") or [],
                    "answer": answer,
                    "timestamp": _now_iso(),
                }
                self.interactions.append(interaction)
                self.record.interactions.append(interaction)
                self._append_turn(
                    role="user",
                    kind="answer",
                    content=f"问题：{interaction['question']}；用户回答：{interaction['answer']}",
                )
                self.audit.write("user_question", **interaction)
                self.sink.on_phase(f"已获得用户回答（A{len(self.interactions)}），继续分析…")
                round_number += 1
                continue

            if decision.action in {"call_tool", "call_tools"}:
                calls = (
                    [{"tool": decision.tool, "arguments": decision.arguments}]
                    if decision.action == "call_tool"
                    else list(decision.tools)
                )
                for call in calls:
                    self._execute_planner_tool(
                        decision,
                        call["tool"],
                        call["arguments"],
                        round_number=round_number,
                        attempt=1,
                    )
                round_number += 1
                continue

            if decision.action == "propose_action":
                self._handle_action_proposal(decision, round_number=round_number)
                round_number += 1
                continue

            raise PlannerProtocolError(f"未知 action：{decision.action}")

    def _validate_decision_tools(self, decision: Decision) -> None:
        calls = (
            [{"tool": decision.tool, "arguments": decision.arguments}]
            if decision.action == "call_tool"
            else list(decision.tools)
        )
        seen: set[str] = set()
        for call in calls:
            name = str(call["tool"])
            normalized = self.registry.validate_call(name, call["arguments"])
            normalized = self._normalize_tool_args(name, normalized)
            # 用户要求：工具可无条件重复调用，不再用“相同工具+参数已执行”拦截验证。
            # 同一批中仍允许重复，但保留一个计数键用于审计去重展示。
            key = self._call_key(name, normalized)
            seen.add(key)
            call["arguments"] = normalized

    def _normalize_tool_args(self, tool: str, normalized: dict[str, Any]) -> dict[str, Any]:
        if tool == "inspect_file" and not normalized.get("path") and self.target_spec.is_file and self.target:
            normalized["path"] = str(self.target)
        if tool in {"inspect_file", "verify_signature_signtool", "map_related_files"}:
            if normalized.get("path") or tool != "map_related_files":
                if normalized.get("path") or self.target_spec.is_file:
                    path = validate_inspect_path(normalized.get("path"), self.target if self.target_spec.is_file else None)
                    normalized["path"] = str(path)
        if tool == "inspect_directory":
            if normalized.get("path") is None and self.target_spec.is_directory and self.target:
                normalized["path"] = str(self.target)
        return normalized

    def _execute_planner_tool(
        self,
        decision: Decision,
        tool: str,
        arguments: dict[str, Any],
        *,
        round_number: int,
        attempt: int,
    ) -> ToolResult:
        self._check_cancel()
        spec = self.registry.get(tool)
        # 当前绝大多数工具只读；沙箱 run 等会启动进程/写副本的工具需要单独标记，
        # 即使信任模式也必须由用户确认。
        needs_confirmation = spec.requires_confirmation and (
            self.options.permission_mode != "trusted"
            or spec.requires_confirmation_in_trusted
        )
        if needs_confirmation:
            approved = self.sink.confirm_tool(
                tool,
                arguments,
                spec.confirmation_reason or "该工具需要用户确认后才能执行。",
            )
            if not approved:
                self.audit.write("tool_rejected", tool=tool, arguments=arguments)
                self.sink.on_warning(f"用户未确认执行 {tool}。")
                result = ToolResult.failure(
                    "用户拒绝执行该工具",
                    "用户未确认该工具；请不要假设已执行。",
                    source=spec.source,
                )
                evidence = self.store.add("tool_confirmation", result, target=self.record.target)
                step = ToolStep(
                    sequence=len(self.steps) + 1,
                    tool="tool_confirmation",
                    arguments={"tool": tool, "requested_arguments": arguments},
                    basis=decision.basis,
                    narration=f"用户拒绝执行 {tool}",
                    why="执行前必须获得用户确认",
                    evidence_id=evidence.id,
                    ok=False,
                    origin="confirmation",
                    error=result.error,
                )
                self._register_step(step, evidence)
                self._append_turn(
                    role="tool",
                    kind="tool_result",
                    content=f"{tool} 被用户拒绝执行；未获得证据。",
                )
                return result
        started = time.monotonic()
        result = self.registry.invoke(tool, arguments, self._ctx)
        result.duration_ms = int((time.monotonic() - started) * 1000)
        evidence = self.store.add(tool, result, target=self.record.target)
        step = ToolStep(
            sequence=len(self.steps) + 1,
            tool=tool,
            arguments=arguments,
            basis=decision.basis,
            narration=decision.narration,
            why=decision.why,
            evidence_id=evidence.id,
            ok=result.ok,
            origin="planner",
            duration_ms=result.duration_ms,
            error=result.error,
        )
        self._register_step(step, evidence)
        self.audit.write(
            "tool_call",
            origin="planner",
            round=round_number,
            attempt=attempt,
            tool=tool,
            arguments=arguments,
            basis=decision.basis,
            narration=decision.narration,
            evidence_id=evidence.id,
            ok=result.ok,
            duration_ms=result.duration_ms,
            error=result.error,
        )
        self._append_turn(
            role="tool",
            kind="tool_result",
            content=(
                f"{tool} {'成功' if result.ok else '失败'}（{evidence.id}）："
                f"{result.summary or result.error or ''}"
            ),
        )
        return result

    def _handle_action_proposal(self, decision: Decision, *, round_number: int) -> None:
        proposal = {
            "narration": decision.narration,
            "basis": decision.basis,
            "actions": decision.actions,
        }
        auto_approved = self.options.permission_mode == "trusted" and all(
            str(item.get("risk") or "medium") == "low" and not item.get("requires_admin")
            for item in decision.actions
        )
        if auto_approved:
            approved = True
            self.sink.on_phase("信任模式：低风险动作无需二次确认，已无条件放行。")
        else:
            approved = self.sink.confirm_actions(proposal)
        self.audit.write(
            "action_proposal",
            round=round_number,
            approved=approved,
            auto_approved=auto_approved,
            permission_mode=self.options.permission_mode,
            actions=decision.actions,
            basis=decision.basis,
        )
        if not approved:
            self.sink.on_warning("用户拒绝了本次修复动作提案。")
            self.actions.append(
                {
                    "approved": False,
                    "reason": "user_rejected",
                    "proposed_at": _now_iso(),
                    "actions": decision.actions,
                }
            )
            self.record.actions.append(self.actions[-1])
            self._append_turn(
                role="assistant",
                kind="action_rejected",
                content="用户拒绝修复动作提案：" + "；".join(str(item.get("title")) for item in decision.actions),
            )
            return
        for action in decision.actions:
            self._check_cancel()
            outcome = execute_action(
                action,
                cancel_event=self.cancel_event,
                timeout_seconds=self.options.action_timeout_seconds,
            )
            outcome_dict = outcome.to_dict()
            self.actions.append(outcome_dict)
            self.record.actions.append(outcome_dict)
            self.audit.write("action_executed", **outcome_dict)
            output = (outcome.stdout + ("\n" + outcome.stderr if outcome.stderr else "")).strip()
            result = ToolResult(
                ok=outcome.ok,
                summary=(
                    f"动作「{action.get('title')}」"
                    + ("成功" if outcome.ok else "失败")
                    + f"（退出码 {outcome.returncode}）"
                ),
                data={
                    "action": action,
                    "outcome": {
                        "returncode": outcome.returncode,
                        "elevated": outcome.elevated,
                        "started_at": outcome.started_at,
                        "finished_at": outcome.finished_at,
                        "duration_ms": outcome.duration_ms,
                        "stdout": outcome.stdout,
                        "stderr": outcome.stderr,
                        "error": outcome.error,
                    },
                },
                source="Helper Plus Action Executor",
                native_command=outcome.command,
                error=outcome.error,
                duration_ms=outcome.duration_ms,
            )
            evidence = self.store.add("action_execute", result, target=self.record.target)
            step = ToolStep(
                sequence=len(self.steps) + 1,
                tool="action_execute",
                arguments={"title": action.get("title"), "command": outcome.command, "shell": outcome.shell},
                basis=decision.basis,
                narration=f"执行用户确认的动作：{action.get('title')}",
                why=action.get("rationale") or "",
                evidence_id=evidence.id,
                ok=outcome.ok,
                origin="action",
                duration_ms=outcome.duration_ms,
                error=outcome.error,
            )
            self._register_step(step, evidence)
            self._append_turn(
                role="tool",
                kind="action_result",
                content=(
                    f"动作「{action.get('title')}」{'成功' if outcome.ok else '失败'}"
                    f"（exit={outcome.returncode}, elevated={outcome.elevated}, {evidence.id}）"
                ),
            )
            self.sink.on_phase(
                f"动作执行{'成功' if outcome.ok else '失败'}：{action.get('title')}（退出码 {outcome.returncode}）"
            )
        self._sync_record()

    # --------------------------------------------------- local context / cases
    def _append_turn(self, *, role: str, kind: str, content: str) -> None:
        self._turn_counter += 1
        turn = {
            "id": f"T{self._turn_counter}",
            "role": role,
            "kind": kind,
            "content": str(content or "")[:2400],
            "timestamp": _now_iso(),
        }
        self.turns.append(turn)
        self.record.context_turns.append(turn)

    def _filtered_turns(self) -> list[dict[str, Any]]:
        if not self.turns:
            return []
        keep_ids: set[str] = set()
        decisions = (self.local_context or {}).get("recent_turn_decisions") or {}
        if isinstance(decisions, dict):
            keep_ids = {str(item) for item in decisions.get("keep_turn_ids") or []}
        always_ids = {self.turns[0]["id"]} if self.turns else set()
        always_ids.update(turn["id"] for turn in self.turns[-2:])
        if keep_ids:
            selected = [
                turn for turn in self.turns if turn["id"] in keep_ids or turn["id"] in always_ids
            ]
        else:
            selected = self.turns[-10:]
        # 去重并保持时间顺序
        seen: set[str] = set()
        result = []
        for turn in selected:
            if turn["id"] in seen:
                continue
            seen.add(turn["id"])
            result.append(turn)
        return result[-12:]

    def _refresh_local_context(self, round_number: int) -> None:
        agent = self.options.local_context
        if agent is None:
            return
        changed = (
            len(self.interactions) > self._last_local_interactions
            or len(self.actions) > self._last_local_actions
        )
        due = round_number - self._last_local_round >= 8
        if self.local_context is not None and not changed and not due:
            return
        # 用户要求对话/上下文都经过本地模型处理，不再设置“每次会话最多 3 次”
        # 的应用级上限；后台同一时间只跑一个 Qwen 刷新，完成后继续后续轮次。
        self._last_local_round = round_number
        self._local_refresh_count += 1
        if self.options.local_async:
            self._start_local_context_async(round_number)
            return
        self.sink.on_phase("本地 Qwen 正在整理上下文、历史病历与近期消息…")
        try:
            cases = agent.archive.related_cases(
                record_id=self.record.record_id,
                target=self.record.target,
                issue=self.issue,
            )
            self.prior_cases = cases
            self.local_context = agent.analyze(
                record_id=self.record.record_id,
                target=self.record.target or "本机系统",
                target_kind=self.record.target_kind,
                issue=self.issue,
                status=self.record.status,
                elapsed_seconds=time.monotonic() - self._started_monotonic,
                round_number=round_number,
                plan=self.plan,
                evidence=self.store.to_list(),
                steps=[step.to_dict() for step in self.steps],
                interactions=self.interactions,
                actions=self.actions,
                recent_turns=self.turns,
                main_goal=self.confirmed_user_goal,
            )
            self._last_local_interactions = len(self.interactions)
            self._last_local_actions = len(self.actions)
            if self.local_context:
                self.record.local_context = self.local_context
                self.audit.write(
                    "local_context",
                    round=round_number,
                    goal_candidate=self.local_context.get("user_goal_candidate"),
                    completed=self.local_context.get("completed"),
                    unfinished=self.local_context.get("unfinished"),
                    keep_turn_ids=(self.local_context.get("recent_turn_decisions") or {}).get("keep_turn_ids"),
                )
                self._append_turn(
                    role="assistant",
                    kind="local_context",
                    content=str(self.local_context.get("context_summary") or "")[:1800],
                )
                self.sink.on_phase("本地 Qwen 上下文整理完成，已作为主模型参考（非最终结论）。")
            else:
                self.local_context_error = getattr(agent, "last_error", None)
                if self.local_context_error:
                    self.audit.write("local_context_error", error=self.local_context_error)
                    self.sink.on_warning(f"本地 Qwen 上下文参考不可用：{self.local_context_error}")
        except Exception as exc:  # noqa: BLE001
            self.local_context_error = f"{type(exc).__name__}: {exc}"
            self.audit.write("local_context_error", error=self.local_context_error)
            self.sink.on_warning(f"本地 Qwen 上下文参考不可用：{self.local_context_error}")

    def _start_local_context_async(self, round_number: int) -> None:
        """后台运行本地 Qwen，绝不阻塞主 Planner。"""
        agent = self.options.local_context
        if agent is None or self._local_busy:
            return
        self._local_busy = True
        self.sink.on_phase("本地 Qwen 在后台整理上下文（不阻塞主模型）…")
        snapshot = {
            "record_id": self.record.record_id,
            "target": self.record.target or "本机系统",
            "target_kind": self.record.target_kind,
            "issue": self.issue,
            "status": self.record.status,
            "elapsed_seconds": time.monotonic() - self._started_monotonic,
            "round_number": round_number,
            "plan": self.plan,
            "evidence": self.store.to_list(),
            "steps": [step.to_dict() for step in self.steps],
            "interactions": list(self.interactions),
            "actions": list(self.actions),
            "recent_turns": list(self.turns),
            "main_goal": self.confirmed_user_goal,
        }
        thread = threading.Thread(
            target=self._local_context_async_worker,
            args=(round_number, snapshot),
            daemon=True,
            name="local-qwen-context",
        )
        self._local_thread = thread
        thread.start()

    def _local_context_async_worker(self, round_number: int, data: dict[str, Any]) -> None:
        agent = self.options.local_context
        try:
            cases = agent.archive.related_cases(
                record_id=self.record.record_id,
                target=self.record.target,
                issue=self.issue,
            )
            result = agent.analyze(
                record_id=data["record_id"],
                target=data["target"],
                target_kind=data["target_kind"],
                issue=data["issue"],
                status=data["status"],
                elapsed_seconds=data["elapsed_seconds"],
                round_number=round_number,
                plan=data["plan"],
                evidence=data["evidence"],
                steps=data["steps"],
                interactions=data["interactions"],
                actions=data["actions"],
                recent_turns=data["recent_turns"],
                main_goal=data["main_goal"],
            )
            with self._state_lock:
                self.prior_cases = cases
                self.local_context = result
                self.record.local_context = result
                self._last_local_interactions = len(self.interactions)
                self._last_local_actions = len(self.actions)
            if result:
                self.audit.write(
                    "local_context",
                    round=round_number,
                    goal_candidate=result.get("user_goal_candidate"),
                    completed=result.get("completed"),
                    unfinished=result.get("unfinished"),
                )
                self._append_turn(
                    role="assistant",
                    kind="local_context",
                    content=str(result.get("context_summary") or "")[:1800],
                )
            else:
                self.local_context_error = getattr(agent, "last_error", None)
                if self.local_context_error:
                    self.audit.write("local_context_error", error=self.local_context_error)
        except Exception as exc:  # noqa: BLE001
            self.local_context_error = f"{type(exc).__name__}: {exc}"
            self.audit.write("local_context_error", error=self.local_context_error)
        finally:
            self._local_busy = False

    def _finalize_local_history(self) -> None:
        """把本次运行翻译成 Markdown 日志，并归档到病历；永不删除历史。"""
        agent = self.options.local_context
        if agent is None:
            return
        if self.options.local_async:
            thread = threading.Thread(
                target=self._finalize_local_history_sync,
                daemon=True,
                name="local-qwen-log",
            )
            self._finalize_thread = thread
            thread.start()
            return
        self._finalize_local_history_sync()

    def _finalize_local_history_sync(self) -> None:
        agent = self.options.local_context
        if agent is None:
            return
        try:
            case_path = agent.finalize(
                record=self.record.to_dict(),
                local_context=self.local_context,
                main_goal=self.confirmed_user_goal,
            )
            self.record.case_path = str(case_path)
            self.audit.write("case_archived", path=str(case_path))
            self._save_record()
        except Exception as exc:  # noqa: BLE001
            self.local_context_error = f"{type(exc).__name__}: {exc}"
            self.audit.write("case_archive_error", error=self.local_context_error)

    def _parse_and_validate(self, raw: str, *, must_final: bool = False) -> Decision:
        obj = extract_json_object(raw)
        return validate_decision(
            obj,
            self.store.ids,
            require_evidence_reference=bool(self.store.ids),
            require_final=must_final,
        )

    # ----------------------------------------------------------------- helpers
    def _record_planner_attempt(
        self,
        round_number: int,
        attempt: int,
        action: str,
        raw: str,
        *,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        self.record.planner.append(
            {
                "round": round_number,
                "attempt": attempt,
                "action": action,
                "error": error,
                "duration_ms": duration_ms,
                "raw_output": raw[:30000],
                "timestamp": _now_iso(),
            }
        )

    def _audit_planner_request(self, round_number: int, attempt: int, user_message: str) -> None:
        self.audit.write(
            "planner_request",
            round=round_number,
            attempt=attempt,
            evidence_ids=[item.id for item in self.store.items],
            payload_chars=len(user_message),
        )

    def _accumulate_usage(self, usage: dict[str, Any]) -> None:
        if not isinstance(usage, dict) or not usage:
            return
        for key, value in usage.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            self.record.usage[key] = self.record.usage.get(key, 0) + int(value)
        self.sink.on_usage(dict(self.record.usage))

    def _sync_record(self) -> None:
        self.record.evidence = self.store.to_list()
        self.record.tool_steps = [step.to_dict() for step in self.steps]
        self.record.interactions = list(self.interactions)
        self.record.actions = list(self.actions)
        self.record.plan = self.plan
        self.record.context_turns = list(self.turns)
        self.record.local_context = self.local_context
        self.record.confirmed_user_goal = self.confirmed_user_goal
        self.record.elapsed_seconds = round(time.monotonic() - self._started_monotonic, 2)

    def _save_record(self) -> None:
        try:
            self.last_record_path = self.record.save(reports_dir())
            self.audit.write("record_saved", path=str(self.last_record_path))
        except OSError as exc:
            self.record.errors.append(f"保存诊断记录失败：{exc}")

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise _Cancelled()

    @staticmethod
    def _call_key(tool: str, arguments: dict[str, Any]) -> str:
        return tool + "\x00" + json.dumps(arguments, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        if isinstance(exc, LLMConfigurationError):
            return str(exc)
        if isinstance(exc, PlannerProtocolError):
            return "模型输出未通过协议校验：" + "；".join(exc.errors)
        if isinstance(exc, LLMError):
            return str(exc)
        if isinstance(exc, SafetyError):
            return f"安全校验拦截：{exc}"
        return f"{type(exc).__name__}: {exc}"


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")
