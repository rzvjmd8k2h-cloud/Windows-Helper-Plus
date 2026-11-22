"""诊断对象（文件系统路径、设备、打印机、系统）的统一描述。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
from typing import Any


@dataclass(frozen=True)
class DiagnosisTarget:
    kind: str  # file | directory | drive | system | device | printer
    display: str
    path: Path | None = None
    device_id: str | None = None
    printer_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def is_filesystem(self) -> bool:
        return self.kind in {"file", "directory", "drive"}

    @property
    def is_file(self) -> bool:
        return self.kind == "file" and self.path is not None

    @property
    def is_directory(self) -> bool:
        return self.kind in {"directory", "drive"} and self.path is not None

    @property
    def exists(self) -> bool:
        return self.path is not None and self.path.exists()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "display": self.display,
            "path": str(self.path) if self.path else None,
            "device_id": self.device_id,
            "printer_name": self.printer_name,
            "metadata": self.metadata,
        }

    @classmethod
    def system(cls) -> "DiagnosisTarget":
        return cls(kind="system", display="本机系统")

    @classmethod
    def from_path(cls, raw: str | Path) -> "DiagnosisTarget":
        text = str(raw).strip().strip('"')
        if not text:
            raise ValueError("目标路径为空。")
        if "\x00" in text or len(text) > 1024:
            raise ValueError("目标路径无效。")
        path = Path(text).expanduser()
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"目标不存在或不可访问：{exc}") from exc
        if resolved.is_file():
            return cls(kind="file", display=str(resolved), path=resolved)
        if resolved.is_dir():
            # 盘符根目录单独标记，便于前端/工具选择盘健康检查。
            anchor = resolved.anchor
            is_drive = bool(anchor) and os.path.normcase(str(resolved)) == os.path.normcase(anchor)
            return cls(
                kind="drive" if is_drive else "directory",
                display=str(resolved),
                path=resolved,
            )
        raise ValueError("目标不是文件或目录。")

    @classmethod
    def device(cls, device_id: str, name: str = "", metadata: dict[str, Any] | None = None) -> "DiagnosisTarget":
        label = name or device_id
        return cls(
            kind="device",
            display=f"设备：{label}",
            device_id=device_id,
            metadata=metadata or {},
        )

    @classmethod
    def printer(cls, name: str, metadata: dict[str, Any] | None = None) -> "DiagnosisTarget":
        return cls(
            kind="printer",
            display=f"打印机：{name}",
            printer_name=name,
            metadata=metadata or {},
        )


def coerce_target(value: Any) -> DiagnosisTarget:
    """把 UI/测试传入的 Path/str/None/Dict 统一转换成 DiagnosisTarget。"""
    if isinstance(value, DiagnosisTarget):
        return value
    if value is None or value == "":
        return DiagnosisTarget.system()
    if isinstance(value, Path):
        return DiagnosisTarget.from_path(value)
    if isinstance(value, str):
        return DiagnosisTarget.from_path(value)
    if isinstance(value, dict):
        kind = str(value.get("kind") or "system")
        if kind == "device":
            return DiagnosisTarget.device(str(value.get("device_id") or ""), str(value.get("display") or ""))
        if kind == "printer":
            return DiagnosisTarget.printer(str(value.get("printer_name") or value.get("display") or ""))
        if value.get("path"):
            return DiagnosisTarget.from_path(value["path"])
    raise ValueError(f"不支持的目标类型：{type(value).__name__}")
