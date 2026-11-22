"""AI 分析报告与 HTML 渲染。

本地不再生成"诊断结论"；报告只渲染 Planner 返回的 final（含 E# 证据引用），
并附加计划、用户问答、已确认动作、时间线。HTML 不引入外部资源。
"""

from __future__ import annotations

from datetime import datetime
import html
import json
from pathlib import Path
import re
from typing import Any

from .models import DiagnosticRecord


_REF_PATTERN = re.compile(r"([\[【])(Q1|E\d+)([\]】])")


def _html_text(text: Any, *, refs: bool = True) -> str:
    escaped = html.escape("" if text is None else str(text)).replace("\n", "<br>")
    if refs:

        def _mark(match: re.Match[str]) -> str:
            return (
                '<span style="color:#0b5cab;font-weight:600;">'
                f"{match.group(1)}{match.group(2)}{match.group(3)}</span>"
            )

        escaped = _REF_PATTERN.sub(_mark, escaped)
    return escaped


_STATUS_LABELS = {
    "completed": "完成",
    "partial": "部分完成",
    "cancelled": "已取消",
    "error": "失败",
    "running": "进行中",
}


def _duration_text(seconds: Any) -> str:
    try:
        total = int(float(seconds or 0))
    except (TypeError, ValueError):
        total = 0
    return f"{total // 60:02d}:{total % 60:02d}"


def _report_body_html(report: dict[str, Any], record: DiagnosticRecord) -> str:
    headline = _html_text(report.get("headline"))
    summary = _html_text(report.get("summary"))
    target = _html_text(record.target or "（系统级诊断）", refs=False)
    issue = _html_text(record.issue or "（未填写）", refs=False)
    date = _html_text(record.finished_at or record.started_at, refs=False)
    model = _html_text(record.model, refs=False)
    status_label = _STATUS_LABELS.get(str(record.status), str(record.status))
    target_kind = _html_text(getattr(record, "target_kind", "system"), refs=False)
    elapsed_text = _duration_text(getattr(record, "elapsed_seconds", 0))
    summary_label = "AI 分析"

    findings_html = "".join(
        f'<tr><td style="padding:6px 8px;border-bottom:1px solid #e6e6e6;vertical-align:top;">'
        f'{_html_text(item.get("text"))}<br>'
        f'<span style="color:#666;font-size:9pt;">证据：'
        f'{_html_text("、".join(item.get("evidence") or []), refs=True)} · '
        f'置信度 {float(item.get("confidence") or 0):.2f}</span></td></tr>'
        for item in report.get("findings") or []
    )
    # 不充分/低可信度的推断不呈现给用户：只显示有证据且 confidence>=0.6 的候选原因。
    visible_causes = [
        item
        for item in report.get("candidate_causes") or []
        if float(item.get("confidence") or 0) >= 0.6 and item.get("evidence")
    ]
    causes_html = "".join(
        f'<tr><td style="padding:8px;border-bottom:1px solid #e6e6e6;vertical-align:top;">'
        f'<b>{_html_text(item.get("cause"))}</b><br>'
        f'<span style="color:#666;font-size:9pt;">证据：'
        f'{_html_text("、".join(item.get("evidence") or []), refs=True)} · 可信度 '
        f'{float(item.get("confidence") or 0):.2f}</span><br>'
        f'<span style="color:#2e6b34;">支持：{_html_text(item.get("supporting"))}</span><br>'
        f'<span style="color:#8a4b00;">反证/不确定：{_html_text(item.get("against"))}</span></td></tr>'
        for item in visible_causes
    )
    causes_section = (
        "<h3 style=\"color:#0b3d6e;border-bottom:1px solid #d8e3ef;padding-bottom:3px;margin-top:16px;\">"
        "候选原因</h3>"
        "<table width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" style=\"border-collapse:collapse;\">"
        f"{causes_html}</table>"
    ) if causes_html else ""
    steps_rows = []
    risk_labels = {
        "read-only": "只读",
        "requires-user-confirmation": "需用户确认",
        "requires-admin": "需管理员权限",
    }
    for item in report.get("next_steps") or []:
        steps_rows.append(
            f'<tr><td style="padding:6px 8px;border-bottom:1px solid #e6e6e6;vertical-align:top;">'
            f'<b>{_html_text(item.get("title"))}</b><br>'
            f'<span style="color:#555;">{_html_text(item.get("why"))}</span><br>'
            f'<span style="color:#666;font-size:9pt;">'
            f'{risk_labels.get(str(item.get("risk")), _html_text(item.get("risk")))}'
            f' · {"已执行" if item.get("executed") else "未执行"}'
            f' · 证据：{_html_text("、".join(item.get("evidence") or []), refs=True)}</span></td></tr>'
        )
    limitations_html = "".join(
        f"<li>{_html_text(item)}</li>" for item in report.get("limitations") or []
    )
    actions_html = ""
    actions = getattr(record, "actions", None) or []
    if actions:
        action_rows = []
        for item in actions:
            if not isinstance(item, dict):
                continue
            if item.get("approved") is False:
                action_rows.append(
                    "<tr><td style='padding:6px;border-bottom:1px solid #eee;'>"
                    f"{_html_text('用户拒绝的动作提案', refs=False)}</td>"
                    "<td style='padding:6px;border-bottom:1px solid #eee;'>未执行</td>"
                    "<td style='padding:6px;border-bottom:1px solid #eee;'>—</td></tr>"
                )
                continue
            action = item.get("action") or {}
            action_rows.append(
                "<tr><td style='padding:6px;border-bottom:1px solid #eee;vertical-align:top;'>"
                f"<b>{_html_text(action.get('title') or '动作', refs=False)}</b><br>"
                f"<code>{_html_text(item.get('command') or action.get('command') or '', refs=False)}</code></td>"
                f"<td style='padding:6px;border-bottom:1px solid #eee;vertical-align:top;'>"
                f"{'成功' if item.get('ok') else '失败'}（退出码 {item.get('returncode')}）<br>"
                f"提升：{'是' if item.get('elevated') else '否'}<br>用时 {item.get('duration_ms') or 0} ms</td>"
                f"<td style='padding:6px;border-bottom:1px solid #eee;vertical-align:top;'>"
                f"<span style='white-space:pre-wrap;font-size:8.5pt;'>"
                f"{_html_text((item.get('stdout') or '')[-2000:], refs=False)}</span></td></tr>"
            )
        actions_html = (
            "<h3 style='color:#0b3d6e;border-bottom:1px solid #d8e3ef;padding-bottom:3px;margin-top:16px;'>"
            "已确认并执行的动作</h3><table width='100%' cellspacing='0' cellpadding='0' style='border-collapse:collapse;'>"
            "<thead><tr><th align='left'>动作/命令</th><th align='left'>结果</th><th align='left'>输出摘要</th></tr></thead><tbody>"
            + "".join(action_rows)
            + "</tbody></table>"
        )
    plan_html = ""
    plan = getattr(record, "plan", None)
    if isinstance(plan, dict):
        plan_steps = "".join(
            f"<li>{_html_text(step, refs=False)}</li>" for step in plan.get("steps") or []
        )
        plan_tools = "、".join(_html_text(str(tool), refs=False) for tool in plan.get("tools") or [])
        plan_html = (
            "<h3 style='color:#0b3d6e;border-bottom:1px solid #d8e3ef;padding-bottom:3px;margin-top:16px;'>"
            "AI 执行计划</h3>"
            f"<p><b>目标：</b>{_html_text(plan.get('goal') or '', refs=False)}</p>"
            f"<ol>{plan_steps}</ol>"
            + (f"<p><b>计划工具：</b>{plan_tools}</p>" if plan_tools else "")
            + (
                f"<p><b>备注：</b>{_html_text(plan.get('notes') or '', refs=False)}</p>"
                if plan.get("notes")
                else ""
            )
        )
    interactions_html = ""
    interactions = getattr(record, "interactions", None) or []
    if interactions:
        rows = "".join(
            "<tr><td style='padding:5px;border-bottom:1px solid #eee;vertical-align:top;'>"
            f"{_html_text(item.get('question') or '', refs=False)}</td>"
            "<td style='padding:5px;border-bottom:1px solid #eee;vertical-align:top;'>"
            f"{_html_text(item.get('answer') or '', refs=False)}</td></tr>"
            for item in interactions
            if isinstance(item, dict)
        )
        interactions_html = (
            "<h3 style='color:#0b3d6e;border-bottom:1px solid #d8e3ef;padding-bottom:3px;margin-top:16px;'>"
            "用户补充信息</h3><table width='100%' cellspacing='0' cellpadding='0' style='border-collapse:collapse;'>"
            "<thead><tr><th align='left'>问题</th><th align='left'>回答</th></tr></thead><tbody>"
            + rows
            + "</tbody></table>"
        )
    return f"""
    <div style="font-family:'Segoe UI','Microsoft YaHei UI',sans-serif;color:#1b1b1b;">
      <div style="background:#f3f7fb;border:1px solid #d8e3ef;border-radius:6px;padding:10px 12px;margin-bottom:10px;">
        <div style="font-size:15pt;font-weight:600;color:#0b3d6e;">{headline}</div>
        <div style="color:#555;margin-top:4px;">目标：{target} · 类型：{target_kind} · 模式：{summary_label} · 模型：{model}</div>
        <div style="color:#555;">开始/结束：{date} · 用时：{elapsed_text} · 状态：{_html_text(status_label, refs=False)}</div>
        {f'<div style="color:#555;">问题：{issue}</div>' if record.issue else ''}
      </div>
      <p>{summary}</p>
      {plan_html}
      <h3 style="color:#0b3d6e;border-bottom:1px solid #d8e3ef;padding-bottom:3px;">事实发现</h3>
      <table width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;">{findings_html}</table>
      {causes_section}
      <h3 style="color:#0b3d6e;border-bottom:1px solid #d8e3ef;padding-bottom:3px;margin-top:16px;">下一步（未自动执行）</h3>
      <table width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;">{''.join(steps_rows)}</table>
      {actions_html}
      {interactions_html}
      <h3 style="color:#0b3d6e;border-bottom:1px solid #d8e3ef;padding-bottom:3px;margin-top:16px;">局限性与安全说明</h3>
      <ul style="margin-top:4px;">{limitations_html}</ul>
    </div>
    """


def render_report_html(report: dict[str, Any], record: DiagnosticRecord) -> str:
    return _report_body_html(report, record)


def render_export_html(record: DiagnosticRecord, report: dict[str, Any]) -> str:
    body = _report_body_html(report, record)
    evidence_sections: list[str] = []
    for item in record.evidence:
        detail = html.escape(json.dumps(item.get("data") or {}, ensure_ascii=False, indent=2))
        evidence_sections.append(
            f"<details style='margin:4px 0;'><summary><b>{html.escape(item.get('id', ''))}</b> "
            f"{html.escape(item.get('summary', ''))}</summary>"
            f"<pre style='white-space:pre-wrap;font-size:8.5pt;background:#f7f7f7;padding:8px;'>"
            f"{detail}</pre></details>"
        )
    steps_rows = "".join(
        f"<tr><td>{html.escape(str(step.get('sequence', '')))}</td>"
        f"<td>{html.escape(str(step.get('tool', '')))}</td>"
        f"<td>{html.escape('、'.join(step.get('basis') or []))}</td>"
        f"<td>{html.escape(str(step.get('evidence_id', '')))}</td>"
        f"<td>{'成功' if step.get('ok') else '失败'}</td></tr>"
        for step in record.tool_steps
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Windows Helper Plus 诊断报告 {html.escape(record.record_id)}</title>
<style>
body {{ font-family: 'Segoe UI', 'Microsoft YaHei UI', sans-serif; margin: 28px; color:#1b1b1b; }}
h1 {{ color:#0b3d6e; }}
table {{ border-collapse: collapse; width:100%; }}
td, th {{ border:1px solid #ddd; padding:5px 7px; text-align:left; vertical-align:top; }}
pre {{ white-space:pre-wrap; }}
code {{ background:#f3f3f3; padding:1px 3px; }}
@media print {{ details {{ display:block; }} }}
</style></head>
<body>
<h1>Windows Helper Plus 诊断报告</h1>
<p>记录 ID：{html.escape(record.record_id)} · 生成器：{html.escape(record.generator)}</p>
{body}
<h3>工具执行审计</h3>
<table><thead><tr><th>步骤</th><th>工具</th><th>依据</th><th>证据</th><th>结果</th></tr></thead>
<tbody>{steps_rows}</tbody></table>
<h3>原始证据</h3>
<p style="color:#666;">以下内容仅在本地导出。可能包含用户名、计算机名或应用路径等隐私信息，分享前请检查。</p>
{''.join(evidence_sections)}
</body></html>"""


def export_json(record: DiagnosticRecord, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def export_html(record: DiagnosticRecord, path: Path) -> Path:
    if not record.report:
        raise ValueError("诊断记录没有可导出的报告。")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_export_html(record, record.report), encoding="utf-8")
    return path


def default_report_name(record: DiagnosticRecord, suffix: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return f"HelperPlus-{stamp}{suffix}"
