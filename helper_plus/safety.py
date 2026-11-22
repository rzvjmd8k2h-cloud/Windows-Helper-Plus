"""安全边界：路径范围、参数校验、拒绝任意命令执行。

本原型（阶段 1）只允许只读取证。所谓"安全"包含两层：
1. Tool 层只暴露固定的 Windows 原生查询；
2. Planner 只能从 ToolRegistry 白名单选择工具，参数被逐项校验。
"""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any, Iterable


class SafetyError(ValueError):
    """参数或路径越过安全边界。"""


_ILLEGAL_NAME_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


def validate_user_target(raw: str | None, *, allow_directory: bool = False) -> Path | None:
    """校验用户在 UI 中选择的目标路径。

    ``allow_directory=False`` 保留旧的单文件语义（部分测试/调用方使用）；
    UI 和右键入口使用 ``allow_directory=True`` 以支持文件夹、驱动器诊断。
    """
    text = (raw or "").strip().strip('"')
    if not text:
        return None
    if "\x00" in text:
        raise SafetyError("目标路径包含非法字符。")
    if len(text) > 1024:
        raise SafetyError("目标路径过长（限制 1024 个字符）。")
    path = Path(text).expanduser()
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SafetyError(f"目标不存在或不可访问：{exc}") from exc
    if path.is_dir():
        if allow_directory:
            return path
        raise SafetyError("该操作需要单个文件目标；请选择具体文件。")
    if not path.is_file():
        raise SafetyError("目标不是普通文件或目录。")
    return path


def validate_directory_path(raw: str | None, target: Path | None) -> Path:
    """目录检查默认放行任意已存在目录（最高权限/信任模式）。

    不在这里做“只能访问选中目录”的程序自我拦截；工具仍是只读枚举。
    strict 模式可在 Orchestrator/工具层预先限制，而不是拒绝所有调用。
    """
    if raw is None or not str(raw).strip():
        if target is None or not target.is_dir():
            raise SafetyError("未提供目录路径，且当前诊断目标不是目录。")
        return target
    text = str(raw).strip().strip('"')
    if not text or len(text) > 1024 or "\x00" in text:
        raise SafetyError("目录路径无效。")
    path = Path(text).expanduser()
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SafetyError(f"目录不存在或不可访问：{exc}") from exc
    if not path.is_dir():
        raise SafetyError("目标不是目录。")
    return path


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def is_under(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([_norm(path), _norm(root)]) == _norm(root)
    except ValueError:
        return False


def validate_inspect_path(raw: str | None, target: Path | None) -> Path:
    """检查文件路径；默认允许任意绝对存在的文件（只读）。

    最高权限/信任模式下不做 Windows 目录/目标目录的程序自我拦截；
    路径仍然必须是绝对、存在、普通文件。
    """
    if raw is None or not str(raw).strip():
        if target is None:
            raise SafetyError("未提供要检查的文件路径，且当前诊断没有目标。")
        path = target
    else:
        text = str(raw).strip().strip('"')
        if not text:
            raise SafetyError("文件路径为空。")
        if len(text) > 1024:
            raise SafetyError("文件路径过长。")
        path = Path(text).expanduser()
        try:
            path = path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise SafetyError(f"路径无法解析：{exc}") from exc

    if not path.is_absolute():
        raise SafetyError("只接受绝对路径。")

    if not path.is_file():
        raise SafetyError(f"目标不是可读文件：{path}")
    return path


def require_choice(value: Any, allowed: Iterable[str], field_name: str) -> str:
    allowed_set = {str(item) for item in allowed}
    text = str(value or "").strip()
    if text not in allowed_set:
        raise SafetyError(f"{field_name} 必须是以下之一：{', '.join(sorted(allowed_set))}")
    return text


def require_int(value: Any, field_name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise SafetyError(f"{field_name} 必须是整数。")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise SafetyError(f"{field_name} 必须是整数。") from exc
    if number < minimum or number > maximum:
        raise SafetyError(f"{field_name} 必须在 {minimum} 到 {maximum} 之间。")
    return number


def require_text(
    value: Any,
    field_name: str,
    *,
    max_length: int = 200,
    required: bool = True,
    pattern: str | None = None,
) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise SafetyError(f"{field_name} 不能为空。")
    if len(text) > max_length:
        raise SafetyError(f"{field_name} 长度不能超过 {max_length}。")
    if pattern and text and not re.fullmatch(pattern, text):
        raise SafetyError(f"{field_name} 格式不合法。")
    return text


def ensure_no_unknown_keys(args: dict[str, Any], allowed: Iterable[str], field_name: str = "arguments") -> None:
    unknown = set(args) - set(allowed)
    if unknown:
        raise SafetyError(f"{field_name} 包含不支持的字段：{', '.join(sorted(unknown))}")
