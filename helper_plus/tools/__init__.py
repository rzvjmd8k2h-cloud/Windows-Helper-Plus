"""Windows 原生只读取证工具注册表（Planner 的唯一工具白名单）。"""

from __future__ import annotations

from typing import Any

from ..models import ToolResult
from ..safety import SafetyError
from .base import BaseTool, ToolContext, describe_args
from .device_printer import DeviceInfoTool, PrinterInfoTool
from .directory import InspectDirectoryTool
from .drivers import DriverInfoTool
from .driver_map import MapDriverFilesTool
from .dump_analysis import AnalyzeDumpTool
from .event_logs import EventLogTool
from .extended import ExtendedDiagnosticsTool
from .file_inspect import InspectFileTool
from .network import NetworkInfoTool
from .project_manifests import InspectProjectManifestsTool
from .reliability import ReliabilityTool
from .sandbox_control import SandboxControlTool
from .setupapi import SetupApiTool
from .system_checks import (
    DismScanTool,
    PnPUtilEnumTool,
    SfcVerifyTool,
    SignToolVerifyTool,
    WevtutilQueryTool,
)
from .system_info import SystemInfoTool
from .updates import WindowsUpdatesTool
from .web import WebFetchTool, WebSearchTool
from .wer import WerTool


class ToolNotFoundError(SafetyError):
    pass


class ToolRegistry:
    """只读工具集合。

    所有工具都被标记为 read-only；registry 没有执行任意命令的入口。
    真正的修复由用户确认后的 Action Executor（core/actions.py）执行，不在此注册。
    后续阶段的修复动作不应加入本 registry，而应进入独立、需确认与提权的
    Action Broker。
    """

    def __init__(self) -> None:
        classes: tuple[type[BaseTool], ...] = (
            SystemInfoTool,
            InspectFileTool,
            InspectDirectoryTool,
            InspectProjectManifestsTool,
            ExtendedDiagnosticsTool,
            EventLogTool,
            ReliabilityTool,
            DriverInfoTool,
            MapDriverFilesTool,
            AnalyzeDumpTool,
            WerTool,
            SetupApiTool,
            WindowsUpdatesTool,
            DeviceInfoTool,
            PrinterInfoTool,
            NetworkInfoTool,
            SfcVerifyTool,
            DismScanTool,
            PnPUtilEnumTool,
            SignToolVerifyTool,
            WevtutilQueryTool,
            SandboxControlTool,
            WebSearchTool,
            WebFetchTool,
        )
        self._tools: dict[str, BaseTool] = {cls.name: cls() for cls in classes}
        if len(self._tools) != len(classes):
            raise RuntimeError("工具名重复。")
        for tool in self._tools.values():
            if tool.risk != "read-only":
                raise RuntimeError(f"工具注册层只允许只读工具：{tool.name}")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def get(self, name: str) -> BaseTool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(
                f"工具 {name!r} 不在白名单中。允许的工具：{', '.join(self.names)}"
            )
        return tool

    def catalog(self) -> list[dict[str, Any]]:
        return [tool.prompt_entry() for tool in self._tools.values()]

    def validate_call(self, name: str, args: Any) -> dict[str, Any]:
        tool = self.get(name)
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise SafetyError("arguments 必须是 JSON 对象。")
        return tool.validate(args)

    def invoke(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        tool = self.get(name)
        return tool.invoke(args, ctx)

    def describe_call(self, name: str, args: dict[str, Any]) -> str:
        return f"{name} {describe_args(args)}"


def create_registry() -> ToolRegistry:
    return ToolRegistry()


__all__ = [
    "BaseTool",
    "ToolContext",
    "ToolNotFoundError",
    "ToolRegistry",
    "create_registry",
]
