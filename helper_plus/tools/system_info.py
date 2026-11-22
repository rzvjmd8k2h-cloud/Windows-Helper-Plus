"""只读工具：操作系统与硬件概览。"""

from __future__ import annotations

from typing import Any

from ..models import ToolResult
from ..safety import ensure_no_unknown_keys
from ..windows import as_dict, run_powershell_json
from .base import BaseTool, ToolContext


_SYSTEM_INFO_SCRIPT = r"""
$os = Get-CimInstance -ClassName Win32_OperatingSystem
$cs = Get-CimInstance -ClassName Win32_ComputerSystem
$cpuList = @(Get-CimInstance -ClassName Win32_Processor)
$cpu = if ($cpuList.Count -gt 0) { $cpuList[0] } else { $null }
$bios = Get-CimInstance -ClassName Win32_BIOS
$secureBoot = $null
try { $secureBoot = [bool](Confirm-SecureBootUEFI -ErrorAction Stop) } catch { $secureBoot = $null }
$pendingPaths = @(
  'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending',
  'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired',
  'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\PendingFileRenameOperations'
)
$pendingReboot = $false
foreach ($p in $pendingPaths) { if (Test-Path -LiteralPath $p) { $pendingReboot = $true; break } }
$systemDrive = $env:SystemDrive
$freeGB = $null
try {
  $drive = Get-PSDrive -Name ($systemDrive.TrimEnd(':')) -ErrorAction Stop
  $freeGB = [math]::Round($drive.Free / 1GB, 2)
} catch { $freeGB = $null }
$tz = $null
try { $tz = (Get-TimeZone).Id } catch { $tz = $null }
$uptime = $null
if ($os.LastBootUpTime) { $uptime = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalHours, 1) }
$result = [ordered]@{
  os_caption = [string]$os.Caption
  os_version = [string]$os.Version
  build_number = [string]$os.BuildNumber
  architecture = [string]$os.OSArchitecture
  install_date = if ($os.InstallDate) { $os.InstallDate.ToString('yyyy-MM-dd') } else { $null }
  last_boot_time = if ($os.LastBootUpTime) { $os.LastBootUpTime.ToString('yyyy-MM-ddTHH:mm:sszzz') } else { $null }
  uptime_hours = $uptime
  computer_manufacturer = [string]$cs.Manufacturer
  computer_model = [string]$cs.Model
  cpu_name = if ($cpu) { [string]$cpu.Name.Trim() } else { $null }
  cpu_cores = if ($cpu) { [int]$cpu.NumberOfCores } else { $null }
  logical_processors = if ($cpu) { [int]$cpu.NumberOfLogicalProcessors } else { $null }
  physical_memory_gb = if ($cs.TotalPhysicalMemory) { [math]::Round($cs.TotalPhysicalMemory / 1GB, 2) } else { $null }
  bios_vendor = [string]$bios.Manufacturer
  bios_version = [string]$bios.SMBIOSBIOSVersion
  secure_boot = $secureBoot
  pending_reboot = $pendingReboot
  system_drive = $systemDrive
  system_drive_free_gb = $freeGB
  time_zone = $tz
  windows_directory = [string]$env:SystemRoot
  powershell_version = [string]$PSVersionTable.PSVersion
}
$result | ConvertTo-Json -Depth 5 -Compress
"""


class SystemInfoTool(BaseTool):
    name = "get_system_info"
    description = (
        "读取 Windows 版本/构建号/架构、CPU、内存、启动时间、安全启动、"
        "系统盘剩余空间、待重启标志等只读基线信息。无参数。"
    )
    source = "CIM / Win32_OperatingSystem"
    native_command = "Get-CimInstance Win32_OperatingSystem / Win32_ComputerSystem / Win32_Processor"
    parameters: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        ensure_no_unknown_keys(args, [], self.name)
        return {}

    def collect(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        parsed, result = run_powershell_json(
            _SYSTEM_INFO_SCRIPT,
            timeout=self.timeout_seconds,
            cancel_event=ctx.cancel_event,
        )
        data = as_dict(parsed)
        caption = data.get("os_caption") or "Windows"
        build = data.get("build_number") or "?"
        arch = data.get("architecture") or "?"
        cpu = (data.get("cpu_name") or "未知处理器").strip()
        memory = data.get("physical_memory_gb")
        pending = "是" if data.get("pending_reboot") else "否"
        summary = f"{caption}（Build {build}，{arch}）；{cpu}；内存 {memory or '?'} GB；存在待处理重启：{pending}"
        return ToolResult(
            ok=True,
            summary=summary,
            data=data,
            source=self.source,
            native_command=self.native_command,
            raw_excerpt=result.excerpt(limit=3000),
        )
