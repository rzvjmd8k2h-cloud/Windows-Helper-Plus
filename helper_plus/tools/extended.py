"""只读高级诊断工具集合（v2.2“底层十法”）。

一个工具、多个 mode，避免注册表爆炸，同时让 Planner 可以按需选择：
- print_queue      Spooler 队列 + PrintService Operational 事件
- pnp_tree         PnP/USB/网络/打印设备树线索与错误码
- pe_dependencies  PE 导入表与缺失 DLL 检查
- cbs_logs         CBS.log / dism.log 错误与损坏行
- crash_dumps      WER dump/Report.wer 与 Driver Verifier 摘要
- file_locks       哪些进程加载了目标文件
- restore_points   系统还原点/VSS 只读查询
- performance      CPU/内存/磁盘/队列性能快照
- network_test     网卡/DNS/网关/连通性检查
- sandbox_plan     沙箱/VM 验证方案与时间预估（不执行）

所有模式只读；修改系统必须走 propose_action + 用户确认/UAC。
"""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import re
import struct
from typing import Any

from ..models import ToolResult
from ..safety import ensure_no_unknown_keys, require_int, require_text, validate_inspect_path
from ..windows import as_dict, run_powershell_json, run_process, truncate_text
from .base import BaseTool, ToolContext


_MODES = (
    "print_queue",
    "pnp_tree",
    "pe_dependencies",
    "cbs_logs",
    "crash_dumps",
    "file_locks",
    "restore_points",
    "performance",
    "network_test",
    "sandbox_plan",
)

_MACHINE_LABELS = {0x14C: "x86", 0x8664: "x64", 0xAA64: "ARM64", 0x1C4: "ARM"}


def _machine_label(machine: int | None) -> str:
    if machine is None:
        return "unknown"
    return _MACHINE_LABELS.get(machine, hex(machine))


def _read_pe_machine(path: Path) -> tuple[int | None, bool | None]:
    """只读取 PE 头，返回 (machine, 是否 PE32+)。不读整个文件。"""
    try:
        with path.open("rb") as handle:
            head = handle.read(4096)
    except OSError:
        return None, None
    if len(head) < 0x40 or head[:2] != b"MZ":
        return None, None
    pe_offset = struct.unpack_from("<I", head, 0x3C)[0]
    if pe_offset + 24 > len(head) or head[pe_offset : pe_offset + 4] != b"PE\0\0":
        return None, None
    machine = struct.unpack_from("<H", head, pe_offset + 4)[0]
    magic = struct.unpack_from("<H", head, pe_offset + 24)[0]
    return machine, magic == 0x20B


def _file_version_info(path: Path) -> tuple[str, str]:
    """读取版本资源中的文件版本/产品版本；读不到时返回空字符串。"""
    if os.name != "nt":
        return "", ""
    try:
        import ctypes
        from ctypes import wintypes

        version = ctypes.windll.version
        version.GetFileVersionInfoSizeW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        size = version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return "", ""
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
            return "", ""
        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        if not version.VerQueryValueW(
            buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)
        ):
            return "", ""
        if not pointer.value or length.value < 52:
            return "", ""
        words = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))
        file_ms, file_ls, product_ms, product_ls = words[2], words[3], words[4], words[5]

        def _format(ms: int, ls: int) -> str:
            return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"

        return _format(file_ms, file_ls), _format(product_ms, product_ls)
    except Exception:  # noqa: BLE001 - 版本资源缺失不应让诊断失败
        return "", ""


def _fmt_ms(ms: int) -> str:
    return f"{int(ms)} ms"


def _read_tail(path: Path, max_bytes: int = 4 * 1024 * 1024) -> tuple[str, bool]:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            data = handle.read()
    except OSError as exc:
        raise RuntimeError(f"无法读取 {path}：{exc}") from exc
    for encoding in ("utf-8", "gb18030", "utf-16", "mbcs"):
        try:
            return data.decode(encoding, errors="replace"), size > max_bytes
        except LookupError:
            continue
    return data.decode("utf-8", errors="replace"), size > max_bytes


def _pe_dependencies(path: Path) -> dict[str, Any]:
    """最小 PE 导入表解析：架构、子系统、导入 DLL，并检查常见目录是否存在。"""
    data = path.read_bytes()
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise RuntimeError("目标不是 PE 文件（缺少 MZ 头）。")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset + 24 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise RuntimeError("目标不是有效 PE 文件（缺少 PE 签名）。")
    coff_offset = pe_offset + 4
    machine, sections, timestamp = struct.unpack_from("<HHI", data, coff_offset)
    optional_size = struct.unpack_from("<H", data, coff_offset + 16)[0]
    optional_offset = coff_offset + 20
    if optional_size < 2 or optional_offset + optional_size > len(data):
        raise RuntimeError("PE 可选头不完整。")
    magic = struct.unpack_from("<H", data, optional_offset)[0]
    pe32_plus = magic == 0x20B
    is64 = pe32_plus
    subsystem_offset = optional_offset + (0x5C if pe32_plus else 0x44)
    subsystem = struct.unpack_from("<H", data, subsystem_offset)[0] if optional_offset + optional_size >= subsystem_offset + 2 else None
    dir_offset = optional_offset + (0x70 if pe32_plus else 0x60)
    import_rva = import_size = 0
    if optional_offset + optional_size >= dir_offset + 16:
        import_rva, import_size = struct.unpack_from("<II", data, dir_offset + 8)

    section_offset = optional_offset + optional_size
    section_headers = []
    for index in range(sections):
        offset = section_offset + index * 40
        if offset + 40 > len(data):
            break
        name = data[offset : offset + 8].rstrip(b"\x00").decode("ascii", errors="replace")
        vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, offset + 8)
        section_headers.append((name, vaddr, max(vsize, rawsize), rawptr, rawsize))

    def rva_to_offset(rva: int) -> int | None:
        for _name, vaddr, size, rawptr, rawsize in section_headers:
            if vaddr <= rva < vaddr + max(size, rawsize):
                return rawptr + (rva - vaddr)
        if rva < len(data):
            return rva
        return None

    imports: list[str] = []
    if import_rva:
        offset = rva_to_offset(import_rva)
        if offset is not None:
            for descriptor_index in range(128):
                base = offset + descriptor_index * 20
                if base + 20 > len(data):
                    break
                original_first, timestamp_d, forwarder, name_rva, first_thunk = struct.unpack_from("<IIIII", data, base)
                if original_first == 0 and name_rva == 0 and first_thunk == 0:
                    break
                name_offset = rva_to_offset(name_rva)
                if name_offset is None:
                    continue
                end = data.find(b"\x00", name_offset, min(name_offset + 512, len(data)))
                if end < 0:
                    continue
                try:
                    name = data[name_offset:end].decode("ascii", errors="replace")
                except Exception:  # noqa: BLE001
                    continue
                if name and name not in imports:
                    imports.append(name)

    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    system32 = windows / "System32"
    syswow64 = windows / "SysWOW64"
    # 32 位目标优先 SysWOW64；64 位/ARM64 目标不用 SysWOW64，避免把错误架构的 DLL 当成可用依赖。
    if machine == 0x14C:
        search_dirs = [path.parent, syswow64, system32, system32 / "downlevel", windows]
    else:
        search_dirs = [path.parent, system32, system32 / "downlevel", windows]
    missing = []
    resolved = {}
    api_set = []
    for dll in imports:
        # api-ms-win-*/ext-ms-* 是 Windows API Set 契约，不一定有同名 DLL 文件；
        # 不能把它们当成“缺失 DLL”。
        if dll.lower().startswith(("api-ms-win-", "ext-ms-")):
            api_set.append(dll)
            continue
        found = None
        for directory in search_dirs:
            candidate = directory / dll
            if candidate.exists():
                found = str(candidate)
                break
        if found:
            resolved[dll] = found
        else:
            missing.append(dll)

    machine_label = _machine_label(machine)
    exe_file_version, exe_product_version = _file_version_info(path)
    dll_checks: list[dict[str, Any]] = []
    incompatible: list[dict[str, Any]] = []
    version_missing: list[str] = []
    for dll, found in resolved.items():
        dll_machine, dll_pe32_plus = _read_pe_machine(Path(found))
        compatible: bool | None = None
        if dll_machine is not None and machine:
            compatible = dll_machine == machine
        file_version, product_version = _file_version_info(Path(found))
        dll_checks.append(
            {
                "dll": dll,
                "path": found,
                "machine": _machine_label(dll_machine),
                "pe32_plus": dll_pe32_plus,
                "compatible": compatible,
                "file_version": file_version,
                "product_version": product_version,
            }
        )
        if compatible is False:
            incompatible.append(
                {
                    "dll": dll,
                    "path": found,
                    "expected_machine": machine_label,
                    "actual_machine": _machine_label(dll_machine),
                }
            )
        if not file_version:
            version_missing.append(dll)

    return {
        "path": str(path),
        "machine": machine_label,
        "pe32_plus": pe32_plus,
        "subsystem": subsystem,
        "timestamp": timestamp,
        "file_version": exe_file_version,
        "product_version": exe_product_version,
        "sections": [item[0] for item in section_headers],
        "import_dlls": imports,
        "api_set_contracts": api_set,
        "resolved_dlls": resolved,
        "dll_checks": dll_checks,
        "missing_dlls": missing,
        "incompatible_dlls": incompatible,
        "version_info_missing": version_missing,
        "search_dirs": [str(item) for item in search_dirs],
        "is64": is64,
    }


class ExtendedDiagnosticsTool(BaseTool):
    name = "extended_diagnostics"
    description = (
        "只读高级诊断（十法合集）：mode=print_queue（打印队列/PrintService 事件）、"
        "pnp_tree（PnP/USB/网络/打印设备树）、pe_dependencies（PE 导入/架构/版本/缺失 DLL）、"
        "cbs_logs（CBS/DISM 错误）、crash_dumps（WER dump/Driver Verifier 摘要）、"
        "file_locks（哪些进程加载了文件）、restore_points（还原点/VSS 只读）、"
        "performance（CPU/内存/磁盘/队列快照）、network_test（网卡/DNS/网关/连通性）、"
        "sandbox_plan（沙箱/VM 验证方案与时间预估，不执行）。"
    )
    source = "Windows 原生只读高级诊断"
    native_command = "CIM / Get-WinEvent / PE parser / CBS logs / WER / counters / network cmdlets"
    timeout_seconds = 120.0
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": list(_MODES), "description": "高级诊断模式。"},
            "path": {
                "type": "string",
                "maxLength": 1024,
                "description": "pe_dependencies / file_locks 使用的文件路径；省略则使用当前目标。",
            },
            "host": {
                "type": "string",
                "maxLength": 253,
                "description": "network_test 的目标主机/域名（可选）。",
            },
            "hours": {"type": "integer", "minimum": 1, "maximum": 168, "default": 24},
            "max_lines": {"type": "integer", "minimum": 5, "maximum": 300, "default": 40},
        },
        "additionalProperties": False,
    }

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        ensure_no_unknown_keys(args, ["mode", "path", "host", "hours", "max_lines"], self.name)
        mode = require_text(args.get("mode"), "mode", max_length=40)
        if mode not in _MODES:
            raise RuntimeError(f"mode 必须是：{', '.join(_MODES)}")
        path = args.get("path")
        if path is not None and not isinstance(path, str):
            raise RuntimeError("path 必须是字符串。")
        host = args.get("host")
        if host is not None and not isinstance(host, str):
            raise RuntimeError("host 必须是字符串。")
        return {
            "mode": mode,
            "path": path,
            "host": host,
            "hours": require_int(args.get("hours", 24), "hours", minimum=1, maximum=168),
            "max_lines": require_int(args.get("max_lines", 40), "max_lines", minimum=5, maximum=300),
        }

    def collect(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        mode = str(args["mode"])
        if mode == "print_queue":
            return self._print_queue(args, ctx)
        if mode == "pnp_tree":
            return self._pnp_tree(args, ctx)
        if mode == "pe_dependencies":
            return self._pe(args, ctx)
        if mode == "cbs_logs":
            return self._cbs(args, ctx)
        if mode == "crash_dumps":
            return self._crash_dumps(args, ctx)
        if mode == "file_locks":
            return self._file_locks(args, ctx)
        if mode == "restore_points":
            return self._restore_points(args, ctx)
        if mode == "performance":
            return self._performance(args, ctx)
        if mode == "network_test":
            return self._network_test(args, ctx)
        if mode == "sandbox_plan":
            return self._sandbox_plan(args, ctx)
        raise RuntimeError(f"未知 mode：{mode}")

    def _ps(self, script: str, *, env: dict[str, str] | None = None, ctx: ToolContext) -> tuple[dict[str, Any], str]:
        parsed, result = run_powershell_json(
            script,
            env_extra=env or {},
            timeout=self.timeout_seconds,
            cancel_event=ctx.cancel_event,
        )
        return as_dict(parsed), result.excerpt(limit=4000) if hasattr(result, "excerpt") else ""

    def _result(self, summary: str, data: dict[str, Any], raw: str = "") -> ToolResult:
        return ToolResult(
            ok=True,
            summary=summary,
            data=data,
            source=self.source,
            native_command=self.native_command,
            raw_excerpt=truncate_text(raw, 5000),
        )

    def _print_queue(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        script = r"""
$limit=[int]$env:HP_LIMIT; $hours=[int]$env:HP_HOURS
$printers=@(); $jobs=@(); $events=@(); $warnings=@()
try { $printers=@(Get-CimInstance Win32_Printer -ErrorAction Stop | Select-Object Name,Default,WorkOffline,PrinterStatus,DetectedErrorState,DriverName,PortName,Shared,Local,Network) } catch { $warnings += "Win32_Printer: $($_.Exception.Message)" }
try { $jobs=@(Get-CimInstance Win32_PrintJob -ErrorAction Stop | Select-Object -First $limit Name,JobId,Document,Owner,Status,TotalPages,PagesPrinted,Size,TimeSubmitted,StartTime,UntilTime,Notify,DataType) } catch { $warnings += "Win32_PrintJob: $($_.Exception.Message)" }
try {
  $start=(Get-Date).AddHours(-$hours)
  $events=@(Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-PrintService/Operational'; StartTime=$start} -MaxEvents $limit -ErrorAction Stop |
    Select-Object TimeCreated,Id,LevelDisplayName,ProviderName,Message)
} catch { $warnings += "PrintService/Operational: $($_.Exception.Message)" }
@{printers=$printers;jobs=$jobs;events=$events;warnings=$warnings;counts=[ordered]@{printers=$printers.Count;jobs=$jobs.Count;events=$events.Count}} | ConvertTo-Json -Depth 6 -Compress
"""
        data, raw = self._ps(script, env={"HP_LIMIT": str(args["max_lines"]), "HP_HOURS": str(args["hours"])}, ctx=ctx)
        printers = data.get("printers") or []
        jobs = data.get("jobs") or []
        events = data.get("events") or []
        if isinstance(printers, dict):
            printers = [printers]
        if isinstance(jobs, dict):
            jobs = [jobs]
        if isinstance(events, dict):
            events = [events]
        summary = f"打印队列：{len(printers)} 台打印机、{len(jobs)} 个队列作业、{len(events)} 条 PrintService 事件"
        return self._result(summary, data, raw)

    def _pnp_tree(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        script = r"""
$limit=[int]$env:HP_LIMIT
$devices=@()
$warnings=@()
try {
  $devices=@(Get-CimInstance Win32_PnPEntity -ErrorAction Stop |
    Where-Object { $_.Name -or $_.PNPClass } |
    Sort-Object PNPClass,Name |
    Select-Object -First $limit Name,DeviceID,PNPClass,Manufacturer,Status,ConfigManagerErrorCode,Service,Present,ClassGuid)
} catch { $warnings += "Win32_PnPEntity: $($_.Exception.Message)" }
$problems=@($devices | Where-Object { $_.ConfigManagerErrorCode -notin @(0,$null,'0') })
@{devices=$devices;problem_count=$problems.Count;warnings=$warnings} | ConvertTo-Json -Depth 6 -Compress
"""
        data, raw = self._ps(script, env={"HP_LIMIT": str(args["max_lines"])}, ctx=ctx)
        devices = data.get("devices") or []
        if isinstance(devices, dict):
            devices = [devices]
        problems = [d for d in devices if d.get("ConfigManagerErrorCode") not in (0, "0", None)]
        summary = f"PnP 设备树线索：{len(devices)} 个设备，{len(problems)} 个有错误码"
        return self._result(summary, data, raw)

    def _pe(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = validate_inspect_path(args.get("path"), ctx.target if ctx.target and ctx.target.is_file() else None)
        if not path.is_file():
            raise RuntimeError(f"不是文件：{path}")
        data = _pe_dependencies(path)
        missing = data.get("missing_dlls") or []
        incompatible = data.get("incompatible_dlls") or []
        version_missing = data.get("version_info_missing") or []
        summary = (
            f"PE 依赖：{path.name}（{data.get('machine')}）；"
            f"导入 {len(data.get('import_dlls') or [])} 个 DLL，缺失 {len(missing)} 个"
        )
        if missing:
            summary += "：" + "、".join(missing[:8])
        if incompatible:
            summary += "；架构不匹配 " + str(len(incompatible)) + " 个：" + "、".join(
                f"{item['dll']}({item['actual_machine']}≠{item['expected_machine']})"
                for item in incompatible[:8]
            )
        if version_missing:
            summary += f"；{len(version_missing)} 个 DLL 无版本信息"
        return self._result(summary, data)

    def _cbs(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        candidates = [
            root / "Logs" / "CBS" / "CBS.log",
            root / "Logs" / "DISM" / "dism.log",
        ]
        max_lines = int(args["max_lines"])
        result: list[dict[str, Any]] = []
        warnings: list[str] = []
        for path in candidates:
            if not path.exists():
                warnings.append(f"不存在：{path}")
                continue
            try:
                text, truncated = _read_tail(path)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{path.name}: {exc}")
                continue
            matches = []
            pattern = re.compile(r"(?i)(error|failed|failure|corrupt|0x[0-9a-f]{8}|\[SR\].*repair|missing)")
            for line in text.splitlines():
                if pattern.search(line):
                    matches.append(line.strip()[:600])
            result.append(
                {
                    "file": path.name,
                    "path": str(path),
                    "size_bytes": path.stat().st_size,
                    "truncated_tail_read": truncated,
                    "matched_lines": matches[-max_lines:],
                    "match_count": len(matches),
                }
            )
        summary = "CBS/DISM 日志：" + "；".join(
            f"{item['file']} {item['match_count']} 条匹配" for item in result
        ) if result else "CBS/DISM 日志不可读或不存在"
        return self._result(summary, {"files": result, "warnings": warnings})

    def _crash_dumps(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        program_data = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
        roots = [
            program_data / "Microsoft" / "Windows" / "WER" / "ReportArchive",
            program_data / "Microsoft" / "Windows" / "WER" / "ReportQueue",
        ]
        dumps: list[dict[str, Any]] = []
        warnings: list[str] = []
        for root in roots:
            if not root.exists():
                continue
            try:
                for path in root.rglob("*"):
                    if not path.is_file():
                        continue
                    if path.suffix.lower() in {".dmp", ".mdmp"} or path.name.lower() == "report.wer":
                        dumps.append(
                            {
                                "path": str(path),
                                "name": path.name,
                                "size_bytes": path.stat().st_size,
                                "modified": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
                            }
                        )
            except OSError as exc:
                warnings.append(f"{root}: {exc}")
        verifier = ""
        try:
            proc = run_process(["verifier.exe", "/query"], timeout=20, cancel_event=ctx.cancel_event)
            verifier = (proc.stdout or proc.stderr)[:3000]
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Driver Verifier: {exc}")
        dumps.sort(key=lambda item: item["modified"], reverse=True)
        summary = f"崩溃/WER：{len(dumps)} 个 dump/报告文件；Driver Verifier 输出 {len(verifier)} 字符"
        return self._result(
            summary,
            {"dumps": dumps[: int(args["max_lines"])], "count": len(dumps), "driver_verifier": verifier, "warnings": warnings},
        )

    def _file_locks(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = validate_inspect_path(args.get("path"), ctx.target if ctx.target and ctx.target.is_file() else None)
        script = r"""
$needle=$env:HP_PATH.ToLower()
$rows=@()
$warnings=@()
try {
  Get-Process -ErrorAction SilentlyContinue | ForEach-Object {
    $proc=$_
    try {
      foreach($m in $proc.Modules){
        $name=$m.FileName
        if($name -and $name.ToLower() -like "*$needle*"){
          $rows += [ordered]@{process=$proc.ProcessName;pid=$proc.Id;module=$name}
        }
      }
    } catch {}
  }
} catch { $warnings += $_.Exception.Message }
@{path=$env:HP_PATH;matches=$rows;count=$rows.Count;warnings=$warnings} | ConvertTo-Json -Depth 5 -Compress
"""
        data, raw = self._ps(script, env={"HP_PATH": str(path)}, ctx=ctx)
        matches = data.get("matches") or []
        if isinstance(matches, dict):
            matches = [matches]
        summary = f"文件占用：{len(matches)} 个进程已加载/占用 {path.name}"
        return self._result(summary, {"path": str(path), "matches": matches, "warnings": data.get("warnings") or []}, raw)

    def _restore_points(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        script = r"""
$points=@(); $warnings=@()
try { $points=@(Get-ComputerRestorePoint -ErrorAction Stop | Select-Object -First 20 SequenceNumber,Description,CreationTime,EventType,RestorePointType) } catch { $warnings += "Get-ComputerRestorePoint: $($_.Exception.Message)" }
@{points=$points;count=$points.Count;warnings=$warnings} | ConvertTo-Json -Depth 5 -Compress
"""
        data, raw = self._ps(script, ctx=ctx)
        points = data.get("points") or []
        if isinstance(points, dict):
            points = [points]
        summary = f"系统还原点：{len(points)} 个可读取"
        if data.get("warnings"):
            summary += "（可能需管理员权限）"
        return self._result(summary, data, raw)

    def _performance(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        script = r"""
$warnings=@(); $rows=@()
try {
  $cpu=Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -ErrorAction Stop | Where-Object Name -eq '_Total'
  $mem=Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory -ErrorAction Stop
  $disk=Get-CimInstance Win32_PerfFormattedData_PerfDisk_PhysicalDisk -ErrorAction Stop | Where-Object Name -eq '_Total'
  $system=Get-CimInstance Win32_PerfFormattedData_PerfOS_System -ErrorAction Stop
  $rows=@([ordered]@{cpu_percent=$cpu.PercentProcessorTime;available_mb=$mem.AvailableMBytes;committed_mb=$mem.CommittedBytes/1MB;disk_percent=$disk.PercentDiskTime;disk_queue=$disk.AvgDiskQueueLength;processor_queue=$system.ProcessorQueueLength;context_switches=$system.ContextSwitchesPerSec;uptime_seconds=$system.SystemUpTime})
} catch { $warnings += $_.Exception.Message }
@{snapshot=$rows;warnings=$warnings} | ConvertTo-Json -Depth 5 -Compress
"""
        data, raw = self._ps(script, ctx=ctx)
        snapshot = data.get("snapshot") or []
        if isinstance(snapshot, dict):
            snapshot = [snapshot]
        summary = "性能快照：" + (
            f"CPU {snapshot[0].get('cpu_percent')}%，可用内存 {snapshot[0].get('available_mb')} MB，磁盘 {snapshot[0].get('disk_percent')}%"
            if snapshot
            else "不可用"
        )
        return self._result(summary, data, raw)

    def _network_test(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        host = str(args.get("host") or "").strip()
        script = r"""
$target=$env:HP_TARGET
$warnings=@(); $cfg=@(); $dns=@(); $ping=@(); $route=@()
try { $cfg=@(Get-NetIPConfiguration -ErrorAction Stop | Select-Object InterfaceAlias,InterfaceDescription,IPv4Address,IPv4DefaultGateway,IPv6DefaultGateway,DNSServer) } catch { $warnings += "NetIPConfiguration: $($_.Exception.Message)" }
try { $route=@(Get-NetRoute -DestinationPrefix 0.0.0.0/0 -ErrorAction Stop | Select-Object InterfaceAlias,NextHop,RouteMetric) } catch { $warnings += "NetRoute: $($_.Exception.Message)" }
if($target){
  try { $dns=@(Resolve-DnsName -Name $target -ErrorAction Stop | Select-Object Name,Type,IPAddress,NameHost) } catch { $warnings += "DNS: $($_.Exception.Message)" }
  try { $ping=@(Test-Connection -ComputerName $target -Count 2 -ErrorAction Stop | Select-Object Address,ResponseTime,StatusCode) } catch { $warnings += "Ping: $($_.Exception.Message)" }
}
@{target=$target;config=$cfg;routes=$route;dns=$dns;ping=$ping;warnings=$warnings} | ConvertTo-Json -Depth 6 -Compress
"""
        data, raw = self._ps(script, env={"HP_TARGET": host}, ctx=ctx)
        dns = data.get("dns") or []
        ping = data.get("ping") or []
        summary = f"网络检查：{len(data.get('config') or [])} 个接口，{len(data.get('routes') or [])} 条默认路由"
        if host:
            summary += f"；目标 {host} DNS {len(dns) if isinstance(dns, list) else 1} 条，Ping {len(ping) if isinstance(ping, list) else 1} 条"
        return self._result(summary, data, raw)

    def _sandbox_plan(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from helper_plus.sandbox import build_sandbox_plan

        target = ctx.target if ctx.target else None
        data = build_sandbox_plan(
            target,
            getattr(ctx.target_spec, "display", "") or "",
            prepare=False,
        )
        data["recommended"] = bool(data.get("recommended_backend"))
        data["requirements"] = [
            "管理员权限/可用的 Windows Sandbox 或 Hyper-V/VirtualBox/VMware",
            "可回滚的干净快照",
            "目标软件安装包/驱动包（官方来源）",
            "如需内核调试：WinDbg/调试符号",
        ]
        data["safety"] = [
            "沙箱/VM 内验证，不直接在本机替换系统文件或驱动；",
            "快照前后校验；结束后恢复快照；",
            "真实硬件/打印机/USB 物理问题无法由 VM 完整模拟。",
        ]
        backend = data.get("recommended_backend") or "不可用"
        minutes = data.get("estimated_minutes") or {}
        summary = (
            f"沙箱策略：推荐 {backend}；预计 {minutes.get('minimum')}–{minutes.get('maximum')} 分钟；"
            "需要用户确认，未执行。"
        )
        return self._result(summary, data)
