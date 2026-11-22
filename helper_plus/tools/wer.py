"""只读工具：WER（Windows 错误报告）元数据。"""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
from typing import Any

from ..models import ToolResult
from ..safety import ensure_no_unknown_keys, require_int
from ..windows import program_data_directory, truncate_text
from .base import BaseTool, ToolContext


_WER_FIELDS = (
    "EventType",
    "AppName",
    "AppPath",
    "ReportIdentifier",
    "ReportDescription",
    "Sig[0].Name",
    "Sig[0].Value",
    "Sig[1].Name",
    "Sig[1].Value",
    "Sig[2].Name",
    "Sig[2].Value",
    "DynamicSig[1].Name",
    "DynamicSig[1].Value",
)


def _decode_report(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith(b"\xff\xfe"):
        return data.decode("utf-16-le", errors="replace").lstrip("\ufeff")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-16-le", errors="replace")


def _parse_wer_text(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines()[:400]:
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in _WER_FIELDS:
            result[key] = truncate_text(value.strip(), 400)
    return result


def _report_dirs() -> list[Path]:
    roots: list[Path] = []
    for base in (
        program_data_directory() / "Microsoft" / "Windows" / "WER" / "ReportArchive",
        program_data_directory() / "Microsoft" / "Windows" / "WER" / "ReportQueue",
    ):
        if base.is_dir():
            roots.append(base)
    return roots


def collect_wer_reports(limit: int) -> tuple[list[dict[str, Any]], int]:
    candidates: list[tuple[float, Path, Path]] = []
    for root in _report_dirs():
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    report = Path(entry.path) / "Report.wer"
                    if not report.is_file():
                        continue
                    try:
                        mtime = entry.stat(follow_symlinks=False).st_mtime
                    except OSError:
                        mtime = 0.0
                    candidates.append((mtime, Path(entry.path), report))
        except OSError:
            continue
    candidates.sort(key=lambda item: item[0], reverse=True)
    reports: list[dict[str, Any]] = []
    for mtime, folder, report in candidates[:limit]:
        try:
            fields = _parse_wer_text(_decode_report(report))
        except OSError as exc:
            reports.append(
                {
                    "folder": folder.name,
                    "report_modified": datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="seconds")
                    if mtime
                    else None,
                    "error": f"无法读取 Report.wer：{exc}",
                }
            )
            continue
        reports.append(
            {
                "folder": folder.name,
                "report_modified": datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="seconds")
                if mtime
                else None,
                "event_type": fields.get("EventType"),
                "app_name": fields.get("AppName"),
                "app_path": fields.get("AppPath"),
                "report_identifier": fields.get("ReportIdentifier"),
                "signature": [
                    {
                        "name": fields.get(f"Sig[{index}].Name"),
                        "value": fields.get(f"Sig[{index}].Value"),
                    }
                    for index in range(3)
                    if fields.get(f"Sig[{index}].Name") or fields.get(f"Sig[{index}].Value")
                ],
            }
        )
    return reports, len(candidates)


class WerTool(BaseTool):
    name = "get_wer_reports"
    description = (
        "读取 Windows 错误报告（WER）目录中最近的崩溃/挂起报告元数据，"
        "包括 EventType、应用名称、AppPath 和签名键值。只读取元数据，不打开转储文件。"
    )
    source = "WER ReportArchive / ReportQueue"
    native_command = "只读读取 %ProgramData%\\Microsoft\\Windows\\WER 下的 Report.wer"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": 8,
                "description": "最多返回多少份最近报告。",
            }
        },
        "additionalProperties": False,
    }

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        ensure_no_unknown_keys(args, ["limit"], self.name)
        return {"limit": require_int(args.get("limit", 8), "limit", minimum=1, maximum=20)}

    def collect(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        reports, total = collect_wer_reports(int(args["limit"]))
        if reports:
            app_names = []
            for report in reports:
                name = report.get("app_name") or report.get("folder") or "未知应用"
                if name not in app_names:
                    app_names.append(str(name))
            summary = (
                f"WER 最近报告：可读取 {len(reports)} 份（候选 {total} 份）；"
                f"涉及应用：{'、'.join(app_names[:4])}"
            )
        else:
            summary = "WER 报告目录中没有可读取的崩溃/挂起报告"
        data = {"reports": reports, "count": len(reports), "candidate_count": total}
        return ToolResult(
            ok=True,
            summary=summary,
            data=data,
            source=self.source,
            native_command=self.native_command,
            raw_excerpt="",
        )
