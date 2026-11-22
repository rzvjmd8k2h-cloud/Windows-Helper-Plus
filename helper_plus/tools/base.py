"""工具基类与执行上下文。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import json
import threading
import time
from pathlib import Path
from typing import Any, ClassVar

from ..config import AppConfig
from ..models import ToolResult
from ..safety import SafetyError
from ..windows import NativeCommandError


@dataclass
class ToolContext:
    target: Path | None
    config: AppConfig
    cancel_event: threading.Event
    api_key: str = ""
    target_spec: Any = None

    @property
    def target_display(self) -> str | None:
        return str(self.target) if self.target else None

    @property
    def target_kind(self) -> str:
        return getattr(self.target_spec, "kind", "file" if self.target else "system")


class BaseTool(ABC):
    """只读工具基类。

    - ``validate`` 必须拒绝所有未知字段，限制枚举/数字范围；
    - ``collect`` 返回结构化证据，不允许修改系统；
    - 超时等暂时性故障由基类重试一次（符合架构文档的错误处理策略）。
    """

    name: ClassVar[str]
    description: ClassVar[str]
    parameters: ClassVar[dict[str, Any]]
    source: ClassVar[str] = "Windows 原生接口"
    native_command: ClassVar[str] = ""
    risk: ClassVar[str] = "read-only"
    requires_confirmation: ClassVar[bool] = False
    # 信任模式默认放行工具调用；少数会打开窗口/启动进程/写副本的工具必须单独标记。
    requires_confirmation_in_trusted: ClassVar[bool] = False
    confirmation_reason: ClassVar[str] = ""
    timeout_seconds: ClassVar[float] = 40.0

    @abstractmethod
    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def collect(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise NotImplementedError

    def invoke(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        started = time.monotonic()
        try:
            if not isinstance(args, dict):
                raise SafetyError("arguments 必须是 JSON 对象。")
            normalized = self.validate(args)
        except SafetyError as exc:
            return ToolResult.failure(
                "参数未通过安全校验",
                str(exc),
                source=self.source,
                native_command=self.native_command,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        result: ToolResult | None = None
        for attempt in (1, 2):
            if ctx.cancel_event.is_set():
                return ToolResult.failure(
                    "操作已取消",
                    "用户取消了诊断。",
                    source=self.source,
                    native_command=self.native_command,
                )
            try:
                result = self.collect(normalized, ctx)
            except NativeCommandError as exc:
                transient = "超时" in str(exc)
                result = ToolResult.failure(
                    "原生命令执行失败",
                    str(exc),
                    source=self.source,
                    native_command=self.native_command,
                    transient=transient,
                )
            except Exception as exc:  # noqa: BLE001 - 工具边界必须兜底
                result = ToolResult.failure(
                    "工具执行异常",
                    f"{type(exc).__name__}: {exc}",
                    source=self.source,
                    native_command=self.native_command,
                )
            result.attempts = attempt
            if result.ok or not result.transient or attempt == 2 or ctx.cancel_event.is_set():
                break
            time.sleep(0.4)
        assert result is not None
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result

    def public_parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": self.parameters.get("properties", {}),
            "additionalProperties": False,
            **({"required": self.parameters["required"]} if "required" in self.parameters else {}),
        }

    def prompt_entry(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "read_only": True,
            "risk": self.risk,
            "requires_confirmation": self.requires_confirmation,
            "confirmation_reason": self.confirmation_reason or None,
            "parameters": self.public_parameters(),
        }


def describe_args(args: dict[str, Any]) -> str:
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(args)
