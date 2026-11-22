"""应用入口：PySide6 图形界面。"""

from __future__ import annotations

import argparse
import ctypes
import sys
import traceback

from .. import __version__
from ..config import AppConfig


def _set_windows_app_id() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "WindowsHelperPlus.ReadOnlyDiagnostic.0.1"
        )
    except Exception:  # noqa: BLE001
        pass


def _install_gui_exception_hook() -> None:
    """pythonw 下没有控制台，把未处理异常写入本地日志并弹窗。"""
    from PySide6.QtWidgets import QMessageBox

    from ..config import logs_dir

    def _hook(exc_type, exc, tb):  # type: ignore[no-untyped-def]
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        detail = "".join(traceback.format_exception(exc_type, exc, tb))
        log_path = ""
        try:
            path = logs_dir() / "unhandled-error.log"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(detail + "\n")
            log_path = str(path)
        except OSError:
            pass
        QMessageBox.critical(
            None,
            "Windows Helper Plus 发生错误",
            f"{exc_type.__name__}: {exc}\n\n"
            + (f"详细信息已写入：\n{log_path}" if log_path else "详细信息无法写入日志。"),
        )

    sys.excepthook = _hook


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="helper-plus",
        description="Windows Helper Plus — AI 驱动的 Windows 全对象诊断与受控修复",
    )
    parser.add_argument("path", nargs="?", help="启动时预填的目标文件/文件夹路径")
    parser.add_argument(
        "--auto-diagnose",
        action="store_true",
        help="GUI 启动后自动开始 AI 分析（供资源管理器右键菜单使用）",
    )
    parser.add_argument(
        "--pick-target",
        choices=["device", "printer"],
        help="启动后打开设备/打印机选择器（供“此电脑/打印机”右键菜单使用）",
    )
    parser.add_argument("--version", action="version", version=f"Windows Helper Plus {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # GUI 延迟导入，保证启动更轻。
    # 注意：本文件位于 helper_plus/ui/ 下，因此这里使用单点相对导入。
    try:
        from .main_window import MainWindow
        from .theme import create_application
    except ImportError as exc:
        detail = str(exc)
        if "PySide6" in detail or "shiboken6" in detail:
            message = (
                "缺少 PySide6 运行库，无法启动图形界面。\n\n"
                f"{exc}\n\n请在本项目目录运行：\n"
                "powershell -ExecutionPolicy Bypass -File .\\setup.ps1"
            )
        else:
            message = (
                "启动图形界面时发生内部导入错误。\n\n"
                f"{exc}\n\n请确认项目文件完整，或在项目目录运行：\n"
                "powershell -ExecutionPolicy Bypass -File .\\setup.ps1"
            )
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(None, message, "Windows Helper Plus", 0x10)
        else:
            print(message, file=sys.stderr)
        return 3

    _set_windows_app_id()
    config = AppConfig.load()
    from ..context_menu import ensure_registered_in_background
    from ..i18n import set_language

    set_language(config.ui_language)
    app = create_application([sys.argv[0]])
    _install_gui_exception_hook()
    window = MainWindow(
        config,
        initial_target=args.path,
        auto_diagnose=args.auto_diagnose,
        pick_target=args.pick_target,
    )
    window.show()
    # 启动后自动绑定/修复右键菜单；后台执行，不阻塞界面。
    ensure_registered_in_background()
    return app.exec()
