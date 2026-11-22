"""Planner 输出协议与证据引用校验测试。"""

from __future__ import annotations

import copy
import json

import pytest

from helper_plus.core.validation import (
    PlannerProtocolError,
    extract_json_object,
    validate_decision,
)


def _final_payload():
    return {
        "narration": "基于 [E1] 的签名事实进行判断。",
        "action": "final",
        "basis": ["E1"],
        "final": {
            "headline": "签名状态正常，但证据不足以定位根因。",
            "summary": "目标文件签名有效 [E1]；仍需更多日志证据。",
            "findings": [
                {"text": "签名状态有效。", "evidence": ["E1"], "confidence": 0.95}
            ],
            "candidate_causes": [
                {
                    "cause": "证据不足，暂不能定位。",
                    "confidence": 0.2,
                    "evidence": ["E1"],
                    "supporting": "仅有一项静态文件证据。",
                    "against": "尚未读取全部事件日志。",
                }
            ],
            "next_steps": [
                {
                    "title": "继续只读检查事件日志。",
                    "why": "补充时间线证据。",
                    "risk": "read-only",
                    "executed": False,
                    "evidence": ["E1"],
                }
            ],
            "limitations": ["只读原型，未执行修复。"],
        },
    }


def test_valid_final():
    decision = validate_decision(_final_payload(), {"E1"})
    assert decision.action == "final"
    assert decision.final is not None
    assert decision.final["findings"][0]["evidence"] == ["E1"]
    assert decision.final["next_steps"][0]["executed"] is False


def test_unknown_evidence_reference_rejected():
    payload = _final_payload()
    payload["basis"] = ["E99"]
    with pytest.raises(PlannerProtocolError, match="不存在的证据"):
        validate_decision(payload, {"E1"})


def test_tool_call_does_not_require_basis_or_narration():
    decision = validate_decision(
        {
            "action": "call_tool",
            "tool": "get_event_logs",
            "arguments": {"log": "System", "hours": 24},
        },
        {"E1"},
    )
    assert decision.action == "call_tool"
    assert decision.narration == ""
    assert decision.basis == []


def test_ask_user_always_gets_other_option():
    decision = validate_decision(
        {
            "action": "ask_user",
            "question": {"prompt": "是否继续？", "options": ["是", "否"]},
        },
        {"E1"},
    )
    options = decision.question["options"]
    assert any("其他" in option for option in options)
    assert decision.question["allow_free_text"] is True


def test_basis_must_contain_evidence_after_baseline():
    payload = _final_payload()
    payload["basis"] = ["Q1"]
    with pytest.raises(PlannerProtocolError, match="至少引用一个 E#"):
        validate_decision(payload, {"E1"})


def test_final_summary_requires_citation():
    payload = _final_payload()
    payload["final"]["summary"] = "签名有效，但没有引用任何证据。"
    with pytest.raises(PlannerProtocolError, match="至少一个证据引用"):
        validate_decision(payload, {"E1"})


def test_final_requires_evidence_on_each_item():
    payload = _final_payload()
    payload["final"]["findings"][0]["evidence"] = ["Q1"]
    with pytest.raises(PlannerProtocolError, match="至少一个证据"):
        validate_decision(payload, {"E1"})


def test_final_cannot_claim_execution():
    payload = _final_payload()
    payload["final"]["next_steps"][0]["executed"] = True
    with pytest.raises(PlannerProtocolError, match="executed"):
        validate_decision(payload, {"E1"})


def test_final_risk_enum():
    payload = _final_payload()
    payload["final"]["next_steps"][0]["risk"] = "run-now"
    with pytest.raises(PlannerProtocolError, match="risk"):
        validate_decision(payload, {"E1"})


def test_require_final_at_last_round():
    payload = {
        "narration": "读取更多证据 [E1]",
        "action": "call_tool",
        "basis": ["E1"],
        "tool": "get_event_logs",
        "arguments": {"log": "System"},
        "why": "需要日志。",
    }
    with pytest.raises(PlannerProtocolError, match="必须返回 action=final"):
        validate_decision(payload, {"E1"}, require_final=True)


def test_extract_json_from_code_fence_and_prose():
    raw = '好的，结果如下：\n```json\n{"a": 1}\n```\n谢谢'
    assert extract_json_object(raw) == {"a": 1}


def test_extract_rejects_empty_and_non_json():
    with pytest.raises(PlannerProtocolError, match="空内容"):
        extract_json_object("  ")
    with pytest.raises(PlannerProtocolError, match="未找到 JSON"):
        extract_json_object("no json here")


def test_confidence_range_and_type():
    payload = copy.deepcopy(_final_payload())
    payload["final"]["findings"][0]["confidence"] = 1.5
    with pytest.raises(PlannerProtocolError, match="0–1"):
        validate_decision(payload, {"E1"})
    payload = copy.deepcopy(_final_payload())
    payload["final"]["findings"][0]["confidence"] = True
    with pytest.raises(PlannerProtocolError, match="数字"):
        validate_decision(payload, {"E1"})


def test_call_tool_decision_shape():
    decision = validate_decision(
        {
            "narration": "根据 [E1]，需要检查日志。",
            "action": "call_tool",
            "basis": ["E1"],
            "tool": "get_event_logs",
            "arguments": {"log": "System", "hours": 24, "max_events": 10},
            "why": "确认近期错误。",
        },
        {"E1"},
    )
    assert decision.tool == "get_event_logs"
    assert decision.arguments["log"] == "System"


def test_json_contract_contains_documented_fields():
    payload = _final_payload()
    dumped = json.dumps(payload, ensure_ascii=False)
    assert '"candidate_causes"' in dumped
    assert '"next_steps"' in dumped
