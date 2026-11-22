"""只读工具：读取 Windows 事件日志（Get-WinEvent）。"""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..models import ToolResult
from ..safety import ensure_no_unknown_keys, require_choice, require_int
from ..windows import as_dict, run_powershell_json, truncate_text
from .base import BaseTool, ToolContext


_EVENT_SCRIPT = r"""
$log = $env:HP_LOG
$hours = [int]$env:HP_HOURS
$max = [int]$env:HP_MAX
$start = (Get-Date).AddHours(-1 * $hours)
$items = @()
$queryError = $null
try {
  $items = @(Get-WinEvent -FilterHashtable @{ LogName = $log; StartTime = $start; Level = @(1,2,3) } -MaxEvents $max -ErrorAction Stop)
} catch {
  $message = $_.Exception.Message
  if ($message -notmatch 'No events were found|未找到') { $queryError = $message }
}
$events = @()
foreach ($e in $items) {
  $events += [ordered]@{
    time_created = if ($e.TimeCreated) { $e.TimeCreated.ToString('yyyy-MM-ddTHH:mm:sszzz') } else { $null }
    event_id = $e.Id
    level = if ($e.LevelDisplayName) { $e.LevelDisplayName } else { $null }
    provider = [string]$e.ProviderName
    message = [string]$e.Message
  }
}
$result = [ordered]@{
  log = $log
  hours = $hours
  requested_max = $max
  count = $events.Count
  query_error = $queryError
  events = $events
}
$result | ConvertTo-Json -Depth 6 -Compress
"""

_ALLOWED_LOGS = ("System", "Application", "Setup")


class EventLogTool(BaseTool):
    name = "get_event_logs"
    description = (
        "读取指定 Windows 事件日志（System / Application / Setup）最近一段时间内的"
        "Critical、Error、Warning 级别事件。返回时间、Event ID、级别、来源和消息摘要。"
    )
    source = "Get-WinEvent"
    native_command = "Get-WinEvent -FilterHashtable @{LogName=<log>; StartTime=<now-hours>; Level=@(1,2,3)}"
    timeout_seconds = 60.0
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "log": {
                "type": "string",
                "enum": list(_ALLOWED_LOGS),
                "description": "要读取的事件日志。",
            },
            "hours": {
                "type": "integer",
                "minimum": 1,
                "maximum": 720,
                "default": 72,
                "description": "回溯小时数（1–720）。",
            },
            "max_events": {
                "type": "integer",
                "minimum": 5,
                "maximum": 100,
                "default": 25,
                "description": "最多返回事件数（5–100）。",
            },
        },
        "required": ["log"],
        "additionalProperties": False,
    }

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        ensure_no_unknown_keys(args, ["log", "hours", "max_events"], self.name)
        log = require_choice(args.get("log"), _ALLOWED_LOGS, "log")
        hours = require_int(args.get("hours", 72), "hours", minimum=1, maximum=720)
        max_events = require_int(args.get("max_events", 25), "max_events", minimum=5, maximum=100)
        return {"log": log, "hours": hours, "max_events": max_events}

    def collect(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        parsed, result = run_powershell_json(
            _EVENT_SCRIPT,
            env_extra={
                "HP_LOG": str(args["log"]),
                "HP_HOURS": str(args["hours"]),
                "HP_MAX": str(args["max_events"]),
            },
            timeout=self.timeout_seconds,
            cancel_event=ctx.cancel_event,
        )
        data = as_dict(parsed)
        events = data.get("events") or []
        if isinstance(events, dict):
            events = [events]
        normalized_events: list[dict[str, Any]] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            normalized_events.append(
                {
                    "time_created": event.get("time_created"),
                    "event_id": event.get("event_id"),
                    "level": event.get("level"),
                    "provider": truncate_text(event.get("provider"), 160),
                    "message": truncate_text(event.get("message"), 500),
                }
            )
        data["events"] = normalized_events
        data["count"] = len(normalized_events)
        if data.get("query_error"):
            raise RuntimeError(f"事件日志查询失败：{truncate_text(data['query_error'], 400)}")

        levels = Counter(str(e.get("level") or "未知") for e in normalized_events)
        providers = Counter(str(e.get("provider") or "未知") for e in normalized_events)
        top = "、".join(f"{name}×{count}" for name, count in providers.most_common(3))
        if normalized_events:
            summary = (
                f"{args['log']} 日志最近 {args['hours']} 小时：{len(normalized_events)} 条 "
                f"{' / '.join(f'{k} {v}' for k, v in levels.items())}；主要来源：{top or '未知'}"
            )
        else:
            summary = f"{args['log']} 日志最近 {args['hours']} 小时无 Critical/Error/Warning 事件"
        return ToolResult(
            ok=True,
            summary=summary,
            data=data,
            source=self.source,
            native_command=self.native_command,
            raw_excerpt=truncate_text(result.excerpt(limit=5000), 5000),
        )
