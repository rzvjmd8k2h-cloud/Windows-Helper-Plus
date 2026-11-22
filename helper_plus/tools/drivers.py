"""只读工具：PnP 驱动与系统驱动清单（CIM）。"""

from __future__ import annotations

from typing import Any

from ..models import ToolResult
from ..safety import SafetyError, ensure_no_unknown_keys, require_int, require_text
from ..windows import as_dict, run_powershell_json, truncate_text
from .base import BaseTool, ToolContext


_DRIVER_SCRIPT = r"""
$query = $env:HP_QUERY
$limit = [int]$env:HP_LIMIT
$warnings = @()
$pnpItems = @()
try {
  $pnpItems = @(Get-CimInstance -ClassName Win32_PnPSignedDriver -ErrorAction Stop)
} catch { $warnings += ('PnPSignedDriver: ' + $_.Exception.Message) }
if ($query) {
  $pnpItems = @($pnpItems | Where-Object {
    ($_.DeviceName -like "*$query*") -or ($_.DriverName -like "*$query*") -or
    ($_.InfName -like "*$query*") -or ($_.DeviceID -like "*$query*") -or
    ($_.Manufacturer -like "*$query*")
  })
}
$pnp = @()
foreach ($d in ($pnpItems | Sort-Object DeviceName | Select-Object -First $limit)) {
  $pnp += [ordered]@{
    device_name = [string]$d.DeviceName
    device_id = [string]$d.DeviceID
    device_class = [string]$d.DeviceClass
    manufacturer = [string]$d.Manufacturer
    driver_name = [string]$d.DriverName
    driver_version = [string]$d.DriverVersion
    driver_date = if ($d.DriverDate) { $d.DriverDate.ToString('yyyy-MM-dd') } else { $null }
    provider = [string]$d.DriverProviderName
    inf_name = [string]$d.InfName
    is_signed = [string]$d.IsSigned
    signer = [string]$d.Signer
    hardware_ids = @($d.HardWareID | Select-Object -First 3)
  }
}
$sysItems = @()
try {
  $sysItems = @(Get-CimInstance -ClassName Win32_SystemDriver -ErrorAction Stop)
} catch { $warnings += ('SystemDriver: ' + $_.Exception.Message) }
if ($query) {
  $sysItems = @($sysItems | Where-Object {
    ($_.Name -like "*$query*") -or ($_.DisplayName -like "*$query*") -or ($_.PathName -like "*$query*")
  })
}
$sys = @()
foreach ($d in ($sysItems | Sort-Object Name | Select-Object -First $limit)) {
  $sys += [ordered]@{
    name = [string]$d.Name
    display_name = [string]$d.DisplayName
    state = [string]$d.State
    start_mode = [string]$d.StartMode
    path_name = [string]$d.PathName
    service_type = [string]$d.ServiceType
  }
}
$result = [ordered]@{
  query = $query
  pnp_driver_count = $pnp.Count
  system_driver_count = $sys.Count
  pnp_drivers = $pnp
  system_drivers = $sys
  warnings = $warnings
}
$result | ConvertTo-Json -Depth 7 -Compress
"""


class DriverInfoTool(BaseTool):
    name = "get_driver_info"
    description = (
        "读取 Windows 驱动信息：PnP 签名驱动（Win32_PnPSignedDriver）和已注册系统驱动"
        "（Win32_SystemDriver）。可用 query 按设备名、驱动名、INF 名、硬件 ID 或 .sys 路径筛选。"
        "只读，不安装/删除/更新驱动。"
    )
    source = "CIM Win32_PnPSignedDriver / Win32_SystemDriver"
    native_command = "Get-CimInstance Win32_PnPSignedDriver; Get-CimInstance Win32_SystemDriver"
    timeout_seconds = 75.0
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "maxLength": 120,
                "description": "可选筛选片段，例如设备名、oem12.inf 或 xxx.sys；省略则列出部分驱动。",
            },
            "limit": {
                "type": "integer",
                "minimum": 5,
                "maximum": 80,
                "default": 30,
                "description": "每类最多返回多少条驱动。",
            },
        },
        "additionalProperties": False,
    }

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        ensure_no_unknown_keys(args, ["query", "limit"], self.name)
        query = require_text(
            args.get("query", ""), "query", required=False, max_length=120
        ) if args.get("query") is not None else ""
        if "\x00" in query:
            raise SafetyError("query 包含非法字符。")
        limit = require_int(args.get("limit", 30), "limit", minimum=5, maximum=80)
        return {"query": query, "limit": limit}

    def collect(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        parsed, result = run_powershell_json(
            _DRIVER_SCRIPT,
            env_extra={"HP_QUERY": args["query"], "HP_LIMIT": str(args["limit"])},
            timeout=self.timeout_seconds,
            cancel_event=ctx.cancel_event,
        )
        data = as_dict(parsed)
        pnp = data.get("pnp_drivers") or []
        sys_drivers = data.get("system_drivers") or []
        if isinstance(pnp, dict):
            pnp = [pnp]
        if isinstance(sys_drivers, dict):
            sys_drivers = [sys_drivers]
        warnings = data.get("warnings") or []
        if isinstance(warnings, str):
            warnings = [warnings]
        for item in pnp:
            if isinstance(item, dict):
                item["device_name"] = truncate_text(item.get("device_name"), 220)
                item["device_id"] = truncate_text(item.get("device_id"), 260)
                item["signer"] = truncate_text(item.get("signer"), 260)
                hardware = item.get("hardware_ids")
                if isinstance(hardware, str):
                    item["hardware_ids"] = [hardware]
                elif isinstance(hardware, list):
                    item["hardware_ids"] = [truncate_text(v, 160) for v in hardware[:3]]
        for item in sys_drivers:
            if isinstance(item, dict):
                item["path_name"] = truncate_text(item.get("path_name"), 300)
                item["display_name"] = truncate_text(item.get("display_name"), 160)
        data["pnp_drivers"] = pnp
        data["system_drivers"] = sys_drivers
        data["warnings"] = [truncate_text(w, 300) for w in warnings]
        query_text = f"筛选“{args['query']}”" if args["query"] else "未筛选"
        summary = (
            f"驱动信息（{query_text}）：PnP 签名驱动 {len(pnp)} 条、"
            f"系统驱动 {len(sys_drivers)} 条"
        )
        return ToolResult(
            ok=True,
            summary=summary,
            data=data,
            source=self.source,
            native_command=self.native_command,
            raw_excerpt=truncate_text(result.excerpt(limit=3000), 3000),
        )
