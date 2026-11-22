r"""把应用诊断日志/记录导出到项目根目录的 Log 文件夹。

用法（项目根目录）：
    .\.venv\Scripts\python.exe scripts\collect_logs.py
    .\.venv\Scripts\python.exe scripts\collect_logs.py --data-dir "D:\custom\WindowsHelperPlus"

导出内容：
- Log/audit/      原始 JSONL 审计日志（不含 API Key）
- Log/reports/    完整 JSON 诊断记录
- Log/latest/     最近一次运行的审计与记录副本
- Log/README.md   文件索引与安全提示
- Log/latest-summary.md  最近一次运行的人类可读摘要

原始日志不会被修改或删除。
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "WindowsHelperPlus"


def _fmt_time(seconds: float) -> str:
    total = int(seconds or 0)
    return f"{total // 60:02d}:{total % 60:02d}"


def _safe_name(value: object) -> str:
    text = str(value or "unknown")
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in text)[:120]


def _excerpt(text: object, limit: int = 3000) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[:limit] + "\n…（已截断）"


def _latest_files(data_dir: Path) -> tuple[Path | None, Path | None]:
    report_dir = data_dir / "reports"
    audit_dir = data_dir / "logs"
    reports = sorted(report_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    audits = sorted(audit_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return (reports[0] if reports else None, audits[0] if audits else None)


def _load_record(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def _render_latest_summary(record: dict, report_path: Path, audit_path: Path | None) -> str:
    lines: list[str] = []
    add = lines.append
    add("# 最新一次诊断摘要")
    add("")
    add(f"- 记录 ID：`{record.get('record_id') or report_path.stem}`")
    add(f"- 目标：`{record.get('target') or '本机系统'}`")
    add(f"- 类型：{record.get('target_kind') or 'system'}")
    add(f"- 状态：{record.get('status') or 'unknown'}")
    add(f"- 模型：{record.get('model') or ''} / {record.get('reasoning_effort') or ''}")
    add(f"- 开始：{record.get('started_at') or ''}")
    add(f"- 结束：{record.get('finished_at') or ''}")
    add(f"- 用时：{_fmt_time(record.get('elapsed_seconds') or 0)}")
    add(f"- 证据数：{len(record.get('evidence') or [])}；步骤数：{len(record.get('tool_steps') or [])}；动作数：{len(record.get('actions') or [])}")
    add(f"- 记录文件：`{report_path}`")
    if audit_path:
        add(f"- 审计文件：`{audit_path}`")
    plan = record.get("plan")
    if isinstance(plan, dict):
        add("")
        add("## 执行计划")
        add("")
        add(f"**目标：** {plan.get('goal') or ''}")
        add("")
        for index, step in enumerate(plan.get("steps") or [], 1):
            add(f"{index}. {step}")
        if plan.get("tools"):
            add("")
            add("工具：" + "、".join(str(item) for item in plan.get("tools") or []))
        if plan.get("notes"):
            add("")
            add(f"备注：{plan.get('notes')}")
    report = record.get("report")
    if isinstance(report, dict):
        add("")
        add("## 结论")
        add("")
        add(f"**{report.get('headline') or ''}**")
        add("")
        add(str(report.get("summary") or ""))
        findings = report.get("findings") or []
        if findings:
            add("")
            add("### 事实发现")
            for item in findings:
                add(
                    f"- {item.get('text') or ''} "
                    f"（证据：{'、'.join(item.get('evidence') or [])}；置信度 {item.get('confidence')}）"
                )
        causes = report.get("candidate_causes") or []
        if causes:
            add("")
            add("### 候选原因")
            for item in causes:
                add(
                    f"- {item.get('cause') or ''} "
                    f"（证据：{'、'.join(item.get('evidence') or [])}；置信度 {item.get('confidence')}）"
                )
        steps = report.get("next_steps") or []
        if steps:
            add("")
            add("### 下一步")
            for item in steps:
                add(f"- {item.get('title') or ''}（风险：{item.get('risk') or ''}）")
        limitations = report.get("limitations") or []
        if limitations:
            add("")
            add("### 限制")
            for item in limitations:
                add(f"- {item}")
    actions = record.get("actions") or []
    if actions:
        add("")
        add("## 已确认动作")
        add("")
        add("| 动作 | 命令 | 结果 | 退出码 | 提权 | 用时 |")
        add("| --- | --- | --- | --- | --- | --- |")
        for item in actions:
            if item.get("approved") is False:
                add("| 用户拒绝的提案 | — | 未执行 | — | — | — |")
                continue
            action = item.get("action") if isinstance(item.get("action"), dict) else {}
            command = str(item.get("command") or action.get("command") or "").replace("|", "\\|")
            add(
                f"| {str(action.get('title') or '动作').replace('|', '\\|')} | `{command}` | "
                f"{'成功' if item.get('ok') else '失败'} | {item.get('returncode')} | "
                f"{'是' if item.get('elevated') else '否'} | {item.get('duration_ms') or 0} ms |"
            )
    evidence = record.get("evidence") or []
    if evidence:
        add("")
        add("## 证据索引")
        add("")
        add("| ID | 工具 | 结果 | 摘要 |")
        add("| --- | --- | --- | --- |")
        for item in evidence:
            summary = str(item.get("summary") or "").replace("|", "\\|").replace("\n", " ")
            add(f"| {item.get('id')} | {item.get('tool')} | {'成功' if item.get('ok') else '失败'} | {summary[:300]} |")
    steps = record.get("tool_steps") or []
    if steps:
        add("")
        add("## 步骤时间线")
        add("")
        add("| 序号 | 工具 | 依据 | 证据 | 结果 | 耗时 |")
        add("| --- | --- | --- | --- | --- | --- |")
        for item in steps:
            add(
                f"| {item.get('sequence')} | {item.get('tool')} | "
                f"{'、'.join(item.get('basis') or [])} | {item.get('evidence_id')} | "
                f"{'成功' if item.get('ok') else '失败'} | {item.get('duration_ms') or 0} ms |"
            )
    errors = record.get("errors") or []
    if errors:
        add("")
        add("## 错误")
        for item in errors:
            add(f"- {item}")
    add("")
    add("---")
    add("本摘要由 `scripts/collect_logs.py` 从应用诊断记录生成；原始审计/记录在 `Log/audit/` 与 `Log/reports/`。")
    return "\n".join(lines) + "\n"


def _render_readme(
    log_dir: Path,
    data_dir: Path,
    audit_count: int,
    report_count: int,
    latest: str | None,
    runs: list[dict],
) -> str:
    lines = [
        "# Windows Helper Plus 日志导出",
        "",
        f"- 导出时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"- 原始数据目录：`{data_dir}`",
        f"- 审计日志：`Log/audit/`（{audit_count} 个 JSONL）",
        f"- 完整诊断记录：`Log/reports/`（{report_count} 个 JSON）",
        f"- 最近一次摘要：`Log/latest-summary.md`" + (f"（{latest}）" if latest else ""),
        "",
        "## 运行索引（最新在前）",
        "",
        "| 记录 | 状态 | 目标 | 开始时间 | 用时 | 证据 | 动作 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for run in runs:
        target = str(run.get("target") or "本机系统").replace("|", "\\|")
        lines.append(
            f"| `{run.get('record_id') or ''}` | {run.get('status') or ''} | {target[:120]} | "
            f"{run.get('started_at') or ''} | {_fmt_time(run.get('elapsed_seconds') or 0)} | "
            f"{len(run.get('evidence') or [])} | {len(run.get('actions') or [])} |"
        )
    if not runs:
        lines.append("| — | — | — | — | — | — | — |")
    lines.extend(
        [
            "",
            "## 目录结构",
            "",
            "```text",
            "Log/",
            "  README.md              本说明与运行索引",
            "  latest-summary.md      最近一次诊断的人类可读摘要",
            "  audit/                 原始 JSONL 审计日志",
            "  reports/               完整 JSON 诊断记录（含证据、Planner、动作）",
            "  latest/                最近一次运行的审计/记录副本",
            "  runs/                  本地 Qwen 翻译的每次运行 Markdown",
            "  病历/                  完整病历（Markdown + JSON + index.jsonl，只追加）",
            "  local-model/           本地 llama-server 运行日志",
            "```",
            "",
            "## 安全提示",
            "",
            "- 日志可能包含计算机名、用户名、文件路径、设备/打印机名称、命令输出等本机信息；分享前请自行检查并脱敏。",
            "- 本导出**不包含** `credentials.bin`、API Key、系统提示词或完整文件内容。",
            "- 日志是应用运行记录，不会上传；如需提交技术支持，请先确认隐私信息。",
            "",
            "## 刷新方式",
            "",
            "```powershell",
            r".\.venv\Scripts\python.exe scripts\collect_logs.py",
            "```",
            "",
            "该脚本只复制/生成文件，不会修改或删除原始日志。",
        ]
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="导出 Windows Helper Plus 日志到 Log 文件夹")
    parser.add_argument("--data-dir", help="应用数据目录；默认 %%LOCALAPPDATA%%\\WindowsHelperPlus")
    parser.add_argument("--out", default=str(ROOT / "Log"), help="输出目录；默认项目根目录下的 Log")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir) if args.data_dir else DEFAULT_DATA_DIR
    log_dir = Path(args.out)
    if not data_dir.exists():
        print(f"数据目录不存在：{data_dir}", file=sys.stderr)
        return 2
    audit_src = data_dir / "logs"
    report_src = data_dir / "reports"
    log_dir.mkdir(parents=True, exist_ok=True)
    audit_out = log_dir / "audit"
    report_out = log_dir / "reports"
    latest_out = log_dir / "latest"
    for path in (audit_out, report_out, latest_out):
        path.mkdir(parents=True, exist_ok=True)
    audit_files = sorted(audit_src.glob("*.jsonl")) if audit_src.exists() else []
    report_files = sorted(report_src.glob("*.json")) if report_src.exists() else []
    for source in audit_files:
        shutil.copy2(source, audit_out / source.name)
    for source in report_files:
        shutil.copy2(source, report_out / source.name)
    latest_report, latest_audit = _latest_files(data_dir)
    latest_id = latest_report.stem if latest_report else None
    if latest_report:
        shutil.copy2(latest_report, latest_out / latest_report.name)
    if latest_audit:
        shutil.copy2(latest_audit, latest_out / latest_audit.name)
    if latest_report:
        record = _load_record(latest_report)
        summary = _render_latest_summary(record, latest_report, latest_audit)
        (log_dir / "latest-summary.md").write_text(summary, encoding="utf-8")
    else:
        (log_dir / "latest-summary.md").write_text("# 暂无诊断记录\n", encoding="utf-8")
    runs: list[dict] = []
    for source in reversed(report_files):
        try:
            record = _load_record(source)
        except (OSError, json.JSONDecodeError):
            continue
        runs.append(
            {
                "record_id": record.get("record_id") or source.stem,
                "status": record.get("status"),
                "target": record.get("target"),
                "started_at": record.get("started_at"),
                "elapsed_seconds": record.get("elapsed_seconds"),
                "evidence": record.get("evidence"),
                "actions": record.get("actions"),
            }
        )
    (log_dir / "README.md").write_text(
        _render_readme(log_dir, data_dir, len(audit_files), len(report_files), latest_id, runs),
        encoding="utf-8",
    )
    print(f"已导出到：{log_dir}")
    print(f"审计日志：{len(audit_files)}；诊断记录：{len(report_files)}")
    if latest_id:
        print(f"最新记录：{latest_id}")
    print(f"摘要：{log_dir / 'latest-summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
