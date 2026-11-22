"""右键菜单自动注册的纯路径/命令测试。"""

from __future__ import annotations

from helper_plus.context_menu import expected_command, menu_key_path


def test_menu_key_path_handles_shell_containers():
    assert menu_key_path(r"*") == r"*\shell\WindowsHelperPlus"
    assert menu_key_path(r"Directory\Background") == r"Directory\Background\shell\WindowsHelperPlus"
    assert menu_key_path(r"DesktopBackground\Shell") == r"DesktopBackground\Shell\WindowsHelperPlus"
    assert (
        menu_key_path(r"CLSID\{20D04FE0-3AEA-1069-A2D8-08002B30309D}\shell")
        == r"CLSID\{20D04FE0-3AEA-1069-A2D8-08002B30309D}\shell\WindowsHelperPlus"
    )


def test_expected_command_points_to_current_launcher():
    command = expected_command()
    assert command is not None
    assert "run_gui.pyw" in command
    assert "--auto-diagnose" in command
    assert '"%1"' in command
