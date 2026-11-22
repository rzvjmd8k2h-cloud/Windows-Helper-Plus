"""只读工具：PnP 设备与打印机枚举。"""

from __future__ import annotations

from typing import Any

from ..models import ToolResult
from ..safety import ensure_no_unknown_keys, require_int, require_text
from ..windows import as_dict, run_powershell_json, truncate_text
from .base import BaseTool, ToolContext


_DEVICE_SCRIPT = r"""
$query = $env:HP_QUERY
$limit = [int]$env:HP_LIMIT
$entities = @()
$warnings = @()
try {
  $entities = @(Get-CimInstance -ClassName Win32_PnPEntity -ErrorAction Stop)
} catch { $warnings += ('PnPEntity: ' + $_.Exception.Message) }
$drivers = @()
try {
  $drivers = @(Get-CimInstance -ClassName Win32_PnPSignedDriver -ErrorAction Stop)
} catch { $warnings += ('PnPSignedDriver: ' + $_.Exception.Message) }
if ($query) {
  $entities = @($entities | Where-Object {
    ($_.Name -like "*$query*") -or ($_.DeviceID -like "*$query*") -or
    ($_.PNPClass -like "*$query*") -or ($_.Manufacturer -like "*$query*")
  })
}
$driverByDevice = @{}
foreach ($d in $drivers) {
  if ($d.DeviceID) { $driverByDevice[[string]$d.DeviceID] = $d }
}
$items = @()
foreach ($e in ($entities | Select-Object -First $limit)) {
  $drv = $null
  if ($e.DeviceID -and $driverByDevice.ContainsKey([string]$e.DeviceID)) {
    $drv = $driverByDevice[[string]$e.DeviceID]
  }
  $items += [ordered]@{
    name = [string]$e.Name
    device_id = [string]$e.DeviceID
    pnp_class = [string]$e.PNPClass
    manufacturer = [string]$e.Manufacturer
    status = [string]$e.Status
    config_manager_error_code = $e.ConfigManagerErrorCode
    service = [string]$e.Service
    driver_version = if ($drv) { [string]$drv.DriverVersion } else { $null }
    driver_date = if ($drv -and $drv.DriverDate) { $drv.DriverDate.ToString('yyyy-MM-dd') } else { $null }
    driver_provider = if ($drv) { [string]$drv.DriverProviderName } else { $null }
    inf_name = if ($drv) { [string]$drv.InfName } else { $null }
    is_signed = if ($drv) { [string]$drv.IsSigned } else { $null }
    signer = if ($drv) { [string]$drv.Signer } else { $null }
  }
}
$result = [ordered]@{
  query = $query
  count = $items.Count
  devices = $items
  warnings = $warnings
}
$result | ConvertTo-Json -Depth 6 -Compress
"""

_PRINTER_SCRIPT = r"""
$query = $env:HP_QUERY
$limit = [int]$env:HP_LIMIT
$items = @()
$errorText = $null
try {
  $printers = @(Get-CimInstance -ClassName Win32_Printer -ErrorAction Stop)
} catch {
  $errorText = [string]$_.Exception.Message
  $printers = @()
}
if ($query) {
  $printers = @($printers | Where-Object {
    ($_.Name -like "*$query*") -or ($_.DriverName -like "*$query*") -or
    ($_.PortName -like "*$query*") -or ($_.Location -like "*$query*")
  })
}
$rows = @()
foreach ($p in ($printers | Select-Object -First $limit)) {
  $rows += [ordered]@{
    name = [string]$p.Name
    default = [bool]$p.Default
    driver_name = [string]$p.DriverName
    port_name = [string]$p.PortName
    shared = [bool]$p.Shared
    share_name = [string]$p.ShareName
    published = [bool]$p.Published
    work_offline = [bool]$p.WorkOffline
    local = [bool]$p.Local
    network = [bool]$p.Network
    status = [string]$p.Status
    printer_status = $p.PrinterStatus
    detected_error_state = $p.DetectedErrorState
    location = [string]$p.Location
    comment = [string]$p.Comment
  }
}
$result = [ordered]@{
  query = $query
  count = $rows.Count
  printers = $rows
  error = $errorText
}
$result | ConvertTo-Json -Depth 6 -Compress
"""


class DeviceInfoTool(BaseTool):
    name = "get_device_info"
    description = (
        "只读查询 Windows PnP 设备：名称、DeviceID、类别、状态、错误码、关联驱动版本/签名/INF。"
        "可用 query 按设备名、硬件 ID、类别或厂商筛选；用于设备管理器和“此电脑”中的设备诊断。"
    )
    source = "CIM Win32_PnPEntity / Win32_PnPSignedDriver"
    native_command = "Get-CimInstance Win32_PnPEntity; Get-CimInstance Win32_PnPSignedDriver"
    timeout_seconds = 75.0
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "maxLength": 160, "description": "设备名/DeviceID/类别筛选片段。"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 80, "default": 30},
        },
        "additionalProperties": False,
    }

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        ensure_no_unknown_keys(args, ["query", "limit"], self.name)
        return {
            "query": require_text(args.get("query", ""), "query", required=False, max_length=160)
            if args.get("query") is not None
            else "",
            "limit": require_int(args.get("limit", 30), "limit", minimum=1, maximum=80),
        }

    def collect(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        parsed, result = run_powershell_json(
            _DEVICE_SCRIPT,
            env_extra={"HP_QUERY": args["query"], "HP_LIMIT": str(args["limit"])},
            timeout=self.timeout_seconds,
            cancel_event=ctx.cancel_event,
        )
        data = as_dict(parsed)
        devices = data.get("devices") or []
        if isinstance(devices, dict):
            devices = [devices]
        normalized = []
        for item in devices if isinstance(devices, list) else []:
            if not isinstance(item, dict):
                continue
            normalized.append(
                {
                    "name": truncate_text(item.get("name"), 240),
                    "device_id": truncate_text(item.get("device_id"), 300),
                    "pnp_class": truncate_text(item.get("pnp_class"), 120),
                    "manufacturer": truncate_text(item.get("manufacturer"), 160),
                    "status": truncate_text(item.get("status"), 80),
                    "config_manager_error_code": item.get("config_manager_error_code"),
                    "service": truncate_text(item.get("service"), 120),
                    "driver_version": truncate_text(item.get("driver_version"), 120),
                    "driver_date": truncate_text(item.get("driver_date"), 40),
                    "driver_provider": truncate_text(item.get("driver_provider"), 160),
                    "inf_name": truncate_text(item.get("inf_name"), 120),
                    "is_signed": truncate_text(item.get("is_signed"), 40),
                    "signer": truncate_text(item.get("signer"), 260),
                }
            )
        data["devices"] = normalized
        data["count"] = len(normalized)
        problems = [d for d in normalized if d.get("config_manager_error_code") not in (None, 0, "0")]
        summary = f"设备查询：{len(normalized)} 个设备"
        if problems:
            summary += f"，其中 {len(problems)} 个有错误码"
        if normalized:
            summary += f"；首个：{normalized[0]['name']}"
        return ToolResult(
            ok=True,
            summary=summary,
            data=data,
            source=self.source,
            native_command=self.native_command,
            raw_excerpt=truncate_text(result.excerpt(limit=3000), 3000),
        )


class PrinterInfoTool(BaseTool):
    name = "get_printer_info"
    description = (
        "只读查询打印机：名称、默认打印机、驱动、端口、共享/离线状态、打印队列状态。"
        "用于打印机/打印队列故障诊断。"
    )
    source = "CIM Win32_Printer"
    native_command = "Get-CimInstance Win32_Printer"
    timeout_seconds = 45.0
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "maxLength": 160, "description": "打印机名/驱动/端口筛选片段。"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 60, "default": 20},
        },
        "additionalProperties": False,
    }

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        ensure_no_unknown_keys(args, ["query", "limit"], self.name)
        return {
            "query": require_text(args.get("query", ""), "query", required=False, max_length=160)
            if args.get("query") is not None
            else "",
            "limit": require_int(args.get("limit", 20), "limit", minimum=1, maximum=60),
        }

    def collect(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        parsed, result = run_powershell_json(
            _PRINTER_SCRIPT,
            env_extra={"HP_QUERY": args["query"], "HP_LIMIT": str(args["limit"])},
            timeout=self.timeout_seconds,
            cancel_event=ctx.cancel_event,
        )
        data = as_dict(parsed)
        printers = data.get("printers") or []
        if isinstance(printers, dict):
            printers = [printers]
        normalized = []
        for item in printers if isinstance(printers, list) else []:
            if not isinstance(item, dict):
                continue
            normalized.append(
                {
                    "name": truncate_text(item.get("name"), 240),
                    "default": bool(item.get("default")),
                    "driver_name": truncate_text(item.get("driver_name"), 240),
                    "port_name": truncate_text(item.get("port_name"), 120),
                    "shared": bool(item.get("shared")),
                    "share_name": truncate_text(item.get("share_name"), 120),
                    "published": bool(item.get("published")),
                    "work_offline": bool(item.get("work_offline")),
                    "local": bool(item.get("local")),
                    "network": bool(item.get("network")),
                    "status": truncate_text(item.get("status"), 120),
                    "printer_status": item.get("printer_status"),
                    "detected_error_state": item.get("detected_error_state"),
                    "location": truncate_text(item.get("location"), 160),
                    "comment": truncate_text(item.get("comment"), 200),
                }
            )
        data["printers"] = normalized
        data["count"] = len(normalized)
        offline = [p for p in normalized if p.get("work_offline")]
        summary = f"打印机查询：{len(normalized)} 台"
        if offline:
            summary += f"，其中 {len(offline)} 台离线"
        if normalized:
            summary += f"；首个：{normalized[0]['name']}（{normalized[0].get('driver_name') or '驱动未知'}）"
        if data.get("error"):
            raise RuntimeError(f"打印机查询失败：{truncate_text(data['error'], 300)}")
        return ToolResult(
            ok=True,
            summary=summary,
            data=data,
            source=self.source,
            native_command=self.native_command,
            raw_excerpt=truncate_text(result.excerpt(limit=3000), 3000),
        )
