"""AI 报告渲染、HTML 转义与结构化字段测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from helper_plus.reporting import render_export_html, render_report_html


def _report():
    return {
        "headline": "测试结论 <script>alert(1)</script>",
        "summary": "系统信息 [E1] 与文件签名 [E2] 已读取。",
        "findings": [
            {
                "text": "系统为 Windows 11 <b>不安全文本</b> [E1]",
                "evidence": ["E1"],
                "confidence": 0.9,
            }
        ],
        "candidate_causes": [
            {
                "cause": "候选原因 <script>alert(2)</script>，[E1]",
                "confidence": 0.4,
                "evidence": ["E1", "E2"],
                "supporting": "支持文本",
                "against": "反证文本",
            }
        ],
        "next_steps": [
            {
                "title": "只读复核 <script>alert(3)</script>",
                "why": "证据 [E1] 支持继续观察。",
                "risk": "read-only",
                "executed": False,
                "evidence": ["E1"],
            }
        ],
        "limitations": ["测试限制 <script>alert(4)</script>"],
    }


def _record_view():
    return SimpleNamespace(
        target="C:\\Windows\\System32\\kernel32.dll",
        issue="测试 <script> 注入",
        mode="ai",
        model="deepseek-flash",
        status="completed",
        started_at="2026-01-01T00:00:00+08:00",
        finished_at="2026-01-01T00:01:00+08:00",
        record_id="DIAG-TEST",
        generator="test",
        target_kind="file",
        elapsed_seconds=60,
        evidence=[
            {
                "id": "E1",
                "summary": "test <b>summary</b>",
                "data": {"evil": "<script>alert(1)</script>"},
            },
            {"id": "E2", "summary": "kernel32.dll 签名有效", "data": {}},
        ],
        tool_steps=[
            {
                "sequence": 1,
                "tool": "get_system_info",
                "basis": ["Q1"],
                "evidence_id": "E1",
                "ok": True,
            }
        ],
        plan={
            "goal": "核对文件与日志",
            "steps": ["读取证据", "验证签名"],
            "tools": ["inspect_file", "get_event_logs"],
            "notes": "只读计划。",
        },
        actions=[
            {
                "action": {
                    "title": "测试动作",
                    "command": "echo hello",
                },
                "ok": True,
                "returncode": 0,
                "elevated": False,
                "duration_ms": 12,
                "stdout": "hello <script>action</script>",
                "stderr": "",
            }
        ],
        interactions=[
            {"question": "何时出现？", "answer": "更新后 <script>answer</script>"}
        ],
    )


def test_html_escapes_untrusted_text_and_renders_plan_actions():
    report = _report()
    page = render_report_html(report, _record_view())
    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page
    assert "AI 执行计划" in page
    assert "已确认并执行的动作" in page
    assert "用户补充信息" in page
    assert "E1" in page
    export = render_export_html(_record_view(), report)
    assert "<script>alert" not in export
    assert "DIAG-TEST" in export
    assert "证据" in export


def test_low_confidence_inference_is_hidden_from_user_report():
    report = _report()
    report["candidate_causes"][0]["confidence"] = 0.3
    report["candidate_causes"][0]["cause"] = "低可信度猜测不应显示"
    page = render_report_html(report, _record_view())
    assert "低可信度猜测不应显示" not in page
    assert "候选原因" not in page
    export = render_export_html(_record_view(), report)
    assert "低可信度猜测不应显示" not in export


def test_report_json_serializable():
    report = _report()
    raw = json.dumps(report, ensure_ascii=False)
    assert "summary" in raw
    assert "[E1]" in raw
