"""Windows 版本/兼容性自检与运行环境检查。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import os
from pathlib import Path
import platform
import shutil
import sys
from typing import Any

from .config import AppConfig, PACKAGE_ROOT
from .local_context import log_root


@dataclass
class OSInfo:
    product_name: str
    display_version: str
    build: int
    architecture: str
    windows_label: str
    supported: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "product_name": self.product_name,
            "display_version": self.display_version,
            "build": self.build,
            "architecture": self.architecture,
            "windows_label": self.windows_label,
            "supported": self.supported,
            "reason": self.reason,
        }


def classify_windows_build(build: int) -> tuple[str, bool, str]:
    """根据 build 号判断 Windows 10/11；兼容 Win11 注册表仍报 Windows 10 的情况。"""
    try:
        number = int(build)
    except (TypeError, ValueError):
        return "未知 Windows", False, "无法读取 Windows build 号"
    if number >= 22000:
        return "Windows 11", True, "Windows 11（build ≥ 22000）"
    if number >= 10240:
        return "Windows 10", True, "Windows 10（build ≥ 10240）"
    return "不受支持的 Windows", False, "仅支持 Windows 10/11（build ≥ 10240）"


def _registry_string(root, path: str, name: str, default: str = "") -> str:
    try:
        import winreg

        with winreg.OpenKey(getattr(winreg, root), path) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value or default)
    except Exception:  # noqa: BLE001
        return default


def get_os_info() -> OSInfo:
    build = 0
    try:
        build = int(sys.getwindowsversion().build)
    except Exception:  # noqa: BLE001
        build = 0
    product = _registry_string(
        "HKEY_LOCAL_MACHINE",
        r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
        "ProductName",
        platform.system(),
    )
    display = _registry_string(
        "HKEY_LOCAL_MACHINE",
        r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
        "DisplayVersion",
        "",
    )
    label, supported, reason = classify_windows_build(build)
    arch = platform.machine() or os.environ.get("PROCESSOR_ARCHITECTURE", "unknown")
    supported = bool(supported and "64" in arch)
    if supported is False and "64" not in arch:
        reason = "需要 64 位 Windows"
    return OSInfo(
        product_name=product or platform.platform(),
        display_version=display,
        build=build,
        architecture=arch,
        windows_label=label,
        supported=supported,
        reason=reason,
    )


def _check(name: str, ok: bool, detail: str, *, warning: bool = False) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail, "warning": warning}


def collect_self_check(config: AppConfig | None = None) -> dict[str, Any]:
    config = config or AppConfig()
    os_info = get_os_info()
    checks: list[dict[str, Any]] = []
    checks.append(
        _check(
            "Windows 版本适配",
            os_info.supported,
            f"{os_info.windows_label} {os_info.build} / {os_info.architecture}：{os_info.reason}",
        )
    )
    py_ok = sys.version_info >= (3, 10)
    checks.append(
        _check(
            "Python",
            py_ok,
            f"{platform.python_version()}（要求 ≥3.10）",
        )
    )
    try:
        import PySide6

        qt_ok = True
        qt_detail = f"PySide6 {PySide6.__version__}"
    except Exception as exc:  # noqa: BLE001
        qt_ok = False
        qt_detail = f"PySide6 不可用：{exc}"
    checks.append(_check("Qt UI 运行时", qt_ok, qt_detail))

    server = Path(getattr(config, "local_model_server_exe", ""))
    model = Path(getattr(config, "local_model_path", ""))
    server_ok = server.is_file()
    model_ok = model.is_file() and model.stat().st_size > 100 * 1024 * 1024
    checks.append(_check("本地 Qwen llama-server", server_ok, str(server)))
    checks.append(
        _check(
            "本地 Qwen 模型",
            model_ok,
            f"{model}（{model.stat().st_size / (1024**3):.2f} GB）" if model.exists() else str(model),
        )
    )
    try:
        free_gb = shutil.disk_usage(PACKAGE_ROOT).free / (1024**3)
    except OSError:
        free_gb = 0.0
    checks.append(
        _check(
            "磁盘空间",
            free_gb >= 2.0,
            f"应用所在盘可用 {free_gb:.1f} GB（建议 ≥2 GB）",
            warning=True,
        )
    )
    try:
        from .secrets import get_api_key

        key_ok = bool(get_api_key("").strip())
    except Exception:  # noqa: BLE001
        key_ok = False
    checks.append(
        _check(
            "DeepSeek API Key",
            key_ok,
            "已配置（内容不显示）" if key_ok else "未配置；所有 AI 分析必须配置 Key",
            warning=True,
        )
    )
    try:
        from .autostart import autostart_enabled

        auto_ok = autostart_enabled()
    except Exception:  # noqa: BLE001
        auto_ok = False
    checks.append(
        _check(
            "本地 Qwen 开机自启动",
            auto_ok,
            "已安装 Startup 快捷方式" if auto_ok else "未安装；可在设置或 scripts/install_local_qwen_autostart.ps1 安装",
            warning=True,
        )
    )
    try:
        log_root().mkdir(parents=True, exist_ok=True)
        probe = log_root() / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        log_ok = True
    except OSError as exc:
        log_ok = False
    checks.append(_check("Log 目录可写", log_ok, str(log_root())))
    try:
        from .sandbox import detect_sandbox_backends

        caps = detect_sandbox_backends()
        backend = caps.get("recommended")
        windows_capable = bool(caps.get("usable_for_windows_driver"))
        backend_detail = (
            f"推荐 {backend}；可用：{'、'.join(caps.get('available') or []) or '无'}"
            f"{'；可覆盖 Windows 驱动/系统' if windows_capable else '；无 Windows 驱动级后端（WSL 仅 Linux 环境，不能完整兜底）'}"
            if backend
            else "未检测到 Windows Sandbox/Hyper-V/VirtualBox/VMware；沙箱兜底需用户启用或提供 VM"
        )
        sandbox_ok = windows_capable
    except Exception as exc:  # noqa: BLE001
        backend_detail = f"沙箱能力检测失败：{exc}"
        sandbox_ok = False
    checks.append(
        _check(
            "沙箱后端",
            sandbox_ok,
            backend_detail,
            warning=True,
        )
    )
    hard_fail = [item for item in checks if not item["ok"] and not item["warning"]]
    return {
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "os": os_info.to_dict(),
        "checks": checks,
        "passed": not hard_fail,
        "hard_failures": [item["name"] for item in hard_fail],
    }


def format_self_check_text(result: dict[str, Any]) -> str:
    lines = [
        "Windows Helper Plus 自检",
        "",
        f"检查时间：{result.get('checked_at', '')}",
        f"系统：{result.get('os', {}).get('windows_label', '')} "
        f"{result.get('os', {}).get('build', '')} / {result.get('os', {}).get('architecture', '')}",
        "",
    ]
    for item in result.get("checks") or []:
        mark = "✓" if item.get("ok") else ("!" if item.get("warning") else "✗")
        lines.append(f"[{mark}] {item.get('name')}：{item.get('detail')}")
    lines.extend(["", "结论：" + ("通过" if result.get("passed") else "存在阻断项：" + "、".join(result.get("hard_failures") or []))])
    return "\n".join(lines)
