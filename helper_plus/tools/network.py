"""只读工具：网络适配器、网络设备与 IP 配置。"""

from __future__ import annotations

from typing import Any

from ..models import ToolResult
from ..safety import ensure_no_unknown_keys, require_int, require_text
from ..windows import as_dict, run_powershell_json, truncate_text
from .base import BaseTool, ToolContext


_NETWORK_SCRIPT = r"""
$query = $env:HP_QUERY
$limit = [int]$env:HP_LIMIT
$warnings = @()
$adapters = @()
try {
  $adapters = @(Get-NetAdapter -ErrorAction Stop | Select-Object -First $limit `
    Name, InterfaceDescription, Status, LinkSpeed, MacAddress, DriverProvider,
    DriverVersion, DriverDate, ifIndex, PnPDeviceID, MediaType, ConnectorPresent, AdminStatus)
} catch {
  $warnings += ('Get-NetAdapter: ' + $_.Exception.Message)
  try {
    $adapters = @(Get-CimInstance Win32_NetworkAdapter -ErrorAction Stop |
      Where-Object { $_.PhysicalAdapter -eq $true } |
      Select-Object -First $limit Name, Description, NetConnectionStatus, Speed, MACAddress,
        Manufacturer, PNPDeviceID, AdapterType, NetEnabled)
  } catch { $warnings += ('Win32_NetworkAdapter: ' + $_.Exception.Message) }
}
$configs = @()
try {
  $configs = @(Get-CimInstance Win32_NetworkAdapterConfiguration -ErrorAction Stop |
    Where-Object { $_.IPEnabled -eq $true } |
    Select-Object Description, InterfaceIndex, IPAddress, DefaultIPGateway,
      DNSServerSearchOrder, DHCPEnabled, DHCPServer, DNSDomain, MACAddress)
} catch { $warnings += ('Win32_NetworkAdapterConfiguration: ' + $_.Exception.Message) }

function Normalize($value) {
  if ($null -eq $value) { return @() }
  return @($value | ForEach-Object { [string]$_ })
}
if ($query) {
  $adapters = @($adapters | Where-Object {
    ($_.Name -like "*$query*") -or ($_.InterfaceDescription -like "*$query*") -or
    ($_.DriverProvider -like "*$query*") -or ($_.Manufacturer -like "*$query*")
  })
}
$items = @()
$index = 0
foreach ($a in $adapters) {
  $index += 1
  $description = if ($a.InterfaceDescription) { $a.InterfaceDescription } else { $a.Description }
  $cfg = $null
  if ($a.ifIndex) { $cfg = $configs | Where-Object { $_.InterfaceIndex -eq $a.ifIndex } | Select-Object -First 1 }
  if (-not $cfg -and $a.MACAddress) {
    $cfg = $configs | Where-Object { $_.MACAddress -eq $a.MACAddress } | Select-Object -First 1
  }
  $items += [ordered]@{
    name = [string]$a.Name
    description = [string]$description
    status = [string]$a.Status
    media_type = [string]$a.MediaType
    link_speed = [string]$a.LinkSpeed
    mac_address = [string]$a.MACAddress
    manufacturer = [string]$a.Manufacturer
    driver_provider = [string]$a.DriverProvider
    driver_version = [string]$a.DriverVersion
    driver_date = if ($a.DriverDate) { ([datetime]$a.DriverDate).ToString('yyyy-MM-dd') } else { $null }
    pnp_device_id = [string]$a.PNPDeviceID
    net_enabled = if ($null -ne $a.NetEnabled) { [bool]$a.NetEnabled } else { $null }
    ip_addresses = Normalize($cfg.IPAddress)
    default_gateways = Normalize($cfg.DefaultIPGateway)
    dns_servers = Normalize($cfg.DNSServerSearchOrder)
    dhcp_enabled = if ($cfg) { [bool]$cfg.DHCPEnabled } else { $null }
    dhcp_server = [string]$cfg.DHCPServer
    dns_domain = [string]$cfg.DNSDomain
  }
}
$result = [ordered]@{
  query = $query
  count = $items.Count
  adapters = $items
  warnings = $warnings
}
$result | ConvertTo-Json -Depth 8 -Compress
"""


class NetworkInfoTool(BaseTool):
    name = "get_network_info"
    description = (
        "只读查询网络适配器/网络设备：名称、厂商、描述、MAC、链路状态/速率、"
        "驱动提供者/版本/日期、PnPDeviceID、IP/网关/DNS/DHCP。"
        "用于网络设备、网卡驱动、连接故障，以及从网络设备厂商/型号推断品牌。"
    )
    source = "Get-NetAdapter / CIM Win32_NetworkAdapter(+Configuration)"
    native_command = "Get-NetAdapter; Get-CimInstance Win32_NetworkAdapterConfiguration"
    timeout_seconds = 45.0
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "maxLength": 160,
                "description": "可选：按适配器名称/描述/厂商/驱动提供者筛选。",
            },
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
            _NETWORK_SCRIPT,
            env_extra={"HP_QUERY": args["query"], "HP_LIMIT": str(args["limit"])},
            timeout=self.timeout_seconds,
            cancel_event=ctx.cancel_event,
        )
        data = as_dict(parsed)
        adapters = data.get("adapters") or []
        if isinstance(adapters, dict):
            adapters = [adapters]
        normalized = []
        for item in adapters if isinstance(adapters, list) else []:
            if not isinstance(item, dict):
                continue
            normalized.append(
                {
                    "name": truncate_text(item.get("name"), 160),
                    "description": truncate_text(item.get("description"), 300),
                    "status": truncate_text(item.get("status"), 80),
                    "media_type": truncate_text(item.get("media_type"), 120),
                    "link_speed": truncate_text(item.get("link_speed"), 80),
                    "mac_address": truncate_text(item.get("mac_address"), 60),
                    "manufacturer": truncate_text(item.get("manufacturer"), 160),
                    "driver_provider": truncate_text(item.get("driver_provider"), 160),
                    "driver_version": truncate_text(item.get("driver_version"), 120),
                    "driver_date": truncate_text(item.get("driver_date"), 40),
                    "pnp_device_id": truncate_text(item.get("pnp_device_id"), 320),
                    "net_enabled": item.get("net_enabled"),
                    "ip_addresses": [truncate_text(x, 80) for x in (item.get("ip_addresses") or [])][:8],
                    "default_gateways": [truncate_text(x, 80) for x in (item.get("default_gateways") or [])][:4],
                    "dns_servers": [truncate_text(x, 80) for x in (item.get("dns_servers") or [])][:6],
                    "dhcp_enabled": item.get("dhcp_enabled"),
                    "dhcp_server": truncate_text(item.get("dhcp_server"), 80),
                    "dns_domain": truncate_text(item.get("dns_domain"), 160),
                }
            )
        data["adapters"] = normalized
        data["count"] = len(normalized)
        connected = [a for a in normalized if str(a.get("status") or "").lower() in {"up", "2"}]
        problems = [a for a in normalized if str(a.get("status") or "").lower() in {"disabled", "not present", "disconnected"}]
        summary = f"网络适配器：{len(normalized)} 个"
        if connected:
            summary += f"，已连接 {len(connected)} 个"
        if problems:
            summary += f"，异常/未连接 {len(problems)} 个"
        if normalized:
            summary += f"；首个：{normalized[0]['description'] or normalized[0]['name']}"
        return ToolResult(
            ok=True,
            summary=summary,
            data=data,
            source=self.source,
            native_command=self.native_command,
            raw_excerpt=truncate_text(result.excerpt(limit=4000), 4000),
        )
