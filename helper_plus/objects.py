"""设备/打印机对象选择支持（UI 侧，只读枚举）。"""

from __future__ import annotations

from typing import Any

from .windows import run_powershell_json


_DEVICES_SCRIPT = r"""
$items = @(Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue |
  Where-Object { $_.Name } |
  Sort-Object Name |
  Select-Object -First 300 Name, DeviceID, PNPClass, Manufacturer, Status, ConfigManagerErrorCode)
$result = [ordered]@{ count = $items.Count; devices = $items }
$result | ConvertTo-Json -Depth 6 -Compress
"""

_PRINTERS_SCRIPT = r"""
$items = @(Get-CimInstance Win32_Printer -ErrorAction SilentlyContinue |
  Sort-Object Name |
  Select-Object Name, DriverName, PortName, Default, WorkOffline, Local, Network, Status)
$result = [ordered]@{ count = $items.Count; printers = $items }
$result | ConvertTo-Json -Depth 6 -Compress
"""


def list_devices() -> list[dict[str, Any]]:
    try:
        parsed, _ = run_powershell_json(_DEVICES_SCRIPT, timeout=45)
    except Exception:  # noqa: BLE001
        return []
    data = parsed if isinstance(parsed, dict) else {}
    devices = data.get("devices") or []
    if isinstance(devices, dict):
        devices = [devices]
    return [item for item in devices if isinstance(item, dict)]


def list_printers() -> list[dict[str, Any]]:
    try:
        parsed, _ = run_powershell_json(_PRINTERS_SCRIPT, timeout=30)
    except Exception:  # noqa: BLE001
        return []
    data = parsed if isinstance(parsed, dict) else {}
    printers = data.get("printers") or []
    if isinstance(printers, dict):
        printers = [printers]
    return [item for item in printers if isinstance(item, dict)]
