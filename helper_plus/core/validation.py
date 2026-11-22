"""Planner 输出协议：严格 JSON 解析、证据引用校验、字段归一化。

这些校验是减少幻觉的最后一道闸门：
- 只有 final/原因推理必须引用真实 E#；工具调用、ask_user、propose_action、plan
  不强制 basis/narration（用户要求自由调用验证工具）；
- final 中每个 finding/cause/next_step 都必须绑定证据 ID；
- final 的 next_steps 不得声称已执行；修复动作只能通过 propose_action + 用户确认进入 action_history。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any


class PlannerProtocolError(ValueError):
    """模型输出不符合协议。错误消息会反馈给模型做一次修正。"""

    def __init__(self, errors: list[str] | str) -> None:
        if isinstance(errors, str):
            errors = [errors]
        self.errors = errors
        super().__init__("；".join(errors))

    def feedback(self) -> str:
        return "上一条输出未通过协议/证据校验：" + "；".join(self.errors)


_REF_RE = re.compile(r"\b(Q1|E\d+)\b")
_RISKS = {"read-only", "requires-user-confirmation", "requires-admin"}
_ACTION_RISKS = {"low", "medium", "high"}


@dataclass
class Decision:
    action: str
    narration: str
    basis: list[str]
    tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    tools: list[dict[str, Any]] = field(default_factory=list)
    question: dict[str, Any] | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)
    plan: dict[str, Any] | None = None
    why: str = ""
    final: dict[str, Any] | None = None
    raw_json: dict[str, Any] = field(default_factory=dict)


def extract_json_object(text: str) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise PlannerProtocolError("模型返回空内容。")
    cleaned = text.strip().lstrip("\ufeff")
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    if start < 0:
        raise PlannerProtocolError("未找到 JSON 对象（输出必须是以 { 开始的对象）。")
    try:
        obj, end = json.JSONDecoder().raw_decode(cleaned, start)
    except ValueError as exc:
        raise PlannerProtocolError(f"JSON 语法错误：{exc}") from exc
    if not isinstance(obj, dict):
        raise PlannerProtocolError("JSON 根节点必须是对象。")
    return obj


def _valid_refs(
    value: Any,
    evidence_ids: set[str],
    *,
    allow_empty: bool = False,
) -> list[str]:
    allowed = set(evidence_ids) | {"Q1"}
    if value is None and allow_empty:
        return []
    if not isinstance(value, list) or (not value and not allow_empty):
        raise PlannerProtocolError("evidence/basis 必须是非空数组。")
    refs: list[str] = []
    for item in value:
        ref = str(item).strip()
        if ref not in allowed:
            raise PlannerProtocolError(f"引用了不存在的证据 ID：{ref}")
        if ref not in refs:
            refs.append(ref)
    return refs


def _text(value: Any, field_name: str, *, max_len: int, required: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise PlannerProtocolError(f"{field_name} 必须是字符串。")
    text = value.strip()
    if required and not text:
        raise PlannerProtocolError(f"{field_name} 不能为空。")
    if len(text) > max_len:
        raise PlannerProtocolError(f"{field_name} 超过 {max_len} 字符。")
    return text


def _confidence(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise PlannerProtocolError(f"{field_name} 必须是 0–1 的数字。")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PlannerProtocolError(f"{field_name} 必须是 0–1 的数字。") from exc
    if not 0.0 <= number <= 1.0:
        raise PlannerProtocolError(f"{field_name} 必须在 0–1 之间。")
    return round(number, 3)


def _normalize_final(final: Any, evidence_ids: set[str]) -> dict[str, Any]:
    if not isinstance(final, dict):
        raise PlannerProtocolError("action=final 时必须提供 final 对象。")
    headline = _text(final.get("headline"), "final.headline", max_len=300)
    summary = _text(final.get("summary"), "final.summary", max_len=3000)
    summary_refs = _REF_RE.findall(summary)
    if not any(ref.startswith("E") for ref in summary_refs):
        raise PlannerProtocolError("final.summary 必须用 [E#] 标出至少一个证据引用。")

    findings_raw = final.get("findings")
    if not isinstance(findings_raw, list) or not (1 <= len(findings_raw) <= 8):
        raise PlannerProtocolError("final.findings 必须是 1–8 项的数组。")
    findings: list[dict[str, Any]] = []
    for index, item in enumerate(findings_raw, 1):
        if not isinstance(item, dict):
            raise PlannerProtocolError(f"final.findings[{index}] 必须是对象。")
        text = _text(item.get("text"), f"final.findings[{index}].text", max_len=1200)
        refs = _valid_refs(item.get("evidence"), evidence_ids)
        if not any(ref.startswith("E") for ref in refs):
            raise PlannerProtocolError(f"final.findings[{index}] 必须引用至少一个证据 ID。")
        findings.append(
            {
                "text": text,
                "evidence": refs,
                "confidence": _confidence(item.get("confidence"), f"final.findings[{index}].confidence"),
            }
        )

    causes_raw = final.get("candidate_causes")
    if not isinstance(causes_raw, list) or not (1 <= len(causes_raw) <= 6):
        raise PlannerProtocolError("final.candidate_causes 必须是 1–6 项的数组。")
    causes: list[dict[str, Any]] = []
    for index, item in enumerate(causes_raw, 1):
        if not isinstance(item, dict):
            raise PlannerProtocolError(f"final.candidate_causes[{index}] 必须是对象。")
        cause = _text(item.get("cause"), f"final.candidate_causes[{index}].cause", max_len=500)
        refs = _valid_refs(item.get("evidence"), evidence_ids)
        if not any(ref.startswith("E") for ref in refs):
            raise PlannerProtocolError(f"final.candidate_causes[{index}] 必须引用至少一个证据 ID。")
        causes.append(
            {
                "cause": cause,
                "confidence": _confidence(item.get("confidence"), f"final.candidate_causes[{index}].confidence"),
                "evidence": refs,
                "supporting": _text(
                    item.get("supporting"), f"final.candidate_causes[{index}].supporting",
                    max_len=1000, required=False
                ),
                "against": _text(
                    item.get("against"), f"final.candidate_causes[{index}].against",
                    max_len=1000, required=False
                )
                or "暂无已知反证。",
            }
        )

    steps_raw = final.get("next_steps")
    if not isinstance(steps_raw, list) or not (1 <= len(steps_raw) <= 8):
        raise PlannerProtocolError("final.next_steps 必须是 1–8 项的数组。")
    next_steps: list[dict[str, Any]] = []
    for index, item in enumerate(steps_raw, 1):
        if not isinstance(item, dict):
            raise PlannerProtocolError(f"final.next_steps[{index}] 必须是对象。")
        title = _text(item.get("title"), f"final.next_steps[{index}].title", max_len=300)
        why = _text(item.get("why"), f"final.next_steps[{index}].why", max_len=1200)
        risk = _text(item.get("risk"), f"final.next_steps[{index}].risk", max_len=40)
        if risk not in _RISKS:
            raise PlannerProtocolError(
                f"final.next_steps[{index}].risk 必须是 {sorted(_RISKS)} 之一。"
            )
        if item.get("executed") is not False:
            raise PlannerProtocolError(
                f"final.next_steps[{index}].executed 必须为 false：修复只能通过受控动作执行。"
            )
        refs = _valid_refs(item.get("evidence"), evidence_ids)
        if not any(ref.startswith("E") for ref in refs):
            raise PlannerProtocolError(f"final.next_steps[{index}] 必须引用至少一个证据 ID。")
        next_steps.append(
            {
                "title": title,
                "why": why,
                "risk": risk,
                "executed": False,
                "evidence": refs,
            }
        )

    limitations_raw = final.get("limitations", [])
    if limitations_raw is None:
        limitations_raw = []
    if not isinstance(limitations_raw, list):
        raise PlannerProtocolError("final.limitations 必须是字符串数组。")
    limitations: list[str] = []
    for index, item in enumerate(limitations_raw[:10], 1):
        limitations.append(_text(item, f"final.limitations[{index}]", max_len=800, required=False))

    return {
        "headline": headline,
        "summary": summary,
        "findings": findings,
        "candidate_causes": causes,
        "next_steps": next_steps,
        "limitations": limitations,
    }


def _normalize_question(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlannerProtocolError("action=ask_user 时必须提供 question 对象。")
    prompt = _text(value.get("prompt"), "question.prompt", max_len=2000)
    options_raw = value.get("options", [])
    if options_raw is None:
        options_raw = []
    if not isinstance(options_raw, list) or len(options_raw) > 12:
        raise PlannerProtocolError("question.options 必须是最多 12 项的字符串数组。")
    options = [_text(item, "question.options", max_len=300) for item in options_raw]
    # 用户要求任何追问弹窗都必须有“其他”入口，允许补充未列出的情况。
    if not any("其他" in option for option in options):
        options.append("其他（请自行补充）")
    allow_free_text = value.get("allow_free_text", True)
    if not isinstance(allow_free_text, bool):
        raise PlannerProtocolError("question.allow_free_text 必须是布尔值。")
    # 有“其他”选项时必须有自由文本框。
    allow_free_text = True
    return {"prompt": prompt, "options": options, "allow_free_text": allow_free_text}


def _normalize_actions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not (1 <= len(value) <= 5):
        raise PlannerProtocolError("action=propose_action 时必须提供 1–5 个 actions。")
    actions: list[dict[str, Any]] = []
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict):
            raise PlannerProtocolError(f"actions[{index}] 必须是对象。")
        title = _text(item.get("title"), f"actions[{index}].title", max_len=240)
        command = _text(item.get("command"), f"actions[{index}].command", max_len=8000)
        shell = _text(item.get("shell", "cmd"), f"actions[{index}].shell", max_len=20).lower()
        if shell not in {"cmd", "powershell"}:
            raise PlannerProtocolError(f"actions[{index}].shell 只能是 cmd 或 powershell。")
        risk = _text(item.get("risk", "medium"), f"actions[{index}].risk", max_len=20).lower()
        if risk not in _ACTION_RISKS:
            raise PlannerProtocolError(f"actions[{index}].risk 必须是 low/medium/high。")
        requires_admin = item.get("requires_admin", False)
        if not isinstance(requires_admin, bool):
            raise PlannerProtocolError(f"actions[{index}].requires_admin 必须是布尔值。")
        backup_paths_raw = item.get("backup_paths") or []
        if not isinstance(backup_paths_raw, list) or len(backup_paths_raw) > 8:
            raise PlannerProtocolError(f"actions[{index}].backup_paths 必须是最多 8 项的路径数组。")
        backup_paths = [
            _text(path, f"actions[{index}].backup_paths", max_len=1024)
            for path in backup_paths_raw
        ]
        rollback_shell = _text(
            item.get("rollback_shell", "cmd"),
            f"actions[{index}].rollback_shell",
            max_len=20,
        ).lower()
        if rollback_shell not in {"cmd", "powershell"}:
            raise PlannerProtocolError(f"actions[{index}].rollback_shell 只能是 cmd 或 powershell。")
        actions.append(
            {
                "title": title,
                "command": command,
                "shell": shell,
                "risk": risk,
                "requires_admin": requires_admin,
                "rationale": _text(
                    item.get("rationale", ""), f"actions[{index}].rationale", max_len=1500, required=False
                ),
                "rollback": _text(
                    item.get("rollback", ""), f"actions[{index}].rollback", max_len=1500, required=False
                ),
                "backup_paths": backup_paths,
                "rollback_command": _text(
                    item.get("rollback_command", ""),
                    f"actions[{index}].rollback_command",
                    max_len=8000,
                    required=False,
                ),
                "rollback_shell": rollback_shell,
            }
        )
    return actions


def _normalize_plan(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlannerProtocolError("action=plan 时必须提供 plan 对象。")
    goal = _text(value.get("goal"), "plan.goal", max_len=800)
    confirmed_user_goal = _text(
        value.get("confirmed_user_goal"),
        "plan.confirmed_user_goal",
        max_len=1200,
    )
    steps_raw = value.get("steps")
    if not isinstance(steps_raw, list) or not (1 <= len(steps_raw) <= 20):
        raise PlannerProtocolError("plan.steps 必须是 1–20 项的字符串数组。")
    steps = [_text(item, "plan.steps", max_len=500) for item in steps_raw]
    tools_raw = value.get("tools", [])
    if tools_raw is None:
        tools_raw = []
    if not isinstance(tools_raw, list) or len(tools_raw) > 20:
        raise PlannerProtocolError("plan.tools 必须是最多 20 项的字符串数组。")
    tools = [_text(item, "plan.tools", max_len=120) for item in tools_raw]
    notes = _text(value.get("notes", ""), "plan.notes", max_len=2000, required=False)
    return {
        "goal": goal,
        "confirmed_user_goal": confirmed_user_goal,
        "steps": steps,
        "tools": tools,
        "notes": notes,
    }


def validate_decision(
    obj: dict[str, Any],
    evidence_ids: set[str],
    *,
    require_evidence_reference: bool = True,
    require_final: bool = False,
) -> Decision:
    if not isinstance(obj, dict):
        raise PlannerProtocolError("根节点必须是 JSON 对象。")
    action = _text(obj.get("action"), "action", max_len=30)
    if action not in {"plan", "call_tool", "call_tools", "ask_user", "propose_action", "final"}:
        raise PlannerProtocolError(
            "action 只能是 plan、call_tool、call_tools、ask_user、propose_action 或 final。"
        )
    # 用户要求：工具调用、追问、动作提案等操作不再强制 narration/basis。
    # 只有“原因推理/最终结论”需要证据；final 内部字段仍逐项校验 E#。
    narration = _text(obj.get("narration"), "narration", max_len=2500, required=False)
    basis = _valid_refs(obj.get("basis"), evidence_ids, allow_empty=True)
    if (
        require_evidence_reference
        and action == "final"
        and not any(ref.startswith("E") for ref in basis)
    ):
        raise PlannerProtocolError("final 的 basis 至少引用一个 E# 证据 ID。")
    if require_final and action != "final":
        raise PlannerProtocolError("当前轮次必须返回 action=final。")

    if action == "plan":
        return Decision(
            action=action,
            narration=narration,
            basis=basis,
            plan=_normalize_plan(obj.get("plan")),
            raw_json=obj,
        )

    if action == "call_tool":
        tool = obj.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            raise PlannerProtocolError("action=call_tool 时必须提供 tool。")
        arguments = obj.get("arguments", {})
        if not isinstance(arguments, dict):
            raise PlannerProtocolError("arguments 必须是 JSON 对象。")
        why = _text(obj.get("why"), "why", max_len=1200, required=False)
        return Decision(
            action=action,
            narration=narration,
            basis=basis,
            tool=tool.strip(),
            arguments=arguments,
            why=why,
            raw_json=obj,
        )

    if action == "call_tools":
        tools_raw = obj.get("tools")
        if not isinstance(tools_raw, list) or not (1 <= len(tools_raw) <= 8):
            raise PlannerProtocolError("action=call_tools 时必须提供 1–8 个工具。")
        tools: list[dict[str, Any]] = []
        for index, item in enumerate(tools_raw, 1):
            if not isinstance(item, dict):
                raise PlannerProtocolError(f"tools[{index}] 必须是对象。")
            name = item.get("tool")
            if not isinstance(name, str) or not name.strip():
                raise PlannerProtocolError(f"tools[{index}].tool 不能为空。")
            arguments = item.get("arguments", {})
            if not isinstance(arguments, dict):
                raise PlannerProtocolError(f"tools[{index}].arguments 必须是对象。")
            tools.append({"tool": name.strip(), "arguments": arguments})
        why = _text(obj.get("why"), "why", max_len=1500, required=False)
        return Decision(
            action=action,
            narration=narration,
            basis=basis,
            tools=tools,
            why=why,
            raw_json=obj,
        )

    if action == "ask_user":
        question = _normalize_question(obj.get("question"))
        return Decision(
            action=action,
            narration=narration,
            basis=basis,
            question=question,
            raw_json=obj,
        )

    if action == "propose_action":
        actions = _normalize_actions(obj.get("actions"))
        return Decision(
            action=action,
            narration=narration,
            basis=basis,
            actions=actions,
            raw_json=obj,
        )

    final = _normalize_final(obj.get("final"), evidence_ids)
    return Decision(
        action=action,
        narration=narration,
        basis=basis,
        final=final,
        why="",
        raw_json=obj,
    )
