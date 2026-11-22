"""用户确认后的动作执行器（阶段 3）。

安全模型：
- 模型只能提出动作；UI 必须逐条展示命令并等待用户明确确认；
- 用户未确认时绝不执行；
- ``requires_admin`` 动作通过 UAC 启动一次性提升进程，输出写入临时文件后读取；
- 不做沙箱（按用户要求），但记录完整命令、输出、退出码与时间，便于审计。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Any

from ..local_context import log_root
from ..windows import CREATE_NO_WINDOW

_BACKUP_MAX_BYTES = 1 * 1024**3
_BACKUP_MAX_FILES = 20_000


def _is_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class ActionOutcome:
    action: dict[str, Any]
    ok: bool
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    elevated: bool = False
    error: str | None = None
    started_at: str = field(default_factory=_timestamp)
    finished_at: str = ""
    duration_ms: int = 0
    command: str = ""
    shell: str = "cmd"
    backup_dir: str | None = None
    backup_entries: list[dict[str, Any]] = field(default_factory=list)
    rollback_available: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "ok": self.ok,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "elevated": self.elevated,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "command": self.command,
            "shell": self.shell,
            "backup_dir": self.backup_dir,
            "backup_entries": self.backup_entries,
            "rollback_available": self.rollback_available,
        }


def execute_action(
    action: dict[str, Any],
    *,
    cancel_event: threading.Event | None = None,
    timeout_seconds: float = 3600.0,
) -> ActionOutcome:
    command = str(action.get("command") or "").strip()
    shell = str(action.get("shell") or "cmd").lower()
    requires_admin = bool(action.get("requires_admin"))
    started = time.monotonic()
    started_at = _timestamp()
    outcome = ActionOutcome(
        action=action,
        ok=False,
        returncode=None,
        command=command,
        shell=shell,
        started_at=started_at,
    )
    if not command:
        outcome.error = "空命令"
        outcome.finished_at = _timestamp()
        return outcome
    if shell not in {"cmd", "powershell"}:
        outcome.error = f"不支持的 shell：{shell}"
        outcome.finished_at = _timestamp()
        return outcome
    backup_paths = action.get("backup_paths") or []
    if backup_paths:
        try:
            backup = create_action_backup(action)
        except Exception as exc:  # noqa: BLE001
            outcome.error = f"动作前置备份失败，已中止执行：{type(exc).__name__}: {exc}"
            outcome.finished_at = _timestamp()
            outcome.duration_ms = int((time.monotonic() - started) * 1000)
            return outcome
        outcome.backup_dir = backup["manifest_path"]
        outcome.backup_entries = backup["entries"]
        outcome.rollback_available = True
    elif action.get("rollback_command") or action.get("rollback"):
        outcome.rollback_available = True
    try:
        if requires_admin and not _is_admin():
            result = _execute_elevated(command, shell, timeout_seconds, cancel_event)
            outcome.stdout = result["stdout"]
            outcome.stderr = result["stderr"]
            outcome.returncode = result["returncode"]
            outcome.elevated = True
            outcome.ok = result["returncode"] == 0 and not result.get("error")
            outcome.error = result.get("error")
        else:
            result = _execute_current(command, shell, timeout_seconds, cancel_event)
            outcome.stdout = result["stdout"]
            outcome.stderr = result["stderr"]
            outcome.returncode = result["returncode"]
            outcome.elevated = _is_admin()
            outcome.ok = result["returncode"] == 0 and not result.get("error")
            outcome.error = result.get("error")
    except Exception as exc:  # noqa: BLE001
        outcome.error = f"{type(exc).__name__}: {exc}"
    outcome.duration_ms = int((time.monotonic() - started) * 1000)
    outcome.finished_at = _timestamp()
    return outcome


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "mbcs", "cp936"):
        try:
            return data.decode(encoding, errors="replace")
        except LookupError:
            continue
    return data.decode("utf-8", errors="replace")


def _execute_current(
    command: str,
    shell: str,
    timeout_seconds: float,
    cancel_event: threading.Event | None,
) -> dict[str, Any]:
    if shell == "cmd":
        # cmd.exe 不走 subprocess 的列表参数转义：列表模式会把命令里的引号转成 \"，
        # cmd 不识别该转义，含引号路径（如 copy /Y "C:\a b\x.dll" ...）会被解析错位。
        # 用 /s 加一层外层引号，交给 cmd 自己剥离，才能正确执行带引号的命令。
        comspec = os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe")
        argv = f'"{comspec}" /d /s /c "{command}"'
    else:
        argv = [
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ]
    creationflags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        shell=False,
        creationflags=creationflags,
    )
    started = time.monotonic()
    out = b""
    err = b""
    while True:
        if cancel_event is not None and cancel_event.is_set():
            proc.kill()
            try:
                out, err = proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            return {"stdout": _decode(out), "stderr": _decode(err), "returncode": -1, "error": "用户取消"}
        if time.monotonic() - started > timeout_seconds:
            proc.kill()
            try:
                out, err = proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            return {"stdout": _decode(out), "stderr": _decode(err), "returncode": -1, "error": "动作执行超时"}
        try:
            out, err = proc.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            continue
    return {
        "stdout": _decode(out)[-200_000:],
        "stderr": _decode(err)[-200_000:],
        "returncode": proc.returncode,
        "error": None,
    }


_ELEVATED_RUNNER = r"""
param(
    [string]$CommandFile,
    [string]$Shell,
    [string]$OutFile,
    [string]$DoneFile,
    [string]$ErrorFile
)
$ErrorActionPreference = 'Continue'
try {
    $command = [IO.File]::ReadAllText($CommandFile, [Text.Encoding]::UTF8)
    if ($Shell -eq 'cmd') {
        $output = & "$env:ComSpec" /d /s /c $command 2>&1 | Out-String
    } else {
        $output = & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command $command 2>&1 | Out-String
    }
    $code = $LASTEXITCODE
    [IO.File]::WriteAllText($OutFile, $output, (New-Object Text.UTF8Encoding($false)))
    [IO.File]::WriteAllText($DoneFile, [string]$code, (New-Object Text.UTF8Encoding($false)))
} catch {
    [IO.File]::WriteAllText($ErrorFile, $_.Exception.ToString(), (New-Object Text.UTF8Encoding($false)))
    [IO.File]::WriteAllText($DoneFile, '1', (New-Object Text.UTF8Encoding($false)))
}
"""


def _execute_elevated(
    command: str,
    shell: str,
    timeout_seconds: float,
    cancel_event: threading.Event | None,
) -> dict[str, Any]:
    if sys.platform != "win32":
        return {"stdout": "", "stderr": "", "returncode": -1, "error": "仅 Windows 支持 UAC 动作"}
    work = Path(tempfile.gettempdir()) / "WindowsHelperPlus" / "Actions" / uuid.uuid4().hex
    work.mkdir(parents=True, exist_ok=True)
    command_file = work / "command.txt"
    runner = work / "runner.ps1"
    out_file = work / "stdout.txt"
    err_file = work / "stderr.txt"
    done_file = work / "done.txt"
    command_file.write_text(command, encoding="utf-8")
    runner.write_text(_ELEVATED_RUNNER, encoding="utf-8-sig")
    powershell = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    arguments = (
        f'-NoLogo -NoProfile -ExecutionPolicy Bypass -File "{runner}" '
        f'-CommandFile "{command_file}" -Shell "{shell}" -OutFile "{out_file}" '
        f'-DoneFile "{done_file}" -ErrorFile "{err_file}"'
    )
    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteW.argtypes = [
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_int,
    ]
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    handle = shell32.ShellExecuteW(None, "runas", powershell, arguments, str(work), 1)
    code = int(handle) if handle else 0
    if code <= 32:
        return {"stdout": "", "stderr": "", "returncode": -1, "error": "UAC 被取消或无法启动提升进程"}
    deadline = time.monotonic() + max(5.0, timeout_seconds)
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            return {"stdout": "", "stderr": "", "returncode": -1, "error": "用户取消（提升进程可能仍在运行）"}
        if done_file.exists():
            break
        time.sleep(0.2)
    if not done_file.exists():
        return {"stdout": "", "stderr": "", "returncode": -1, "error": "提升动作执行超时"}
    output = out_file.read_text(encoding="utf-8", errors="replace") if out_file.exists() else ""
    errors = err_file.read_text(encoding="utf-8", errors="replace") if err_file.exists() else ""
    try:
        returncode = int(done_file.read_text(encoding="utf-8", errors="replace").strip() or "-1")
    except ValueError:
        returncode = -1
    return {
        "stdout": output[-200_000:],
        "stderr": errors[-20_000:],
        "returncode": returncode,
        "error": None,
    }


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _backup_stats(path: Path) -> tuple[int, int]:
    total = 0
    count = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            count += 1
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
            if count > _BACKUP_MAX_FILES or total > _BACKUP_MAX_BYTES:
                return total, count
    return total, count


def create_action_backup(action: dict[str, Any]) -> dict[str, Any]:
    """动作执行前备份指定文件/目录；失败时调用方必须中止动作。"""
    raw = action.get("backup_paths") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("backup_paths 必须是非空路径数组。")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    root = log_root() / "actions" / stamp
    backup_root = root / "backup"
    backup_root.mkdir(parents=True, exist_ok=True)
    log_dir = log_root().resolve()
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        for index, item in enumerate(raw, 1):
            if not isinstance(item, str) or not item.strip():
                raise RuntimeError(f"backup_paths[{index}] 必须是非空字符串。")
            original = Path(os.path.expandvars(os.path.expanduser(item.strip())))
            original = original.resolve() if original.is_absolute() else (Path.cwd() / original).resolve()
            key = str(original).lower()
            if key in seen:
                continue
            seen.add(key)
            if original == log_dir or log_dir in original.parents:
                raise RuntimeError(f"不能备份 Log 目录本身：{original}")
            if original in log_dir.parents:
                raise RuntimeError(
                    f"不能备份包含 WHP Log 目录的上层目录：{original}；请改为备份具体文件/子目录。"
                )
            entry: dict[str, Any] = {"original": str(original)}
            if not original.exists():
                entry.update({"kind": "missing", "backup": None, "sha256": None})
                entries.append(entry)
                continue
            target = backup_root / f"{index:02d}-{original.name}"
            if original.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, target)
                entry.update(
                    {"kind": "file", "backup": str(target), "sha256": _sha256_file(original)}
                )
            else:
                total, count = _backup_stats(original)
                if count > _BACKUP_MAX_FILES or total > _BACKUP_MAX_BYTES:
                    raise RuntimeError(
                        f"备份目录过大：{count} 个文件 / {total / 1024**2:.1f} MB"
                        f"（上限 {_BACKUP_MAX_FILES} 个 / {_BACKUP_MAX_BYTES // 1024**3} GB）。"
                    )
                shutil.copytree(original, target)
                entry.update({"kind": "dir", "backup": str(target), "sha256": None})
            entries.append(entry)
        manifest = {
            "version": 1,
            "created_at": _timestamp(),
            "title": action.get("title") or "",
            "command": action.get("command") or "",
            "shell": action.get("shell") or "cmd",
            "paths": entries,
        }
        manifest_path = root / "backup-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {"manifest_path": str(manifest_path), "entries": entries, "backup_root": str(root)}
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def restore_action_backup(
    manifest_or_dir: str | Path, *, dry_run: bool = False
) -> dict[str, Any]:
    """按动作前备份清单还原文件；目录会先删除现有内容再复制回备份。"""
    path = Path(manifest_or_dir)
    if path.is_dir():
        path = path / "backup-manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"读取备份清单失败：{exc}", "restored": [], "errors": []}
    restored: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for entry in manifest.get("paths") or []:
        try:
            original = Path(str(entry.get("original") or ""))
            if not str(original):
                raise RuntimeError("备份条目缺少 original。")
            kind = entry.get("kind")
            backup_value = entry.get("backup")
            backup = Path(str(backup_value)) if backup_value else None
            if kind == "missing":
                if dry_run:
                    restored.append({"path": str(original), "action": "would_remove_if_exists"})
                else:
                    if original.exists() or original.is_symlink():
                        _remove_path(original)
                    restored.append({"path": str(original), "action": "removed_created_path"})
                continue
            if backup is None or not backup.exists():
                raise RuntimeError(f"备份文件不存在：{backup}")
            if dry_run:
                restored.append({"path": str(original), "action": f"would_restore_{kind}"})
                continue
            if kind == "file":
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, original)
                restored.append({"path": str(original), "action": "restored_file"})
            elif kind == "dir":
                if original.exists():
                    _remove_path(original)
                shutil.copytree(backup, original)
                restored.append({"path": str(original), "action": "restored_dir"})
            else:
                raise RuntimeError(f"未知备份类型：{kind}")
        except Exception as exc:  # noqa: BLE001
            errors.append({"path": str(entry.get("original") or ""), "error": str(exc)})
    return {
        "ok": not errors,
        "manifest": str(path),
        "dry_run": dry_run,
        "restored": restored,
        "errors": errors,
    }


def list_action_backups(limit: int = 30) -> list[dict[str, Any]]:
    root = log_root() / "actions"
    manifests = sorted(
        root.glob("*/backup-manifest.json"), key=lambda item: item.stat().st_mtime, reverse=True
    )
    items: list[dict[str, Any]] = []
    for manifest in manifests[: max(1, limit)]:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items.append(
            {
                "manifest": str(manifest),
                "created_at": data.get("created_at") or "",
                "title": data.get("title") or "",
                "command": data.get("command") or "",
                "paths": len(data.get("paths") or []),
            }
        )
    return items
