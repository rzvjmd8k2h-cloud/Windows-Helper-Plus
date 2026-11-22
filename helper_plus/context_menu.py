"""启动时自动维护资源管理器右键菜单。

只写 HKCU\\Software\\Classes，不需要管理员权限。每次启动做一次轻量检查：
- 11 个菜单入口都存在，且“任意文件”命令指向当前目录的 run_gui.pyw；
- 缺失或指向旧目录时，调用 scripts/install_context_menu.ps1 重新注册；
- 已正确时不做任何写入。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from .config import logs_dir
from .windows import CREATE_NO_WINDOW, powershell_executable

_MENU_NAME = "WindowsHelperPlus"

# 与 scripts/install_context_menu.ps1 中的 Entry 列表保持一致。
_MENU_PATHS = (
    r"*",
    r"AllFilesystemObjects",
    r"Directory",
    r"Folder",
    r"Drive",
    r"Directory\Background",
    r"DesktopBackground\Shell",
    r"CLSID\{20D04FE0-3AEA-1069-A2D8-08002B30309D}\shell",
    r"CLSID\{2227A280-3AEA-1069-A2DE-08002B30309D}\shell",
    r"System.Devices.PrintQueue\shell",
    r"System.Devices.PnPDevice\shell",
)


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _launcher_paths() -> tuple[Path, Path] | None:
    """返回 (pythonw, run_gui.pyw)；冻结版没有源码启动器，跳过自动注册。"""
    if getattr(sys, "frozen", False):
        return None
    root = _project_root()
    launcher = root / "run_gui.pyw"
    if not launcher.exists():
        return None
    pythonw = root / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        fallback = Path(sys.executable).with_name("pythonw.exe")
        pythonw = fallback if fallback.exists() else Path(sys.executable)
    return pythonw, launcher


def menu_key_path(base: str) -> str:
    """把 Classes 下的对象路径转换成菜单键路径。

    DesktopBackground\\Shell、CLSID\\...\\shell 等本身已经是菜单容器，
    不能再追加一层 shell。
    """
    if base.lower().endswith("\\shell"):
        return base + "\\" + _MENU_NAME
    return base + "\\shell\\" + _MENU_NAME


def expected_command() -> str | None:
    paths = _launcher_paths()
    if paths is None:
        return None
    pythonw, launcher = paths
    return f'"{pythonw}" "{launcher}" --auto-diagnose "%1"'


def is_registered() -> bool:
    """检查 11 个入口是否存在，并确认“任意文件”命令指向当前目录。"""
    if sys.platform != "win32":
        return False
    expected = expected_command()
    if expected is None:
        return False
    try:
        import winreg
    except ImportError:
        return False

    try:
        for base in _MENU_PATHS:
            key_path = r"Software\Classes" + "\\" + menu_key_path(base) + r"\command"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                if base != r"*":
                    continue
                value, _ = winreg.QueryValueEx(key, "")
                if os.path.normcase(str(value)).strip() != os.path.normcase(expected).strip():
                    return False
        return True
    except OSError:
        return False


def _log(message: str) -> None:
    try:
        path = logs_dir() / "context-menu.log"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(message.rstrip() + "\n")
    except OSError:
        pass


def ensure_registered() -> tuple[bool, str]:
    """缺失/过期时运行安装脚本；返回 (是否已注册, 说明)。"""
    if sys.platform != "win32":
        return False, "仅 Windows 支持右键菜单"
    if is_registered():
        return True, "右键菜单已指向当前目录"
    if _launcher_paths() is None:
        return True, "当前启动方式跳过自动注册（冻结版请手动安装菜单）"

    script = _project_root() / "scripts" / "install_context_menu.ps1"
    if not script.exists():
        return False, "未找到 scripts/install_context_menu.ps1"

    try:
        completed = subprocess.run(
            [
                powershell_executable(),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
            ],
            cwd=str(_project_root()),
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return False, f"右键菜单注册失败：{exc}"

    if completed.returncode != 0:
        detail = completed.stderr.decode("mbcs", errors="replace").strip()
        return False, f"右键菜单注册脚本退出码 {completed.returncode}：{detail}"
    if not is_registered():
        return False, "右键菜单脚本已执行，但校验仍未通过"
    return True, "右键菜单已自动注册"


def ensure_registered_in_background() -> None:
    """在后台线程执行，避免拖慢 GUI 启动。"""

    def _run() -> None:
        ok, detail = ensure_registered()
        _log(f"{'OK' if ok else 'WARN'} {detail}")

    threading.Thread(target=_run, name="whp-context-menu", daemon=True).start()
