"""Windows Helper Plus 开机自启动本地 Qwen 的开关封装。"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from .config import PACKAGE_ROOT
from .windows import CREATE_NO_WINDOW
SHORTCUT_NAME = "WindowsHelperPlusLocalQwen.lnk"


def startup_dir() -> Path:
    appdata = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def shortcut_path() -> Path:
    return startup_dir() / SHORTCUT_NAME


def autostart_enabled() -> bool:
    return shortcut_path().is_file()


def _run_powershell(script: Path, *args: str) -> tuple[bool, str]:
    if not script.is_file():
        return False, f"脚本不存在：{script}"
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        *args,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=90,
            creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    def _decode(data: bytes) -> str:
        for encoding in ("utf-8-sig", "gb18030", "cp936", "mbcs"):
            try:
                return data.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        return data.decode("utf-8", errors="replace")

    output = _decode(completed.stdout or b"") + _decode(completed.stderr or b"")
    return completed.returncode == 0, output.strip()


def install_autostart() -> tuple[bool, str]:
    return _run_powershell(PACKAGE_ROOT / "scripts" / "install_local_qwen_autostart.ps1")


def remove_autostart() -> tuple[bool, str]:
    return _run_powershell(PACKAGE_ROOT / "scripts" / "uninstall_local_qwen_autostart.ps1")
