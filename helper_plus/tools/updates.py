"""只读工具：已安装 Windows 更新（Get-HotFix）。"""

from __future__ import annotations

from typing import Any

from ..models import ToolResult
from ..safety import ensure_no_unknown_keys, require_int
from ..windows import as_dict, run_powershell_json, truncate_text
from .base import BaseTool, ToolContext


_UPDATES_SCRIPT = r"""
$limit = [int]$env:HP_LIMIT
$items = @()
$queryError = $null
try {
  $items = @(Get-HotFix -ErrorAction Stop | Sort-Object InstalledOn -Descending | Select-Object -First $limit)
} catch {
  $queryError = [string]$_.Exception.Message
  $items = @()
}
$records = @()
foreach ($h in $items) {
  $records += [ordered]@{
    hotfix_id = [string]$h.HotFixID
    description = [string]$h.Description
    installed_on = if ($h.InstalledOn) { $h.InstalledOn.ToString('yyyy-MM-dd') } else { $null }
  }
}
$result = [ordered]@{ count = $records.Count; query_error = $queryError; updates = $records }
$result | ConvertTo-Json -Depth 5 -Compress
"""


class WindowsUpdatesTool(BaseTool):
    name = "get_windows_updates"
    description = (
        "读取已安装的 Windows 更新（Get-HotFix），用于关联“最近更新后出现问题”的故障时间线。"
        "这是已安装补丁的只读清单，不查询或触发 Windows Update。"
    )
    source = "Get-HotFix"
    native_command = "Get-HotFix | Sort-Object InstalledOn -Descending"
    timeout_seconds = 45.0
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 20,
                "description": "最多返回多少条更新记录。",
            }
        },
        "additionalProperties": False,
    }

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        ensure_no_unknown_keys(args, ["limit"], self.name)
        return {"limit": require_int(args.get("limit", 20), "limit", minimum=1, maximum=50)}

    def collect(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        parsed, result = run_powershell_json(
            _UPDATES_SCRIPT,
            env_extra={"HP_LIMIT": str(args["limit"])},
            timeout=self.timeout_seconds,
            cancel_event=ctx.cancel_event,
        )
        data = as_dict(parsed)
        updates = data.get("updates") or []
        if isinstance(updates, dict):
            updates = [updates]
        normalized = []
        for update in updates:
            if not isinstance(update, dict):
                continue
            normalized.append(
                {
                    "hotfix_id": truncate_text(update.get("hotfix_id"), 80),
                    "description": truncate_text(update.get("description"), 200),
                    "installed_on": truncate_text(update.get("installed_on"), 80),
                }
            )
        data["updates"] = normalized
        data["count"] = len(normalized)
        if data.get("query_error"):
            raise RuntimeError(f"Get-HotFix 查询失败：{truncate_text(data['query_error'], 300)}")
        latest = normalized[0] if normalized else None
        if latest:
            summary = (
                f"已安装更新 {len(normalized)} 条；最新：{latest['hotfix_id']} "
                f"（{latest.get('installed_on') or '日期未知'}，{latest['description']}）"
            )
        else:
            summary = "未读取到已安装的热修复补丁记录（可能使用其他更新通道或有待安装更新）"
        return ToolResult(
            ok=True,
            summary=summary,
            data=data,
            source=self.source,
            native_command=self.native_command,
            raw_excerpt=truncate_text(result.excerpt(limit=3000), 3000),
        )
