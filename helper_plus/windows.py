"""Windows 原生命令封装。

安全要点：
1. 所有外部命令都使用参数数组调用，绝不使用 ``shell=True``；
2. 动态数据（路径、枚举值等）通过子进程环境变量传入 PowerShell 脚本，
   不拼接进脚本文本，避免命令注入；
3. 隐藏控制台窗口（``CREATE_NO_WINDOW``），不弹出黑框；
4. 输出统一按 UTF-8 解码，并限制大小，防止日志把内存/提示词撑爆。
"""

from __future__ import annotations

from dataclasses import dataclass
import locale
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Mapping


CREATE_NO_WINDOW = 0x08000000
POWERSHELL_TIMEOUT_DEFAULT = 45
MAX_RAW_OUTPUT = 16_000


class NativeCommandError(RuntimeError):
    """原生命令调用失败（用于工具层转成结构化失败证据）。"""


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.cancelled

    def excerpt(self, limit: int = MAX_RAW_OUTPUT) -> str:
        text = (self.stdout + ("\n" + self.stderr if self.stderr else "")).strip()
        if len(text) > limit:
            return text[:limit] + f"\n…（输出已截断，共 {len(text)} 字符）"
        return text


def powershell_executable() -> str:
    if sys.platform != "win32":
        raise NativeCommandError("该工具仅支持 Windows。")
    candidate = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("powershell.exe")
    if found:
        return found
    raise NativeCommandError("未找到 Windows PowerShell（powershell.exe）。")


def _decode(data: bytes) -> str:
    """严格按常见编码依次解码；避免 UTF-8 的 errors=replace 把 GBK 输出变成乱码。"""
    if not data:
        return ""
    for encoding in ("utf-8-sig", "gb18030", "cp936", locale.getpreferredencoding(False) or "cp936"):
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def run_process(
    argv: list[str],
    *,
    timeout: float = 30,
    env_extra: Mapping[str, str] | None = None,
    cancel_event: threading.Event | None = None,
) -> CommandResult:
    """带超时/取消支持的安全子进程执行器。"""
    if not argv:
        raise ValueError("argv 不能为空")
    env = os.environ.copy()
    if env_extra:
        for key, value in env_extra.items():
            if not key.startswith(("HP_", "HELPER_PLUS_")):
                raise ValueError(f"不允许注入环境变量: {key}")
            env[key] = str(value)
    started = time.monotonic()
    creationflags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=env,
            shell=False,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise NativeCommandError(f"无法启动命令 {argv[0]}: {exc}") from exc

    out_bytes = b""
    err_bytes = b""
    timed_out = False
    cancelled = False
    deadline = started + max(1.0, float(timeout))
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                proc.kill()
                try:
                    out_bytes, err_bytes = proc.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                proc.kill()
                try:
                    out_bytes, err_bytes = proc.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
                break
            try:
                out_bytes, err_bytes = proc.communicate(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.communicate(timeout=3)
            except Exception:
                pass
    duration_ms = int((time.monotonic() - started) * 1000)
    return CommandResult(
        argv=argv,
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=_decode(out_bytes),
        stderr=_decode(err_bytes),
        duration_ms=duration_ms,
        timed_out=timed_out,
        cancelled=cancelled,
    )


_PS_PROLOGUE = (
    "$ErrorActionPreference='Stop';"
    "$ProgressPreference='SilentlyContinue';"
    "$WarningPreference='SilentlyContinue';"
    "$InformationPreference='SilentlyContinue';"
    "$VerbosePreference='SilentlyContinue';"
    "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);"
    "$OutputEncoding=[Console]::OutputEncoding;"
)
_JSON_MARKER = "@@HP_JSON@@"


def _extract_json_after_marker(text: str) -> str:
    index = text.rfind(_JSON_MARKER)
    if index < 0:
        raise NativeCommandError("PowerShell 未返回结构化结果（缺少 JSON 标记）。")
    payload = text[index + len(_JSON_MARKER) :].strip()
    if not payload:
        raise NativeCommandError("PowerShell 返回了空的 JSON 结果。")
    # 标记之后只应有一个 JSON 对象；保留到最后一个 }，容忍尾部空白/提示。
    last_brace = payload.rfind("}")
    if last_brace >= 0:
        payload = payload[: last_brace + 1]
    return payload


def run_powershell_json(
    script: str,
    *,
    env_extra: Mapping[str, str] | None = None,
    timeout: float = POWERSHELL_TIMEOUT_DEFAULT,
    cancel_event: threading.Event | None = None,
) -> tuple[dict[str, Any] | list[Any], CommandResult]:
    """运行固定 PowerShell 脚本，并解析 ``@@HP_JSON@@`` 之后的结构化输出。

    ``script`` 必须是代码中写死的常量。任何动态输入必须经 ``env_extra``
    以 ``HP_`` 前缀传入，并在脚本中用 ``$env:HP_xxx`` 读取。
    """
    if _JSON_MARKER in script:
        raise ValueError("PowerShell 脚本不能自行输出 JSON 标记。")
    # 标记放在脚本输出之前：脚本唯一的 stdout 输出应是 ConvertTo-Json 的结果。
    wrapped = _PS_PROLOGUE + f"Write-Output '{_JSON_MARKER}';" + script.strip()
    argv = [
        powershell_executable(),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        wrapped,
    ]
    result = run_process(argv, timeout=timeout, env_extra=env_extra, cancel_event=cancel_event)
    if result.cancelled:
        raise NativeCommandError("操作已取消。")
    if result.returncode != 0 or result.timed_out:
        detail = result.stderr.strip() or result.stdout.strip() or "未知错误"
        if result.timed_out:
            detail = f"命令超时（{timeout:.0f} 秒）：{detail}"
        raise NativeCommandError(f"PowerShell 失败：{detail[:2000]}")
    import json

    payload = _extract_json_after_marker(result.stdout)
    try:
        parsed = json.loads(payload)
    except ValueError as exc:
        raise NativeCommandError(f"PowerShell JSON 解析失败：{exc}; 输出：{payload[:1500]}") from exc
    return parsed, result


def as_dict(value: Any) -> dict[str, Any]:
    """PowerShell 对单个对象/数组的输出归一化。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {"items": value, "count": len(value)}
    return {"value": value}


def windows_directory() -> Path:
    return Path(os.environ.get("SystemRoot", r"C:\Windows"))


def program_data_directory() -> Path:
    return Path(os.environ.get("ProgramData", r"C:\ProgramData"))


def truncate_text(value: Any, limit: int = 1200) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > limit:
        return text[:limit] + f"…（截断，共 {len(text)} 字符）"
    return text
