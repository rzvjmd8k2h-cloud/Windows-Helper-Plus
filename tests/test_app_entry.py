"""GUI 入口导入路径与启动流程测试（不真正进入事件循环）。"""

from __future__ import annotations

import importlib.util

import pytest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("PySide6") is None, reason="需要 PySide6"
)


def test_main_resolves_gui_modules_and_starts(monkeypatch, tmp_path):
    """回归测试：早期版本在 helper_plus/ui/app.py 中使用 .ui.main_window 错误路径。"""
    import helper_plus.ui.app as app_module
    import helper_plus.ui.main_window as main_window_module
    import helper_plus.ui.theme as theme_module

    class FakeApp:
        def exec(self) -> int:
            return 0

    created: list[FakeWindow] = []

    class FakeWindow:
        def __init__(self, config, initial_target=None, auto_diagnose=False, pick_target=None):
            self.config = config
            self.initial_target = initial_target
            self.auto_diagnose = auto_diagnose
            self.pick_target = pick_target
            self.shown = False
            created.append(self)

        def show(self) -> None:
            self.shown = True

    monkeypatch.setattr(theme_module, "create_application", lambda argv: FakeApp())
    monkeypatch.setattr(main_window_module, "MainWindow", FakeWindow)
    monkeypatch.setattr(app_module, "_install_gui_exception_hook", lambda: None)
    monkeypatch.setattr(app_module, "_set_windows_app_id", lambda: None)

    assert app_module.main([]) == 0
    assert created[0].shown is True
    assert created[0].auto_diagnose is False


def test_context_menu_flag_passes_target_and_auto_diagnose(monkeypatch):
    import helper_plus.ui.app as app_module
    import helper_plus.ui.main_window as main_window_module
    import helper_plus.ui.theme as theme_module

    created = []

    class FakeApp:
        def exec(self) -> int:
            return 0

    class FakeWindow:
        def __init__(self, config, initial_target=None, auto_diagnose=False, pick_target=None):
            created.append((initial_target, auto_diagnose, pick_target))

        def show(self) -> None:
            pass

    monkeypatch.setattr(theme_module, "create_application", lambda argv: FakeApp())
    monkeypatch.setattr(main_window_module, "MainWindow", FakeWindow)
    monkeypatch.setattr(app_module, "_install_gui_exception_hook", lambda: None)
    monkeypatch.setattr(app_module, "_set_windows_app_id", lambda: None)

    target = r"C:\Windows\System32\kernel32.dll"
    assert app_module.main(["--auto-diagnose", target]) == 0
    assert created == [(target, True, None)]
