"""只读工具：文件/驱动关联映射。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..models import ToolResult
from ..safety import SafetyError, ensure_no_unknown_keys, validate_inspect_path
from ..windows import as_dict, run_powershell_json, truncate_text
from .base import BaseTool, ToolContext


_MAP_SCRIPT = r"""
$target = $env:HP_TARGET
$item = Get-Item -LiteralPath $target -ErrorAction Stop
$dir = Split-Path -Parent $target
$base = [IO.Path]::GetFileNameWithoutExtension($target)
$ext = [IO.Path]::GetExtension($target).ToLowerInvariant()
$related = @()

# 同目录同主文件名的 INF/CAT/SYS/DLL
Get-ChildItem -LiteralPath $dir -File -ErrorAction SilentlyContinue |
  Where-Object { $_.BaseName -ieq $base -and $_.FullName -ne $item.FullName } |
  ForEach-Object { $related += $_.FullName }

$services = @()
$pnp = @()
if ($ext -eq '.sys') {
  try {
    $services = @(Get-CimInstance Win32_SystemDriver -ErrorAction Stop |
      Where-Object { $_.PathName -like "*$base*" -or $_.Name -like "$base*" } |
      Select-Object Name, DisplayName, State, StartMode, PathName, ServiceType)
  } catch { }
  try {
    $pnp = @(Get-CimInstance Win32_PnPSignedDriver -ErrorAction Stop |
      Where-Object { $_.DriverName -like "*$base*" -or $_.InfName -like "*$base*" } |
      Select-Object -First 20 DeviceName, DeviceID, DriverVersion, DriverDate, DriverProviderName, InfName, IsSigned, Signer)
  } catch { }
}

$infRefs = @()
if ($ext -eq '.inf') {
  try {
    $lines = Get-Content -LiteralPath $target -ErrorAction Stop
    foreach ($line in $lines) {
      foreach ($m in [regex]::Matches($line, '[A-Za-z0-9_.-]+\.(sys|dll|cat|inf)\b', 'IgnoreCase')) {
        $infRefs += $m.Value
      }
    }
  } catch { }
  $infRefs = @($infRefs | Sort-Object -Unique | Select-Object -First 80)
}

$relatedInfo = @()
foreach ($p in ($related | Sort-Object -Unique)) {
  $fi = Get-Item -LiteralPath $p -ErrorAction SilentlyContinue
  $relatedInfo += [ordered]@{
    path = $p
    exists = [bool]$fi
    size_bytes = if ($fi) { [long]$fi.Length } else { $null }
    extension = [IO.Path]::GetExtension($p)
  }
}

$result = [ordered]@{
  target = [string]$item.FullName
  name = [string]$item.Name
  extension = $ext
  directory = $dir
  related_files = $relatedInfo
  system_services = @($services)
  pnp_drivers = @($pnp)
  inf_file_references = @($infRefs)
}
$result | ConvertTo-Json -Depth 8 -Compress
"""


class MapDriverFilesTool(BaseTool):
    name = "map_related_files"
    description = (
        "只读映射目标文件的相关文件与驱动对象：同目录同名 INF/CAT/SYS/DLL；"
        ".sys 会关联 Win32_SystemDriver 和 PnP 签名驱动；.inf 会提取引用的 SYS/DLL/CAT 文件。"
        "用于在开始诊断时完整扫描驱动包和映射路径。"
    )
    source = "CIM Win32_SystemDriver / Win32_PnPSignedDriver + INF 引用"
    native_command = "Get-CimInstance Win32_SystemDriver / Win32_PnPSignedDriver; INF reference scan"
    timeout_seconds = 75.0
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径；省略使用当前目标。",
                "maxLength": 1024,
            }
        },
        "additionalProperties": False,
    }

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        ensure_no_unknown_keys(args, ["path"], self.name)
        path = args.get("path")
        if path is not None and not isinstance(path, str):
            raise SafetyError("path 必须是字符串。")
        return {"path": path}

    def collect(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = validate_inspect_path(args.get("path"), ctx.target)
        parsed, result = run_powershell_json(
            _MAP_SCRIPT,
            env_extra={"HP_TARGET": str(path)},
            timeout=self.timeout_seconds,
            cancel_event=ctx.cancel_event,
        )
        data = as_dict(parsed)
        related = data.get("related_files") or []
        if isinstance(related, dict):
            related = [related]
        services = data.get("system_services") or []
        if isinstance(services, dict):
            services = [services]
        pnp = data.get("pnp_drivers") or []
        if isinstance(pnp, dict):
            pnp = [pnp]
        data["related_files"] = (
            related if isinstance(related, list) else []
        )
        data["system_services"] = services if isinstance(services, list) else []
        data["pnp_drivers"] = pnp if isinstance(pnp, list) else []
        names = [Path(item["path"]).name for item in data["related_files"] if isinstance(item, dict)]
        service_names = [item.get("Name") for item in data["system_services"] if isinstance(item, dict)]
        summary = (
            f"关联映射：{path.name}；同目录相关文件 {len(names)} 个"
            + (f"（{'、'.join(names[:4])}）" if names else "")
            + (f"；系统服务 {('、'.join(str(x) for x in service_names[:3]))}" if service_names else "")
        )
        return ToolResult(
            ok=True,
            summary=summary,
            data=data,
            source=self.source,
            native_command=self.native_command,
            raw_excerpt=truncate_text(result.excerpt(limit=4000), 4000),
        )
