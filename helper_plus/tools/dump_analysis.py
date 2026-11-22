"""只读工具：使用 Windows Debugging Tools 离线分析崩溃转储（Dump）。"""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import re
import shutil
from typing import Any

from ..local_context import log_root
from ..models import ToolResult
from ..safety import SafetyError, ensure_no_unknown_keys, require_int, validate_inspect_path
from ..windows import program_data_directory, run_process, truncate_text
from .base import BaseTool, ToolContext

_DUMP_SUFFIXES = {".dmp", ".mdmp"}
_HEADER_RE = re.compile(r"^([A-Z][A-Z0-9_]+)\s+\(([0-9a-fA-F]+)\)\s*$", re.MULTILINE)
_STACK_STOP_RE = re.compile(r"^[A-Z][A-Z0-9_ ]{2,}:")
_FIELD_KEYS = (
    "BUGCHECK_CODE",
    "BUGCHECK_STR",
    "BUGCHECK_P1",
    "BUGCHECK_P2",
    "BUGCHECK_P3",
    "BUGCHECK_P4",
    "PROCESS_NAME",
    "MODULE_NAME",
    "IMAGE_NAME",
    "FAULTING_MODULE",
    "SYMBOL_NAME",
    "FAILURE_BUCKET_ID",
    "FAILURE_ID_HASH",
    "DEFAULT_BUCKET_ID",
    "PRIMARY_PROBLEM_CLASS",
    "OS_VERSION",
    "EXCEPTION_CODE",
    "MEMORY_CORRUPTION_LARGE",
)
_INSTALL_HINT = (
    "未找到 cdb.exe。请安装 Windows SDK 并勾选 “Debugging Tools for Windows”，"
    "或安装 WinDbg 后把 cdb.exe 加入 PATH；也可用环境变量 HELPER_PLUS_CDB 指定 cdb.exe 路径。"
)


def _optional_text(value: Any, field: str, *, max_len: int = 1500) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SafetyError(f"{field} 必须是字符串。")
    text = value.strip()
    if "\n" in text or "\r" in text:
        raise SafetyError(f"{field} 不能包含换行。")
    if len(text) > max_len:
        raise SafetyError(f"{field} 过长（最多 {max_len} 字符）。")
    return text


def find_cdb() -> Path | None:
    """查找 cdb.exe：环境变量、PATH、Windows SDK 安装目录。"""
    override = os.environ.get("HELPER_PLUS_CDB", "").strip()
    if override and Path(override).is_file():
        return Path(override)
    found = shutil.which("cdb.exe")
    if found:
        return Path(found)

    candidates: list[Path] = []
    for env in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432"):
        base = os.environ.get(env)
        if not base:
            continue
        kits = Path(base) / "Windows Kits" / "10" / "Debuggers"
        candidates.extend(
            [
                kits / "x64" / "cdb.exe",
                kits / "arm64" / "cdb.exe",
                kits / "x86" / "cdb.exe",
            ]
        )
        if kits.is_dir():
            try:
                candidates.extend(sorted(kits.glob("*/x64/cdb.exe"), reverse=True))
                candidates.extend(sorted(kits.glob("*/arm64/cdb.exe"), reverse=True))
            except OSError:
                pass
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows Kits\Installed Roots"
        ) as key:
            value, _ = winreg.QueryValueEx(key, "KitsRoot10")
        root = Path(str(value))
        candidates.extend(
            [
                root / "Debuggers" / "x64" / "cdb.exe",
                root / "Debuggers" / "arm64" / "cdb.exe",
            ]
        )
    except Exception:  # noqa: BLE001
        pass
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _dump_sources(target: Path | None) -> list[Path]:
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    sources: list[Path] = [
        windows / "MEMORY.DMP",
        windows / "Minidump",
        windows / "LiveKernelReports",
        program_data_directory() / "Microsoft" / "Windows" / "WER" / "ReportArchive",
        program_data_directory() / "Microsoft" / "Windows" / "WER" / "ReportQueue",
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        sources.append(Path(local) / "CrashDumps")
    if target is not None:
        sources.insert(0, target if target.is_dir() else target.parent)
    return sources


def _dump_info(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "name": path.name,
        "size_bytes": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime)
        .astimezone()
        .isoformat(timespec="seconds"),
    }


def collect_dump_candidates(
    target: Path | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """收集最近的可分析 dump；只扫描固定目录，避免全盘遍历。"""
    found: dict[str, dict[str, Any]] = {}
    for source in _dump_sources(target):
        if not source.exists():
            continue
        try:
            if source.is_file():
                paths = [source]
            else:
                paths = []
                for path in source.rglob("*"):
                    if len(paths) >= 500:
                        break
                    if path.is_file() and path.suffix.lower() in _DUMP_SUFFIXES:
                        paths.append(path)
        except OSError:
            continue
        for path in paths:
            if path.suffix.lower() not in _DUMP_SUFFIXES:
                continue
            try:
                info = _dump_info(path)
            except OSError:
                continue
            key = str(path).lower()
            if key in found:
                continue
            found[key] = info
    ordered = sorted(found.values(), key=lambda item: item["modified"], reverse=True)
    return ordered[: max(1, limit)]


def parse_analyze_output(text: str, max_stack_frames: int = 40) -> dict[str, Any]:
    """解析 cdb `!analyze -v` 的文本输出，提取 BugCheck/模块/栈。"""
    data: dict[str, Any] = {}
    for key in _FIELD_KEYS:
        match = re.search(rf"^\s*{key}:\s*(.+?)\s*$", text, re.MULTILINE | re.IGNORECASE)
        if match:
            data[key.lower()] = match.group(1).strip()

    header = _HEADER_RE.search(text)
    if header:
        data.setdefault("bugcheck_name", header.group(1))
        data.setdefault("bugcheck_code", header.group(2))
    if data.get("bugcheck_code"):
        data["bugcheck_code"] = data["bugcheck_code"].removeprefix("0x").lstrip("0") or "0"

    arguments: list[dict[str, str]] = []
    for index in range(1, 5):
        match = re.search(rf"^\s*Arg{index}:\s*([0-9a-fA-F]+),\s*(.+?)\s*$", text, re.MULTILINE)
        if match:
            arguments.append(
                {"index": index, "value": match.group(1), "description": match.group(2)}
            )
    if not arguments:
        for index in range(1, 5):
            value = data.get(f"bugcheck_p{index}")
            if value:
                arguments.append({"index": index, "value": value, "description": ""})
    data["bugcheck_arguments"] = arguments

    probable = re.search(r"Probably caused by\s*:\s*(.+?)\s*$", text, re.MULTILINE | re.IGNORECASE)
    if probable:
        data["probably_caused_by"] = probable.group(1).strip()
    if data.get("exception_code"):
        hex_match = re.search(r"0x[0-9a-fA-F]+", data["exception_code"])
        if hex_match:
            data["exception_code"] = hex_match.group(0)

    stack: list[str] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().upper().startswith("STACK_TEXT:"):
            for follow in lines[index + 1 :]:
                stripped = follow.strip()
                if not stripped:
                    if stack:
                        break
                    continue
                if _STACK_STOP_RE.match(stripped):
                    break
                stack.append(truncate_text(stripped, 400))
                if len(stack) >= max_stack_frames:
                    break
            break
    data["stack_text"] = stack

    head = text[:5000]
    if "User Mini Dump" in head or "This is a user dump" in text:
        data["dump_type"] = "user"
    elif "Bugcheck Analysis" in text or "Kernel" in head:
        data["dump_type"] = "kernel"
    else:
        data["dump_type"] = "unknown"
    data["whea_related"] = any(
        marker in text
        for marker in ("WHEA_ERROR", "WheaError", "Machine Check", "MACHINE_CHECK")
    )
    data["has_bugcheck"] = bool(
        data.get("bugcheck_code")
        or data.get("bugcheck_name")
        or data.get("exception_code")
        or data.get("probably_caused_by")
    )
    return data


class AnalyzeDumpTool(BaseTool):
    name = "analyze_dump"
    description = (
        "使用 Windows Debugging Tools 的 cdb.exe 离线分析 .dmp/.mdmp：执行 !analyze -v，"
        "提取 BugCheck 代码/名称/参数、故障模块、可能原因、调用栈、Failure Bucket、进程名，"
        "并支持自动发现最近的内核/应用 dump。只读，不 attach 运行中的内核、不修改系统。"
        "需要安装 Debugging Tools for Windows（cdb.exe）；未安装时返回候选 dump 与安装指引。"
    )
    source = "Windows Debugging Tools (cdb.exe) + 本地 dump 目录"
    native_command = "cdb.exe -z <dump> -y <symbols> -c \"!analyze -v; q\""
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "maxLength": 1024,
                "description": "可选 dump 文件绝对路径；省略时分析当前目标或最近的 dump。",
            },
            "max_stack_frames": {
                "type": "integer",
                "minimum": 5,
                "maximum": 100,
                "default": 40,
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 20,
                "maximum": 300,
                "default": 120,
            },
            "symbol_path": {
                "type": "string",
                "maxLength": 1500,
                "description": "可选符号路径；默认使用本地缓存 + Microsoft 公共符号服务器。",
            },
            "download_symbols": {"type": "boolean", "default": True},
        },
        "additionalProperties": False,
    }

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        ensure_no_unknown_keys(
            args,
            ["path", "max_stack_frames", "timeout_seconds", "symbol_path", "download_symbols"],
            self.name,
        )
        path = _optional_text(args.get("path"), "path", max_len=1024)
        symbol_path = _optional_text(args.get("symbol_path"), "symbol_path", max_len=1500)
        download = args.get("download_symbols", True)
        if not isinstance(download, bool):
            raise SafetyError("download_symbols 必须是布尔值。")
        return {
            "path": path,
            "max_stack_frames": require_int(
                args.get("max_stack_frames", 40),
                "max_stack_frames",
                minimum=5,
                maximum=100,
            ),
            "timeout_seconds": require_int(
                args.get("timeout_seconds", 120),
                "timeout_seconds",
                minimum=20,
                maximum=300,
            ),
            "symbol_path": symbol_path,
            "download_symbols": download,
        }

    def collect(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        candidates = collect_dump_candidates(ctx.target, limit=20)
        explicit = str(args.get("path") or "").strip()
        dump: Path | None = None
        if explicit:
            dump = validate_inspect_path(explicit, ctx.target if ctx.target and ctx.target.is_file() else None)
            if dump.suffix.lower() not in _DUMP_SUFFIXES:
                raise SafetyError(f"不是可分析的 dump 文件（需要 .dmp/.mdmp）：{dump}")
        elif ctx.target is not None and ctx.target.is_file() and ctx.target.suffix.lower() in _DUMP_SUFFIXES:
            dump = ctx.target
        elif candidates:
            dump = Path(str(candidates[0]["path"]))

        if dump is None:
            return ToolResult(
                ok=True,
                summary="未发现可分析的 dump（已检查 MEMORY.DMP、Minidump、LiveKernelReports、WER、CrashDumps）。",
                data={"analyzed": None, "candidates": candidates, "analyzer_available": find_cdb() is not None},
                source=self.source,
                native_command=self.native_command,
            )

        cdb = find_cdb()
        if cdb is None:
            return ToolResult(
                ok=False,
                summary=(
                    f"发现 {len(candidates)} 个候选 dump，但未找到 cdb.exe，无法解析。"
                    "请先安装 Debugging Tools for Windows。"
                ),
                data={
                    "analyzed": str(dump),
                    "candidates": candidates,
                    "analyzer_available": False,
                },
                source=self.source,
                native_command=self.native_command,
                error=_INSTALL_HINT,
            )

        cache = log_root() / "symbols"
        cache.mkdir(parents=True, exist_ok=True)
        symbol_path = str(args.get("symbol_path") or "")
        if not symbol_path:
            symbol_path = (
                f"srv*{cache}*https://msdl.microsoft.com/download/symbols"
                if args.get("download_symbols", True)
                else f"srv*{cache}"
            )
        command = [
            str(cdb),
            "-z",
            str(dump),
            "-y",
            symbol_path,
            "-c",
            "!analyze -v; q",
        ]
        try:
            proc = run_process(
                command,
                timeout=float(args["timeout_seconds"]),
                cancel_event=ctx.cancel_event,
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                ok=False,
                summary=f"启动 cdb 失败：{exc}",
                data={"analyzed": str(dump), "candidates": candidates, "analyzer_available": True},
                source=self.source,
                native_command=self.native_command,
                error=str(exc),
            )

        text = f"{proc.stdout or ''}\n{proc.stderr or ''}".strip()
        parsed = parse_analyze_output(text, max_stack_frames=int(args["max_stack_frames"]))
        warnings: list[str] = []
        if getattr(proc, "timed_out", False):
            warnings.append("cdb 分析超时（符号下载可能过慢）；以下为已返回的部分输出。")
        if getattr(proc, "cancelled", False):
            warnings.append("分析被用户取消。")
        if proc.returncode not in (0, None):
            warnings.append(f"cdb 退出码：{proc.returncode}")
        if not parsed.get("has_bugcheck"):
            return ToolResult(
                ok=False,
                summary=f"cdb 已运行，但未识别到 BugCheck/异常信息：{dump.name}",
                data={
                    "analyzed": str(dump),
                    "candidates": candidates,
                    "analyzer_available": True,
                    "parsed": parsed,
                    "warnings": warnings,
                    "returncode": proc.returncode,
                    "symbol_path": symbol_path,
                },
                source=self.source,
                native_command=self.native_command,
                raw_excerpt=text[-30000:],
                error="未识别到 BugCheck/异常信息；请确认 dump 类型与 cdb 架构匹配，并检查符号路径。",
            )

        bugcheck = ""
        if parsed.get("bugcheck_code"):
            bugcheck = f"BugCheck 0x{parsed['bugcheck_code']}"
            if parsed.get("bugcheck_name"):
                bugcheck += f"（{parsed['bugcheck_name']}）"
        elif parsed.get("exception_code"):
            bugcheck = f"异常 {parsed['exception_code']}"
        cause = parsed.get("probably_caused_by") or parsed.get("module_name") or parsed.get("image_name")
        summary = (
            f"Dump 分析：{dump.name}（{parsed.get('dump_type') or 'unknown'}）；"
            f"{bugcheck or '已提取异常信息'}"
        )
        if cause:
            summary += f"；可能原因 {cause}"
        stack = parsed.get("stack_text") or []
        if stack:
            summary += f"；栈 {len(stack)} 帧"
        return ToolResult(
            ok=True,
            summary=summary,
            data={
                "analyzed": str(dump),
                "candidates": candidates,
                "analyzer_available": True,
                "parsed": parsed,
                "warnings": warnings,
                "returncode": proc.returncode,
                "symbol_path": symbol_path,
                "command": " ".join(command),
            },
            source=self.source,
            native_command=self.native_command,
            raw_excerpt=text[-30000:],
            duration_ms=proc.duration_ms,
        )
