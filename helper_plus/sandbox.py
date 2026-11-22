"""沙箱/隔离层策略：检测后端、生成可执行计划、提供最后兜底。

定位：工具层无法静态证明（未知程序行为、驱动崩溃、系统文件争议、需要干净环境复现）
时，沙箱作为最后手段。当前实现：
- 检测 Windows Sandbox / Hyper-V / VirtualBox / VMware / WSL；
- 生成 Windows Sandbox `.wsb` 与内部采集脚本（映射目标目录只读、输出目录可写）；
- 生成 Hyper-V/VirtualBox/VMware 的快照+复制+运行方案；
- 不自动启动沙箱；启动必须经 propose_action + 用户确认/UAC（如需）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import html
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Any

from .config import PACKAGE_ROOT
from .local_context import log_root
from .system_compat import get_os_info
from .windows import CREATE_NO_WINDOW


def _which(*names: str) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def _command_exists(name: str) -> bool:
    try:
        result = subprocess.run(
            ["where.exe", name],
            capture_output=True,
            timeout=8,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return result.returncode == 0
    except Exception:  # noqa: BLE001
        return False


_EXECUTABLE_SUFFIXES = {".exe", ".com", ".bat", ".cmd", ".ps1", ".msi"}
_STAGING_EXCLUDE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".pytest_cache",
    "Log",
    "$Recycle.Bin",
    "System Volume Information",
}
_STAGING_MAX_BYTES = 2 * 1024**3
_STAGING_MAX_FILES = 20_000


def _should_execute_target(target: Path | None, execute_target: bool) -> bool:
    if not execute_target or target is None or not target.is_file():
        return False
    return target.suffix.lower() in _EXECUTABLE_SUFFIXES


def _staging_stats(path: Path) -> tuple[int, int]:
    """统计待复制目标的大小/文件数，跳过明确的无关目录，超限前尽早返回。"""
    if path.is_file():
        try:
            return path.stat().st_size, 1
        except OSError:
            return 0, 1
    total = 0
    count = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [name for name in dirs if name not in _STAGING_EXCLUDE_DIRS]
        for name in files:
            count += 1
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
            if count >= _STAGING_MAX_FILES or total >= _STAGING_MAX_BYTES:
                return total, count
    return total, count


def _ensure_staging_allowed(target: Path) -> None:
    total, count = _staging_stats(target)
    if count > _STAGING_MAX_FILES or total > _STAGING_MAX_BYTES:
        raise RuntimeError(
            f"目标过大，拒绝复制到沙箱暂存区：{count} 个文件 / {total / 1024**2:.1f} MB"
            f"（上限 {_STAGING_MAX_FILES} 个 / {_STAGING_MAX_BYTES // 1024**3} GB）。"
            "请改为选择具体文件或更小的子目录。"
        )


def _ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def detect_sandbox_backends() -> dict[str, Any]:
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    sandbox_exe = windows / "System32" / "WindowsSandbox.exe"
    hyperv = {
        "vmms": _command_exists("vmms.exe") or Path(windows / "System32" / "vmms.exe").exists(),
        "get_vm": _command_exists("Get-VM"),
    }
    virtualbox = _which("VBoxManage.exe") or (
        str(Path(r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"))
        if Path(r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe").exists()
        else None
    )
    vmware = _which("vmrun.exe") or (
        str(Path(r"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe"))
        if Path(r"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe").exists()
        else None
    )
    wsl = _command_exists("wsl.exe")
    backends = {
        "windows_sandbox": {
            "available": sandbox_exe.is_file(),
            "path": str(sandbox_exe),
            "note": "需要 Windows 11/10 Pro/Enterprise 且启用“Windows 沙盒”功能",
            "best_for": "未知程序/文件行为隔离，快速、无驱动调试",
            "rough_minutes": "1–5（启动/运行），不含启用功能与重启",
        },
        "hyperv": {
            "available": bool(hyperv["vmms"]),
            "get_vm": bool(hyperv["get_vm"]),
            "note": "需要 Hyper-V 功能和现成 VM；驱动/内核问题首选",
            "best_for": "驱动、蓝屏、系统文件、需要快照回滚",
            "rough_minutes": "20–60（含快照/启动/复制/复现）",
        },
        "virtualbox": {
            "available": bool(virtualbox),
            "path": virtualbox,
            "note": "需要现成 VM 与 Guest Additions",
            "best_for": "可回滚的独立 Windows 测试机",
            "rough_minutes": "20–60",
        },
        "vmware": {
            "available": bool(vmware),
            "path": vmware,
            "note": "需要现成 VM 与 VMware Tools",
            "best_for": "可回滚的独立 Windows 测试机",
            "rough_minutes": "20–60",
        },
        "wsl": {
            "available": wsl,
            "path": _which("wsl.exe"),
            "note": "WSL 是 Linux 环境，不能完整模拟 Windows 驱动/打印机/内核",
            "best_for": "脚本/文件静态行为，不适合 Windows 驱动/硬件",
            "rough_minutes": "2–10",
        },
    }
    available = [name for name, item in backends.items() if item["available"]]
    if "windows_sandbox" in available:
        recommended = "windows_sandbox"
    elif "hyperv" in available:
        recommended = "hyperv"
    elif "virtualbox" in available:
        recommended = "virtualbox"
    elif "vmware" in available:
        recommended = "vmware"
    elif "wsl" in available:
        recommended = "wsl"
    else:
        recommended = None
    return {
        "os": get_os_info().to_dict(),
        "backends": backends,
        "available": available,
        "recommended": recommended,
        "usable_for_windows_driver": any(name in available for name in ("windows_sandbox", "hyperv", "virtualbox", "vmware")),
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


_WINDOWS_SANDBOX_SCRIPT = r"""
$ErrorActionPreference = 'Continue'
$target = $env:WHP_TARGET
$out = $env:WHP_OUTPUT
$requireTarget = $env:WHP_REQUIRE_TARGET -eq '1'
$execute = $env:WHP_EXECUTE -eq '1'
$runTimeout = 60
if ($env:WHP_RUN_TIMEOUT) { try { $runTimeout = [int]$env:WHP_RUN_TIMEOUT } catch { } }
$result = [ordered]@{
  ok = $true
  started_at = (Get-Date).ToString('o')
  target = $target
  execute_target = $execute
  sandbox = [ordered]@{}
  file = $null
  execution = $null
  processes = $null
  stopped_services = $null
  error = $null
}
$lines = @()
try {
  if (-not $out) { throw "WHP_OUTPUT 未设置" }
  New-Item -ItemType Directory -Force -Path $out | Out-Null
  $os = Get-CimInstance Win32_OperatingSystem
  $result.sandbox = [ordered]@{
    caption = [string]$os.Caption
    build = [string]$os.BuildNumber
    version = [string]$os.Version
    architecture = [string]$os.OSArchitecture
    user = $env:USERNAME
    computer = $env:COMPUTERNAME
  }
  $lines += "Sandbox baseline $(Get-Date -Format o)"
  $lines += "OS: $($os.Caption) build $($os.BuildNumber)"
  if ($target -and (Test-Path -LiteralPath $target)) {
    $file = Get-Item -LiteralPath $target
    $hash = Get-FileHash -LiteralPath $target -Algorithm SHA256 -ErrorAction SilentlyContinue
    $sig = Get-AuthenticodeSignature -LiteralPath $target -ErrorAction SilentlyContinue
    $result.file = [ordered]@{
      path = [string]$file.FullName
      size = $file.Length
      sha256 = if ($hash) { [string]$hash.Hash } else { $null }
      signature_status = if ($sig) { [string]$sig.Status } else { $null }
      signer = if ($sig -and $sig.SignerCertificate) { [string]$sig.SignerCertificate.Subject } else { $null }
    }
    $lines += "Target exists: $($file.FullName) size=$($file.Length)"
    if ($hash) { $lines += "SHA256: $($hash.Hash)" }
    if ($sig) { $lines += "Signature: $($sig.Status) $($sig.SignerCertificate.Subject)" }
    if ($execute) {
      try {
        $ext = [IO.Path]::GetExtension($target).ToLowerInvariant()
        $runFile = $target
        $runArgs = @()
        if ($ext -eq '.ps1') {
          $runFile = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
          $runArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $target + '"'))
        } elseif ($ext -eq '.bat' -or $ext -eq '.cmd') {
          $runFile = "$env:ComSpec"
          $runArgs = @('/d', '/c', ('"' + $target + '"'))
        } elseif ($ext -eq '.msi') {
          $runFile = 'msiexec.exe'
          $runArgs = @('/i', ('"' + $target + '"'), '/qn', '/norestart')
        }
        $stdoutPath = Join-Path $out 'target-stdout.txt'
        $stderrPath = Join-Path $out 'target-stderr.txt'
        $startedAt = Get-Date
        $proc = Start-Process -FilePath $runFile -ArgumentList $runArgs `
          -WorkingDirectory (Split-Path -Parent $target) -PassThru `
          -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        $timeoutMs = [Math]::Max(5, $runTimeout) * 1000
        $timedOut = $false
        if (-not $proc.WaitForExit($timeoutMs)) {
          $timedOut = $true
          & "$env:SystemRoot\System32\taskkill.exe" /PID $proc.Id /T /F | Out-Null
        }
        try { $proc.Refresh() } catch { }
        $exitCode = $null
        try { if ($proc.HasExited) { $exitCode = $proc.ExitCode } } catch { }
        $stdoutTail = ''
        $stderrTail = ''
        if (Test-Path -LiteralPath $stdoutPath) {
          $stdoutTail = (Get-Content -LiteralPath $stdoutPath -Tail 80 -ErrorAction SilentlyContinue | Out-String)
        }
        if (Test-Path -LiteralPath $stderrPath) {
          $stderrTail = (Get-Content -LiteralPath $stderrPath -Tail 80 -ErrorAction SilentlyContinue | Out-String)
        }
        $result.execution = [ordered]@{
          executed = $true
          file = $target
          timed_out = $timedOut
          exit_code = $exitCode
          duration_ms = [int](((Get-Date) - $startedAt).TotalMilliseconds)
          stdout_tail = $stdoutTail
          stderr_tail = $stderrTail
        }
        $lines += "Executed target: $target exit=$exitCode timed_out=$timedOut"
        if ($stdoutTail) { $lines += "--- stdout ---`n$stdoutTail" }
        if ($stderrTail) { $lines += "--- stderr ---`n$stderrTail" }
      } catch {
        $result.execution = [ordered]@{
          executed = $false
          file = $target
          error = $_.Exception.ToString()
        }
        $lines += "EXEC ERROR: $($_.Exception.ToString())"
      }
    }
  } else {
    if ($requireTarget) { throw "目标未映射或不存在：$target" }
    $lines += "Target not mapped/exists: $target"
  }
  $result.processes = (Get-Process -ErrorAction SilentlyContinue).Count
  $result.stopped_services = (Get-Service -ErrorAction SilentlyContinue | Where-Object Status -ne 'Running').Count
  $lines += "Processes: $($result.processes)"
  $lines += "Stopped services: $($result.stopped_services)"
} catch {
  $result.ok = $false
  $result.error = $_.Exception.ToString()
  $lines += "ERROR: $($_.Exception.ToString())"
} finally {
  try {
    $lines | Set-Content -LiteralPath (Join-Path $out 'sandbox-report.txt') -Encoding UTF8
    $result.finished_at = (Get-Date).ToString('o')
    $result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $out 'sandbox-result.json') -Encoding UTF8
  } catch { }
}
"""


def create_windows_sandbox_plan(
    target: Path | None,
    issue: str = "",
    *,
    allow_network: bool = False,
    execute_target: bool = True,
    execute_timeout_seconds: int = 60,
    cleanup_staging: bool = True,
) -> dict[str, Any]:
    """生成 Windows Sandbox 配置/脚本；不自动启动。会复制目标到暂存区。"""
    target = target.resolve() if target else None
    if target is not None and not target.exists():
        raise RuntimeError(f"沙箱目标不存在：{target}")
    if target is not None and target.is_dir() and not target.name:
        raise RuntimeError("不能直接沙箱整个驱动器根目录；请选择具体目录或文件。")
    log_dir = log_root().resolve()
    if target is not None and (target == log_dir or log_dir in target.parents):
        raise RuntimeError("不能把 WHP 自己的 Log 目录或其子目录作为沙箱目标。")
    if target is not None:
        _ensure_staging_allowed(target)

    root = log_root() / "sandbox" / (
        datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + str(time.time_ns() % 1_000_000)
    )
    root.mkdir(parents=True, exist_ok=True)
    output = root / "output"
    output.mkdir(parents=True, exist_ok=True)
    script = root / "sandbox_collect.ps1"
    script.write_text(_WINDOWS_SANDBOX_SCRIPT, encoding="utf-8-sig")
    # 不直接映射桌面/受保护目录：先把目标复制到工作目录的 staging，
    # 只映射工作目录。这样 Windows Sandbox 不会因宿主目标 ACL/路径被拒绝访问，
    # 宿主原文件也绝不被沙箱修改。
    staging = root / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    staged_target: Path | None = None
    if target is not None:
        try:
            staged_target = staging / target.name
            if target.is_dir():
                shutil.copytree(
                    target,
                    staged_target,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(*_STAGING_EXCLUDE_DIRS),
                )
            else:
                shutil.copy2(target, staged_target)
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise RuntimeError(f"复制目标到沙箱暂存区失败：{exc}") from exc

    mapped = [
        f"<MappedFolder><HostFolder>{html.escape(str(root))}</HostFolder>"
        f"<SandboxFolder>C:\\WHP\\Work</SandboxFolder><ReadOnly>false</ReadOnly></MappedFolder>"
    ]
    networking = "Enable" if allow_network else "Disable"
    sandbox_target = (
        f"C:\\WHP\\Work\\staging\\{staged_target.name}" if staged_target else ""
    )
    will_execute = _should_execute_target(target, execute_target)
    # 用 PowerShell 启动器，避免 cmd 的 OEM/ASCII 编码问题，支持中文等非 ASCII 路径。
    launcher = root / "run_sandbox.ps1"
    launcher_text = (
        "$ErrorActionPreference = 'Continue'\r\n"
        f"$env:WHP_TARGET = {_ps_literal(sandbox_target)}\r\n"
        "$env:WHP_OUTPUT = Join-Path $PSScriptRoot 'output'\r\n"
        f"$env:WHP_REQUIRE_TARGET = {'1' if staged_target else '0'}\r\n"
        f"$env:WHP_EXECUTE = {'1' if will_execute else '0'}\r\n"
        f"$env:WHP_RUN_TIMEOUT = '{int(execute_timeout_seconds)}'\r\n"
        "& \"$env:SystemRoot\\System32\\WindowsPowerShell\\v1.0\\powershell.exe\" "
        "-NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'sandbox_collect.ps1')\r\n"
    )
    launcher.write_text(launcher_text, encoding="utf-8-sig")
    command = (
        'powershell.exe -NoProfile -ExecutionPolicy Bypass '
        '-File "C:\\WHP\\Work\\run_sandbox.ps1"'
    )
    wsb = f"""<Configuration>
  <Networking>{networking}</Networking>
  <MappedFolders>
    {''.join(mapped)}
  </MappedFolders>
  <LogonCommand>
    <Command>{html.escape(command)}</Command>
  </LogonCommand>
</Configuration>
"""
    wsb_path = root / "analysis.wsb"
    wsb_path.write_text(wsb, encoding="utf-8")
    readme = (
        f"# Windows Sandbox 分析任务\n\n"
        f"- 目标：`{target or '（无）'}`\n"
        f"- 问题：{issue or '（未填写）'}\n"
        f"- 隔离方式：目标复制到 `staging`，只映射工作目录（可写）；宿主原目录不映射，绝不被沙箱修改。\n"
        f"- 目标执行：{'是（沙箱内运行，超时 ' + str(execute_timeout_seconds) + ' 秒）' if will_execute else '否（仅静态采集）'}\n"
        f"- 结果：沙箱内脚本写 `C:\\WHP\\Work\\output\\sandbox-result.json` / `sandbox-report.txt`，"
        f"对应宿主机 `{output}`。\n"
        f"- 启动命令：\n\n```powershell\n"
        f"Start-Process -FilePath '{Path(os.environ.get('SystemRoot', r'C:\\Windows')) / 'System32' / 'WindowsSandbox.exe'}' "
        f"-ArgumentList '\"{wsb_path}\"'\n```\n"
        f"- 关闭沙箱即可丢弃所有更改；宿主原始目标不会被修改。\n"
    )
    (root / "README.md").write_text(readme, encoding="utf-8")
    return {
        "backend": "windows_sandbox",
        "prepared": True,
        "target_path": str(target) if target else None,
        "wsb_path": str(wsb_path),
        "script_path": str(script),
        "launcher_path": str(launcher),
        "work_dir": str(root),
        "output_dir": str(output),
        "staging_dir": str(staging),
        "mapped_target_dir": None,
        "mapped_work_dir": "C:\\WHP\\Work",
        "host_target_mapped": False,
        "staged_target_path": str(staged_target) if staged_target else None,
        "execute_target": will_execute,
        "execute_timeout_seconds": int(execute_timeout_seconds),
        "cleanup_staging": bool(cleanup_staging),
        "allow_network": allow_network,
        "launch_command": (
            f"Start-Process -FilePath '{Path(os.environ.get('SystemRoot', r'C:\\Windows')) / 'System32' / 'WindowsSandbox.exe'}' "
            f"-ArgumentList '\"{wsb_path}\"'"
        ),
        "readme_path": str(root / "README.md"),
    }


def build_sandbox_plan(
    target: Path | None,
    issue: str = "",
    *,
    prepare: bool = False,
    execute_target: bool = True,
    execute_timeout_seconds: int = 60,
    allow_network: bool = False,
) -> dict[str, Any]:
    capabilities = detect_sandbox_backends()
    backend = capabilities.get("recommended")
    plan: dict[str, Any] = {
        "strategic_role": "最后兜底层：静态工具无法证明/复现时，在隔离环境验证行为、驱动、系统文件或未知程序；不直接修改宿主。",
        "capabilities": capabilities,
        "recommended_backend": backend,
        "estimated_minutes": {"minimum": 20, "maximum": 60},
        "user_confirmation_required": True,
        "auto_start": False,
    }
    if backend == "windows_sandbox":
        will_execute = _should_execute_target(target, execute_target)
        plan.update(
            {
                "backend": "windows_sandbox",
                "prepared": False,
                "target_path": str(target.resolve()) if target else None,
                "execute_target": will_execute,
                "execute_timeout_seconds": int(execute_timeout_seconds),
                "host_target_mapped": False,
                "note": "仅生成策略，不创建 .wsb/副本；run 时才准备暂存区。",
            }
        )
        if prepare:
            plan.update(
                create_windows_sandbox_plan(
                    target,
                    issue,
                    allow_network=allow_network,
                    execute_target=execute_target,
                    execute_timeout_seconds=execute_timeout_seconds,
                )
            )
        plan["estimated_minutes"] = {"minimum": 2, "maximum": 10}
        plan["coverage"] = [
            (
                "未知 EXE/脚本行为（沙箱内实际运行，网络默认关闭）"
                if will_execute
                else "目标静态基线（哈希/签名/系统状态；不执行）"
            ),
            "干净环境复现软件/系统文件争议",
            "生成沙箱内基线报告回传宿主",
        ]
        plan["limitations"] = [
            "无法验证真实打印机/USB/PCI 物理设备；",
            "驱动/内核问题不适合 Windows Sandbox（需 Hyper-V/VM）；",
            "目标必须是可执行文件才会被运行；目录仅做静态采集。",
        ]
        plan["next_step"] = (
            "先 ask_user 说明将启动 Windows Sandbox、复制目标到暂存区并在沙箱内运行；"
            "用户同意后调用 sandbox_control(mode=run)，该工具在信任模式下仍会再次确认。"
        )
    elif backend in {"hyperv", "virtualbox", "vmware"}:
        plan["estimated_minutes"] = {"minimum": 20, "maximum": 60}
        plan["coverage"] = ["驱动/内核/系统文件问题", "可快照回滚的真实 Windows VM"]
        plan["next_step"] = (
            "需要用户提供/确认现成 VM 名称与快照；先 ask_user，再 propose_action 执行 "
            "Checkpoint-VM/Start-VM/复制与采集；不得自动启动。"
        )
        plan["vm_requirements"] = [
            "现成 Windows VM + 干净快照",
            "VM 内关闭共享剪贴板/网络（按需）",
            "如需拷贝工具/驱动，先在 VM 中验证签名",
        ]
    elif backend == "wsl":
        plan["coverage"] = ["脚本/文件静态行为（Linux 环境）"]
        plan["limitations"] = ["不能完整模拟 Windows 驱动/打印机/内核/PnP"]
        plan["windows_driver_capable"] = False
        plan["next_step"] = "只能作为脚本隔离，不足以兜住 Windows 内核/驱动；建议先配置 Hyper-V/Windows Sandbox。"
    else:
        plan["available"] = False
        plan["next_step"] = (
            "未检测到 Windows Sandbox/Hyper-V/VirtualBox/VMware；请用户决定是否启用 Windows Sandbox"
            "（需要 Win10/11 Pro+、管理员、重启），或提供现成虚拟机。"
        )
        plan["instructions"] = [
            "Windows 11 Pro/Enterprise：启用“Windows 沙盒”可选功能（需管理员+重启）",
            "Hyper-V：启用 Hyper-V 功能并创建/准备 Windows VM",
            "或安装 VirtualBox/VMware 并准备可回滚 VM",
        ]
    if backend != "windows_sandbox":
        plan["enable_windows_sandbox_command"] = (
            'dism.exe /Online /Enable-Feature /FeatureName:Containers-DisposableClientVM /All /NoRestart'
        )
        plan["enable_note"] = "启用 Windows Sandbox 需要 Windows 10/11 Pro/Enterprise + 管理员 + 重启；必须在用户确认后由 UAC 动作执行，绝不自动。"
    plan["windows_driver_capable"] = bool(
        capabilities.get("usable_for_windows_driver")
    )
    return plan


@dataclass
class SandboxRunResult:
    ok: bool
    plan: dict[str, Any]
    duration_ms: int
    output_dir: str | None = None
    result: dict[str, Any] | None = None
    report_text: str = ""
    host_unchanged: bool | None = None
    host_check_scope: str = "none"
    error: str | None = None
    closed: bool = False
    staging_cleaned: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "plan": self.plan,
            "duration_ms": self.duration_ms,
            "output_dir": self.output_dir,
            "result": self.result,
            "report_text": self.report_text,
            "host_unchanged": self.host_unchanged,
            "host_check_scope": self.host_check_scope,
            "error": self.error,
            "closed": self.closed,
            "staging_cleaned": self.staging_cleaned,
        }


def _sandbox_client_pids() -> set[int]:
    """当前已有的 Windows Sandbox 客户端 PID；用于只关闭本次启动的沙箱。"""
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq WindowsSandboxClient.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            timeout=8,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception:  # noqa: BLE001
        return set()
    text = (completed.stdout or b"").decode("mbcs", errors="replace")
    pids: set[int] = set()
    for line in text.splitlines():
        if not line.startswith('"'):
            continue
        parts = line.split('","')
        if len(parts) < 2:
            continue
        try:
            pids.add(int(parts[1]))
        except ValueError:
            continue
    return pids


class SandboxController:
    """启动 Windows Sandbox、等待沙箱报告、关闭并校验宿主未变化。"""

    def __init__(self) -> None:
        self.windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        self.sandbox_exe = self.windows / "System32" / "WindowsSandbox.exe"

    def available(self) -> bool:
        return self.sandbox_exe.is_file()

    @staticmethod
    def _hash_file(path: Path) -> str | None:
        try:
            import hashlib

            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except OSError:
            return None

    def run(
        self,
        plan: dict[str, Any],
        *,
        timeout_seconds: int = 600,
        cancel_event: threading.Event | None = None,
    ) -> SandboxRunResult:
        started = time.monotonic()
        if not plan.get("prepared"):
            return SandboxRunResult(
                ok=False,
                plan=plan,
                duration_ms=0,
                error="沙箱计划尚未准备（缺少 .wsb/暂存副本）；请用 sandbox_control(mode=run) 重新生成。",
            )
        wsb = Path(str(plan.get("wsb_path") or ""))
        output = Path(str(plan.get("output_dir") or ""))
        target_value = plan.get("target_path")
        target = Path(str(target_value)) if target_value else None
        staged_value = plan.get("staged_target_path")
        staged_target = Path(str(staged_value)) if staged_value else None
        if target is not None and (staged_target is None or not staged_target.exists()):
            return SandboxRunResult(
                ok=False,
                plan=plan,
                duration_ms=0,
                error="目标未能进入沙箱暂存区，拒绝启动空沙箱（可能是复制失败或目标已不存在）。",
            )
        if not self.available():
            return SandboxRunResult(
                ok=False,
                plan=plan,
                duration_ms=0,
                error=f"Windows Sandbox 未启用：缺少 {self.sandbox_exe}",
            )
        if not wsb.is_file():
            return SandboxRunResult(ok=False, plan=plan, duration_ms=0, error=f"缺少 wsb：{wsb}")
        output.mkdir(parents=True, exist_ok=True)
        result_path = output / "sandbox-result.json"
        report_path = output / "sandbox-report.txt"
        for stale in (result_path, report_path):
            try:
                stale.unlink()
            except OSError:
                pass
        before_hash = self._hash_file(target) if target and target.is_file() else None
        before_clients = _sandbox_client_pids()
        try:
            # 与手工验证过的 Start-Process 完全一致；直接用 CreateProcess
            # 传带空格的 .wsb 有时会启动成无配置的沙箱。
            launch_script = (
                f"Start-Process -FilePath '{self.sandbox_exe}' "
                f"-ArgumentList '\"{wsb}\"'"
            )
            proc = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", launch_script],
                # 不用 CREATE_NO_WINDOW：Windows Sandbox 需要正常交互窗口
                # 才会加载 .wsb 配置并执行 LogonCommand。
                creationflags=0,
            )
        except OSError as exc:
            return SandboxRunResult(
                ok=False,
                plan=plan,
                duration_ms=0,
                error=f"启动 Windows Sandbox 失败：{exc}",
            )
        deadline = time.monotonic() + max(60, int(timeout_seconds))
        error: str | None = None
        closed = False
        result_data: dict[str, Any] | None = None

        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                error = "用户取消"
                break
            if result_path.is_file():
                try:
                    result_data = json.loads(
                        result_path.read_text(encoding="utf-8-sig", errors="replace")
                    )
                except (OSError, json.JSONDecodeError) as exc:
                    error = f"沙箱结果 JSON 解析失败：{exc}"
                break
            # WindowsSandbox.exe 启动器可能先退出，实际工作由 WindowsSandboxClient 承担；
            # 不在中途判定失败，避免把仍在初始化的沙箱提前杀掉。
            time.sleep(0.5)
        else:
            error = "沙箱执行超时"
        report = ""
        if report_path.is_file():
            try:
                report = report_path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                pass
        # 只关闭本次新出现的客户端，避免误杀用户已打开的其他 Windows Sandbox。
        for pid in sorted(_sandbox_client_pids() - before_clients):
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F", "/T"],
                    capture_output=True,
                    timeout=15,
                    creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                closed = True
            except Exception:  # noqa: BLE001
                pass
        if proc.poll() is None:
            try:
                proc.terminate()
                closed = True
            except Exception:  # noqa: BLE001
                pass
        after_hash = self._hash_file(target) if target and target.is_file() else None
        host_unchanged = None
        if before_hash is not None and after_hash is not None:
            host_unchanged = before_hash == after_hash
        staging_cleaned: bool | None = None
        if plan.get("cleanup_staging", True) and plan.get("staging_dir"):
            staging = Path(str(plan["staging_dir"]))
            shutil.rmtree(staging, ignore_errors=True)
            staging_cleaned = not staging.exists()
        ok = bool(result_data and result_data.get("ok")) and error is None and host_unchanged is not False
        if not ok and error is None:
            error = (result_data or {}).get("error") or "沙箱未返回成功结果"
        return SandboxRunResult(
            ok=ok,
            plan=plan,
            duration_ms=int((time.monotonic() - started) * 1000),
            output_dir=str(output),
            result=result_data,
            report_text=report[:20000],
            host_unchanged=host_unchanged,
            host_check_scope="file" if before_hash is not None else "none",
            error=error,
            closed=closed,
            staging_cleaned=staging_cleaned,
        )

