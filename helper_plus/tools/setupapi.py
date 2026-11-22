"""只读工具：SetupAPI 设备安装日志摘要。"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from ..models import ToolResult
from ..safety import ensure_no_unknown_keys, require_int
from ..windows import truncate_text, windows_directory
from .base import BaseTool, ToolContext


# 只抽取真正有诊断价值的行：SetupAPI 用 "!" 前缀标记签名/安装问题。
# 不含单独的 0x...（正常 exit(0x00000000) 也会命中，噪声太大）。
_ERROR_PATTERN = re.compile(
    r"(^\s*!)|(\bfailed\b|\bfailure\b|\berror\b|\bcannot\b|\bcould not\b|"
    r"\btimeout\b|\bCR_[A-Z_]+\b|required reboot|not signed|hash mismatch|"
    r"问题|错误|失败)",
    re.IGNORECASE,
)


def _read_tail_text(path: Path, max_bytes: int = 2_500_000) -> tuple[str, int, bool]:
    size = path.stat().st_size
    start = max(0, size - max_bytes)
    with path.open("rb") as handle:
        handle.seek(start)
        data = handle.read()
    if start > 0:
        first_newline = data.find(b"\n")
        if first_newline >= 0:
            data = data[first_newline + 1 :]
    if data.startswith(b"\xff\xfe"):
        text = data.decode("utf-16-le", errors="replace").lstrip("\ufeff")
    elif data.startswith(b"\xef\xbb\xbf"):
        text = data.decode("utf-8-sig", errors="replace")
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
    return text, size, start > 0


def extract_setupapi_errors(text: str, max_lines: int) -> list[str]:
    matches: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _ERROR_PATTERN.search(stripped):
            matches.append(truncate_text(stripped, 500))
    return matches[-max_lines:]


class SetupApiTool(BaseTool):
    name = "get_setupapi_log"
    description = (
        "读取 Windows SetupAPI 设备安装日志（%SystemRoot%\\INF\\setupapi.dev.log 与 "
        "setupapi.app.log）尾部的错误/失败/超时行，用于排查驱动安装失败。只读取摘要行，"
        "不修改日志，不导出完整文件。"
    )
    source = "SetupAPI.dev.log / SetupAPI.app.log"
    native_command = "只读读取 %SystemRoot%\\INF\\setupapi*.log"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "max_lines": {
                "type": "integer",
                "minimum": 10,
                "maximum": 200,
                "default": 80,
                "description": "最多返回多少条匹配到的错误行。",
            }
        },
        "additionalProperties": False,
    }

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        ensure_no_unknown_keys(args, ["max_lines"], self.name)
        return {
            "max_lines": require_int(
                args.get("max_lines", 80), "max_lines", minimum=10, maximum=200
            )
        }

    def collect(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        inf_dir = windows_directory() / "INF"
        logs: list[dict[str, Any]] = []
        total_errors = 0
        for name in ("setupapi.dev.log", "setupapi.app.log"):
            path = inf_dir / name
            entry: dict[str, Any] = {
                "file": name,
                "path": str(path),
                "available": False,
                "size_bytes": None,
                "modified": None,
                "error_line_count": 0,
                "error_lines": [],
            }
            if not path.exists():
                logs.append(entry)
                continue
            try:
                stat = path.stat()
                entry["size_bytes"] = stat.st_size
                from datetime import datetime

                entry["modified"] = (
                    datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds")
                )
                text, size, truncated = _read_tail_text(path)
                errors = extract_setupapi_errors(text, int(args["max_lines"]))
                entry.update(
                    {
                        "available": True,
                        "size_bytes": size,
                        "tail_scanned": truncated,
                        "error_line_count": len(errors),
                        "error_lines": errors,
                    }
                )
                total_errors += len(errors)
            except OSError as exc:
                entry["error"] = f"读取失败：{exc}"
            logs.append(entry)
        available = [log for log in logs if log.get("available")]
        if not available:
            summary = "未找到可读取的 SetupAPI 日志文件（部分系统可能禁用设备安装日志）"
        else:
            summary = (
                f"SetupAPI 日志：读取 {len(available)} 个文件，"
                f"尾部匹配错误/失败行 {total_errors} 条"
            )
        data = {"logs": logs, "available_files": len(available), "matched_error_lines": total_errors}
        return ToolResult(
            ok=True,
            summary=summary,
            data=data,
            source=self.source,
            native_command=self.native_command,
            raw_excerpt="",
        )
