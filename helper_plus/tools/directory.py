"""只读工具：目录/驱动器内容清单与项目结构概览。"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any

from ..models import ToolResult
from ..safety import SafetyError, ensure_no_unknown_keys, require_int, validate_directory_path
from ..windows import truncate_text
from .base import BaseTool, ToolContext


class InspectDirectoryTool(BaseTool):
    name = "inspect_directory"
    description = (
        "只读列出当前诊断目标目录（文件夹/驱动器）的直接子项与摘要：文件数、目录数、"
        "总大小、扩展名分布、最近修改、隐藏/系统项、可执行/驱动类文件。"
        "用于项目目录、驱动包目录、盘符根目录的初步盘点；不读取文件内容。"
        "recursive=true 时可递归盘点子目录（受 max_entries 与内部扫描上限约束）。"
    )
    source = "Windows 文件系统只读枚举"
    native_command = "Directory enumeration (read-only)"
    timeout_seconds = 45.0
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "可选，仅允许当前诊断目录或其子目录；省略表示当前目标。",
                "maxLength": 1024,
            },
            "max_entries": {
                "type": "integer",
                "minimum": 10,
                "maximum": 500,
                "default": 120,
                "description": "最多列出多少个条目（递归时也受此上限约束）。",
            },
            "recursive": {
                "type": "boolean",
                "default": False,
                "description": "是否递归扫描子目录；用于项目/驱动目录的完整文件映射。",
            },
        },
        "additionalProperties": False,
    }

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        ensure_no_unknown_keys(args, ["path", "max_entries", "recursive"], self.name)
        path = args.get("path")
        if path is not None and not isinstance(path, str):
            raise SafetyError("path 必须是字符串。")
        recursive = args.get("recursive", False)
        if not isinstance(recursive, bool):
            raise SafetyError("recursive 必须是布尔值。")
        return {
            "path": path,
            "max_entries": require_int(args.get("max_entries", 120), "max_entries", minimum=10, maximum=500),
            "recursive": recursive,
        }

    def collect(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        root = validate_directory_path(args.get("path"), ctx.target)
        if ctx.cancel_event.is_set():
            raise RuntimeError("操作已取消。")
        max_entries = int(args["max_entries"])
        recursive = bool(args.get("recursive"))
        entries: list[dict[str, Any]] = []
        extensions: Counter[str] = Counter()
        file_count = 0
        dir_count = 0
        total_size = 0
        recent_cutoff = datetime.now(timezone.utc).timestamp() - 7 * 86400
        recent_count = 0
        scanned_dirs = 0
        truncated = False
        truncated_flag = [False]

        def _record(entry: os.DirEntry, relative: str, is_dir: bool, stat: os.stat_result) -> None:
            nonlocal file_count, dir_count, total_size, recent_count
            if is_dir:
                dir_count += 1
            else:
                file_count += 1
                total_size += stat.st_size
                suffix = Path(entry.name).suffix.lower() or "(无扩展名)"
                extensions[suffix] += 1
                if stat.st_mtime >= recent_cutoff:
                    recent_count += 1
            if len(entries) < max_entries:
                entries.append(
                    {
                        "name": relative,
                        "type": "directory" if is_dir else "file",
                        "size_bytes": None if is_dir else stat.st_size,
                        "modified": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
                        "attributes": "hidden" if entry.name.startswith(".") else "",
                    }
                )
            else:
                truncated_flag[0] = True

        if not recursive:
            try:
                iterator = os.scandir(root)
            except OSError as exc:
                raise RuntimeError(f"无法枚举目录：{exc}") from exc
            with iterator:
                for entry in iterator:
                    if ctx.cancel_event.is_set():
                        raise RuntimeError("操作已取消。")
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    _record(entry, entry.name, is_dir, stat)
        else:
            # 递归扫描：不跟随 symlink/junction，并限制目录总数，避免驱动器卡死。
            stack: list[tuple[Path, str]] = [(root, "")]
            max_dirs = 5000
            while stack and scanned_dirs < max_dirs and not truncated:
                current, prefix = stack.pop()
                scanned_dirs += 1
                try:
                    iterator = os.scandir(current)
                except OSError:
                    continue
                with iterator:
                    for entry in iterator:
                        if ctx.cancel_event.is_set():
                            raise RuntimeError("操作已取消。")
                        relative = f"{prefix}{entry.name}"
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                            stat = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        _record(entry, relative, is_dir, stat)
                        if is_dir:
                            stack.append((Path(entry.path), relative + "\\"))
                        if truncated:
                            break
            if scanned_dirs >= max_dirs or stack:
                truncated = True
        truncated = truncated or truncated_flag[0]
        entries.sort(key=lambda item: (item["type"] != "directory", item["name"].lower()))
        data = {
            "path": str(root),
            "drive": root.anchor or None,
            "recursive": recursive,
            "directories_scanned": scanned_dirs if recursive else 1,
            "file_count": file_count,
            "directory_count": dir_count,
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "recently_modified_7d": recent_count,
            "extensions": dict(extensions.most_common(20)),
            "entries": entries,
            "truncated": truncated,
        }
        ext_text = "、".join(f"{name}×{count}" for name, count in extensions.most_common(6)) or "无文件"
        summary = (
            f"目录 {root}：{file_count} 个文件、{dir_count} 个子目录，"
            f"合计 {data['total_size_mb']} MB；7 天内修改 {recent_count} 个；"
            f"主要类型：{ext_text}。"
            + (f"（递归扫描 {scanned_dirs} 个目录，结果已截断）" if recursive and truncated else "")
        )
        return ToolResult(
            ok=True,
            summary=summary,
            data=data,
            source=self.source,
            native_command=self.native_command,
            raw_excerpt=truncate_text(str(entries), 4000),
        )
