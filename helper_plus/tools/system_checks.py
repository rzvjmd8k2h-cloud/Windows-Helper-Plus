"""只读系统检查工具：SFC / DISM / PnPUtil / SignTool / wevtutil。

SFC/DISM 虽然是“检查”命令，但耗时长且可能触发系统组件修复/占用，
因此标记 ``requires_confirmation=True``，由 UI 在执行前明确确认。
"""

from __future__ import annotations

from pathlib import Path
import re
import xml.etree.ElementTree as ET
from typing import Any

from ..models import ToolResult
from ..safety import SafetyError, ensure_no_unknown_keys, require_choice, require_int, validate_inspect_path
from ..windows import run_process, truncate_text
from .base import BaseTool, ToolContext


class SfcVerifyTool(BaseTool):
    name = "run_sfc_verify"
    description = (
        "运行 SFC 的只读完整性检查：sfc /verifyonly。该命令需要管理员权限，"
        "耗时长，会在执行前请求用户确认；不修复文件。"
        "输出用于确认系统文件是否损坏；详细内容在 CBS.log。"
    )
    source = "SFC /verifyonly"
    native_command = "sfc.exe /verifyonly"
    timeout_seconds = 1800.0
    requires_confirmation = True
    confirmation_reason = "SFC 扫描需要管理员权限且可能持续数分钟，会占用系统资源。"
    parameters: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        ensure_no_unknown_keys(args, [], self.name)
        return {}

    def collect(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        result = run_process(
            ["sfc.exe", "/verifyonly"],
            timeout=self.timeout_seconds,
            cancel_event=ctx.cancel_event,
        )
        output = truncate_text(result.stdout + "\n" + result.stderr, 12000)
        summary = "SFC /verifyonly 完成" if result.returncode == 0 else "SFC 检查失败/无结果"
        if "administrator" in output.lower() or "管理员" in output:
            summary = "SFC 需要管理员权限，当前进程无法执行"
        data = {
            "returncode": result.returncode,
            "findings": [line for line in output.splitlines() if any(k in line for k in ("修复", "损坏", "repair", "corrupt", "未找到", "found"))][:20],
        }
        return ToolResult(
            ok=result.returncode == 0,
            summary=summary,
            data=data,
            source=self.source,
            native_command=self.native_command,
            error=None if result.returncode == 0 else truncate_text(result.stderr, 1000),
            raw_excerpt=output,
        )


class DismScanTool(BaseTool):
    name = "run_dism_scan"
    description = (
        "运行 DISM 组件存储检查：/ScanHealth 或 /CheckHealth（只读，不执行 RestoreHealth）。"
        "需要管理员权限，执行前请求用户确认。"
    )
    source = "DISM"
    native_command = "Dism.exe /Online /Cleanup-Image /ScanHealth"
    timeout_seconds = 1800.0
    requires_confirmation = True
    confirmation_reason = "DISM ScanHealth/CheckHealth 需要管理员权限，可能持续数分钟。"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["ScanHealth", "CheckHealth"],
                "default": "ScanHealth",
                "description": "检查模式；不执行修复。",
            }
        },
        "additionalProperties": False,
    }

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        ensure_no_unknown_keys(args, ["mode"], self.name)
        return {"mode": require_choice(args.get("mode", "ScanHealth"), ["ScanHealth", "CheckHealth"], "mode")}

    def collect(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        result = run_process(
            ["Dism.exe", "/Online", "/Cleanup-Image", f"/{args['mode']}"],
            timeout=self.timeout_seconds,
            cancel_event=ctx.cancel_event,
        )
        output = truncate_text(result.stdout + "\n" + result.stderr, 12000)
        summary = f"DISM {args['mode']} 完成" if result.returncode == 0 else f"DISM {args['mode']} 失败"
        if "administrator" in output.lower() or "管理员" in output:
            summary = "DISM 需要管理员权限，当前进程无法执行"
        data = {
            "mode": args["mode"],
            "returncode": result.returncode,
            "lines": [line.strip() for line in output.splitlines() if line.strip()][-30:],
        }
        return ToolResult(
            ok=result.returncode == 0,
            summary=summary,
            data=data,
            source=self.source,
            native_command=f"Dism.exe /Online /Cleanup-Image /{args['mode']}",
            error=None if result.returncode == 0 else truncate_text(result.stderr, 1000),
            raw_excerpt=output,
        )


def _parse_pnputil_blocks(text: str) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                blocks.append(current)
                current = {}
            continue
        lowered = line.lower()
        if ":" in line and any(
            key in lowered
            for key in (
                "published name",
                "发布名称",
                "original name",
                "原始名称",
                "driver package provider",
                "驱动程序包提供程序",
                "提供程序名称",
                "class name",
                "类名称",
                "class guid",
                "类 guid",
                "driver version",
                "驱动程序版本",
                "signer name",
                "签名者名称",
            )
        ):
            key, _, value = line.partition(":")
            current[key.strip()] = value.strip()
    if current:
        blocks.append(current)
    return blocks


class PnPUtilEnumTool(BaseTool):
    name = "enum_drivers_pnputil"
    description = (
        "使用 PnPUtil /enum-drivers 只读枚举驱动仓库：发布名称、原始 INF、类、驱动版本、签名者。"
        "用于驱动包发现与映射；不安装/删除驱动。"
    )
    source = "PnPUtil"
    native_command = "pnputil.exe /enum-drivers"
    timeout_seconds = 90.0
    parameters: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        ensure_no_unknown_keys(args, [], self.name)
        return {}

    def collect(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        result = run_process(["pnputil.exe", "/enum-drivers"], timeout=self.timeout_seconds, cancel_event=ctx.cancel_event)
        if result.returncode != 0:
            raise RuntimeError(truncate_text(result.stderr or result.stdout, 1000))
        blocks = _parse_pnputil_blocks(result.stdout)
        data = {"count": len(blocks), "drivers": blocks[:100], "truncated": len(blocks) > 100}
        return ToolResult(
            ok=True,
            summary=f"PnPUtil 驱动仓库：{len(blocks)} 个第三方驱动包",
            data=data,
            source=self.source,
            native_command=self.native_command,
            raw_excerpt=truncate_text(result.stdout, 5000),
        )


def _find_signtool() -> Path | None:
    roots = [Path(r"C:\Program Files (x86)\Windows Kits\10\bin"), Path(r"C:\Program Files\Windows Kits\10\bin")]
    for root in roots:
        if not root.is_dir():
            continue
        candidates = sorted(root.glob("*/x64/signtool.exe"), reverse=True)
        if candidates:
            return candidates[0]
    return None


class SignToolVerifyTool(BaseTool):
    name = "verify_signature_signtool"
    description = (
        "使用 Windows SDK SignTool 的 verify /pa 对文件做 Authenticode 签名验证，"
        "包含证书链、时间戳等原始输出；若未安装 SignTool 会明确失败。只读。"
    )
    source = "SignTool"
    native_command = "signtool verify /pa /v <file>"
    timeout_seconds = 90.0
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径；省略使用当前文件目标。",
                "maxLength": 1024,
            }
        },
        "additionalProperties": False,
    }

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        ensure_no_unknown_keys(args, ["path"], self.name)
        path = args.get("path")
        if path is not None and not isinstance(path, str):
            raise SafetyError("path 必须是字符串。")
        return {"path": path}

    def collect(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = validate_inspect_path(args.get("path"), ctx.target)
        signtool = _find_signtool()
        if not signtool:
            raise RuntimeError("未找到 SignTool（Windows SDK）。可使用 inspect_file 的 Authenticode 验证作为替代。")
        result = run_process(
            [str(signtool), "verify", "/pa", "/v", str(path)],
            timeout=self.timeout_seconds,
            cancel_event=ctx.cancel_event,
        )
        output = truncate_text(result.stdout + "\n" + result.stderr, 8000)
        return ToolResult(
            ok=result.returncode == 0,
            summary=f"SignTool {'验证通过' if result.returncode == 0 else '验证失败'}：{path.name}",
            data={"path": str(path), "returncode": result.returncode, "signtool": str(signtool)},
            source=self.source,
            native_command=self.native_command,
            error=None if result.returncode == 0 else truncate_text(result.stderr, 1000),
            raw_excerpt=output,
        )


_EVENT_NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}


class WevtutilQueryTool(BaseTool):
    name = "wevtutil_query"
    description = (
        "使用 wevtutil 查询指定 Windows 事件日志的最近 Critical/Error/Warning 事件（XML 解析），"
        "用于需要 wevtutil 原生输出的场景。只读。"
    )
    source = "wevtutil"
    native_command = "wevtutil qe <log> /q:<xpath> /c:<n> /rd:true /f:xml"
    timeout_seconds = 75.0
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "log": {
                "type": "string",
                "enum": ["System", "Application", "Setup", "Security"],
                "default": "System",
            },
            "max_events": {"type": "integer", "minimum": 1, "maximum": 100, "default": 15},
            "level_max": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
        },
        "additionalProperties": False,
    }

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        ensure_no_unknown_keys(args, ["log", "max_events", "level_max"], self.name)
        return {
            "log": require_choice(args.get("log", "System"), ["System", "Application", "Setup", "Security"], "log"),
            "max_events": require_int(args.get("max_events", 15), "max_events", minimum=1, maximum=100),
            "level_max": require_int(args.get("level_max", 3), "level_max", minimum=1, maximum=5),
        }

    def collect(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        xpath = f"*[System[(Level<={int(args['level_max'])})]]"
        result = run_process(
            [
                "wevtutil.exe",
                "qe",
                str(args["log"]),
                f"/q:{xpath}",
                f"/c:{int(args['max_events'])}",
                "/rd:true",
                "/f:xml",
            ],
            timeout=self.timeout_seconds,
            cancel_event=ctx.cancel_event,
        )
        if result.returncode != 0:
            raise RuntimeError(truncate_text(result.stderr or result.stdout, 1000))
        events: list[dict[str, Any]] = []
        text = result.stdout.strip()
        # wevtutil 输出多个 XML 文档；逐个尝试解析。
        for chunk in re.split(r"(?=<\?xml|<Events|<Event\b)", text):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                root = ET.fromstring(chunk)
            except ET.ParseError:
                continue
            for event in root.iter():
                if not event.tag.endswith("Event"):
                    continue
                system = event.find("e:System", _EVENT_NS)
                if system is None:
                    continue
                event_id = system.findtext("e:EventID", default="", namespaces=_EVENT_NS)
                provider = system.find("e:Provider", _EVENT_NS)
                level = system.findtext("e:Level", default="", namespaces=_EVENT_NS)
                time_node = system.find("e:TimeCreated", _EVENT_NS)
                events.append(
                    {
                        "event_id": event_id,
                        "provider": provider.get("Name") if provider is not None else None,
                        "level": level,
                        "time_created": time_node.get("SystemTime") if time_node is not None else None,
                    }
                )
        summary = f"wevtutil 查询 {args['log']}：解析到 {len(events)} 条事件"
        return ToolResult(
            ok=True,
            summary=summary,
            data={"log": args["log"], "events": events[: int(args["max_events"])]},
            source=self.source,
            native_command=self.native_command,
            raw_excerpt=truncate_text(text, 5000),
        )
