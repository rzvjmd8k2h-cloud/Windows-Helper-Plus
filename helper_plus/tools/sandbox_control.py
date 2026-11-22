"""沙箱兜底工具：状态/规划/执行 Windows Sandbox 隔离采集。"""

from __future__ import annotations

import threading
from typing import Any

from ..models import ToolResult
from ..safety import ensure_no_unknown_keys, require_int, require_text
from ..sandbox import SandboxController, build_sandbox_plan
from .base import BaseTool, ToolContext


class SandboxControlTool(BaseTool):
    name = "sandbox_control"
    description = (
        "沙箱兜底（最后手段）：mode=status 检测 Windows Sandbox/Hyper-V/VM；"
        "mode=plan 只生成策略（不复制目标）；mode=run 复制目标到暂存区、启动 Windows Sandbox、"
        "在沙箱内运行可执行目标、等待报告并校验宿主未变化。"
        "run 会打开沙箱窗口，即使信任模式也必须由用户确认；无 Windows Sandbox 时明确失败，不自动启用功能。"
    )
    source = "Windows Sandbox / 沙箱策略"
    native_command = "WindowsSandbox.exe + mapped PowerShell collector"
    timeout_seconds = 900.0
    requires_confirmation = True
    requires_confirmation_in_trusted = True
    confirmation_reason = (
        "启动 Windows Sandbox 会复制目标到暂存区、打开隔离桌面并在沙箱内运行可执行目标（网络默认关闭）；"
        "宿主原文件不会被修改。请确认目标任务、超时与输出目录。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["status", "plan", "run"]},
            "path": {"type": "string", "maxLength": 1024, "description": "可选目标文件/目录；省略用当前诊断目标。"},
            "issue": {"type": "string", "maxLength": 2000},
            "allow_network": {"type": "boolean", "default": False},
            "execute_target": {
                "type": "boolean",
                "default": True,
                "description": "run 时是否在沙箱内实际运行目标（仅对 exe/com/bat/cmd/ps1/msi 文件生效）。",
            },
            "execute_timeout_seconds": {
                "type": "integer",
                "minimum": 5,
                "maximum": 600,
                "default": 60,
            },
            "timeout_seconds": {"type": "integer", "minimum": 60, "maximum": 1800, "default": 600},
        },
        "additionalProperties": False,
    }

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        ensure_no_unknown_keys(
            args,
            [
                "mode",
                "path",
                "issue",
                "allow_network",
                "execute_target",
                "execute_timeout_seconds",
                "timeout_seconds",
            ],
            self.name,
        )
        mode = require_text(args.get("mode"), "mode", max_length=20)
        if mode not in {"status", "plan", "run"}:
            raise RuntimeError("mode 必须是 status、plan 或 run。")
        path = args.get("path")
        if path is not None and not isinstance(path, str):
            raise RuntimeError("path 必须是字符串。")
        issue = args.get("issue", "")
        if issue is not None and not isinstance(issue, str):
            raise RuntimeError("issue 必须是字符串。")
        allow_network = args.get("allow_network", False)
        if not isinstance(allow_network, bool):
            raise RuntimeError("allow_network 必须是布尔值。")
        execute_target = args.get("execute_target", True)
        if not isinstance(execute_target, bool):
            raise RuntimeError("execute_target 必须是布尔值。")
        return {
            "mode": mode,
            "path": path,
            "issue": issue or "",
            "allow_network": allow_network,
            "execute_target": execute_target,
            "execute_timeout_seconds": require_int(
                args.get("execute_timeout_seconds", 60),
                "execute_timeout_seconds",
                minimum=5,
                maximum=600,
            ),
            "timeout_seconds": require_int(
                args.get("timeout_seconds", 600), "timeout_seconds", minimum=60, maximum=1800
            ),
        }

    def collect(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        controller = SandboxController()
        mode = args["mode"]
        if mode == "status":
            from ..sandbox import detect_sandbox_backends

            caps = detect_sandbox_backends()
            return ToolResult(
                ok=True,
                summary=(
                    f"沙箱状态：可用 {', '.join(caps.get('available') or []) or '无'}；"
                    f"推荐 {caps.get('recommended') or '无'}"
                ),
                data=caps,
                source=self.source,
                native_command=self.native_command,
            )
        target = ctx.target
        if args.get("path"):
            from pathlib import Path

            target = Path(args["path"]).expanduser()
            try:
                target = target.resolve(strict=True)
            except OSError as exc:
                raise RuntimeError(f"沙箱目标不存在：{exc}") from exc
        plan = build_sandbox_plan(
            target,
            args.get("issue") or "",
            prepare=mode == "run",
            execute_target=bool(args.get("execute_target", True)),
            execute_timeout_seconds=int(args.get("execute_timeout_seconds", 60)),
            allow_network=bool(args.get("allow_network", False)),
        )
        if mode == "plan":
            plan["launch_requires_confirmation"] = True
            return ToolResult(
                ok=bool(plan.get("recommended_backend")),
                summary=(
                    f"沙箱计划（未复制目标）：推荐 {plan.get('recommended_backend') or '无'}；"
                    f"预计 {plan.get('estimated_minutes', {}).get('minimum')}–"
                    f"{plan.get('estimated_minutes', {}).get('maximum')} 分钟；"
                    f"目标执行 {'是' if plan.get('execute_target') else '否'}"
                ),
                data=plan,
                source=self.source,
                native_command=self.native_command,
                error=None if plan.get("recommended_backend") else "没有可用的 Windows Sandbox/VM 后端",
            )
        if not controller.available():
            raise RuntimeError(
                "Windows Sandbox 未启用。请先启用 Containers-DisposableClientVM（需管理员+重启），"
                "或使用 Hyper-V/VMware/VirtualBox。"
            )
        if plan.get("backend") != "windows_sandbox":
            raise RuntimeError("当前仅自动执行 Windows Sandbox；Hyper-V/VM 方案请人工执行。")
        outcome = controller.run(
            plan,
            timeout_seconds=int(args["timeout_seconds"]),
            cancel_event=ctx.cancel_event,
        )
        execution = (outcome.result or {}).get("execution") or {}
        if execution.get("executed"):
            execution_text = (
                f"目标已运行，exit={execution.get('exit_code')}，"
                f"超时={'是' if execution.get('timed_out') else '否'}"
            )
        else:
            execution_text = "未运行目标（静态采集）"
        return ToolResult(
            ok=outcome.ok,
            summary=(
                "Windows Sandbox 采集完成："
                + ("成功" if outcome.ok else "失败")
                + f"；{execution_text}；输出 {outcome.output_dir}"
            ),
            data=outcome.to_dict(),
            source=self.source,
            native_command=self.native_command,
            error=outcome.error,
            duration_ms=outcome.duration_ms,
        )
