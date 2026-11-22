"""只读工具：可靠性监视器历史（WMI Win32_ReliabilityRecords）。"""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..models import ToolResult
from ..safety import ensure_no_unknown_keys, require_int
from ..windows import as_dict, run_powershell_json, truncate_text
from .base import BaseTool, ToolContext


_RELIABILITY_SCRIPT = r"""
$limit = [int]$env:HP_LIMIT
$items = @()
$queryError = $null
try {
  $items = @(Get-CimInstance -ClassName Win32_ReliabilityRecords -ErrorAction Stop |
    Sort-Object TimeGenerated -Descending | Select-Object -First $limit)
} catch {
  $queryError = [string]$_.Exception.Message
  $items = @()
}
$records = @()
foreach ($r in $items) {
  $records += [ordered]@{
    time = if ($r.TimeGenerated) { $r.TimeGenerated.ToString('yyyy-MM-ddTHH:mm:sszzz') } else { $null }
    source = [string]$r.SourceName
    event_id = if ($null -ne $r.EventIdentifier) { [int]$r.EventIdentifier } else { $null }
    product = [string]$r.ProductName
    message = [string]$r.Message
  }
}
$result = [ordered]@{ count = $records.Count; query_error = $queryError; records = $records }
$result | ConvertTo-Json -Depth 6 -Compress
"""


class ReliabilityTool(BaseTool):
    name = "get_reliability_records"
    description = (
        "读取 Windows 可靠性监视器（Win32_ReliabilityRecords）最近的关键事件，"
        "如崩溃、应用错误、系统错误等；用于判断故障时间线与频次。"
    )
    source = "WMI Win32_ReliabilityRecords"
    native_command = "Get-CimInstance Win32_ReliabilityRecords | Sort-Object TimeGenerated -Descending"
    timeout_seconds = 60.0
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 20,
                "description": "最多返回多少条记录。",
            }
        },
        "additionalProperties": False,
    }

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        ensure_no_unknown_keys(args, ["limit"], self.name)
        return {"limit": require_int(args.get("limit", 20), "limit", minimum=1, maximum=50)}

    def collect(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        parsed, result = run_powershell_json(
            _RELIABILITY_SCRIPT,
            env_extra={"HP_LIMIT": str(args["limit"])},
            timeout=self.timeout_seconds,
            cancel_event=ctx.cancel_event,
        )
        data = as_dict(parsed)
        records = data.get("records") or []
        if isinstance(records, dict):
            records = [records]
        normalized = []
        for record in records:
            if not isinstance(record, dict):
                continue
            normalized.append(
                {
                    "time": record.get("time"),
                    "source": truncate_text(record.get("source"), 160),
                    "event_id": record.get("event_id"),
                    "product": truncate_text(record.get("product"), 160),
                    "message": truncate_text(record.get("message"), 500),
                }
            )
        data["records"] = normalized
        data["count"] = len(normalized)
        if data.get("query_error"):
            raise RuntimeError(f"可靠性记录不可用（可能由系统策略禁用）：{truncate_text(data['query_error'], 300)}")
        top = "、".join(f"{name}×{count}" for name, count in Counter(r["source"] for r in normalized).most_common(3))
        latest = normalized[0]["time"] if normalized else "无"
        summary = f"可靠性历史记录 {len(normalized)} 条；最近一条：{latest}；主要来源：{top or '无'}"
        return ToolResult(
            ok=True,
            summary=summary,
            data=data,
            source=self.source,
            native_command=self.native_command,
            raw_excerpt=truncate_text(result.excerpt(limit=3000), 3000),
        )
