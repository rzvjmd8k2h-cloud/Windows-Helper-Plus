"""本地审计日志（JSONL）。默认不联网、不上传。"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import threading
from typing import Any

from .config import logs_dir


class AuditLog:
    """一次诊断对应一个 JSONL 审计文件。"""

    def __init__(self, session_id: str, *, root: Path | None = None) -> None:
        directory = root or logs_dir()
        directory.mkdir(parents=True, exist_ok=True)
        date = datetime.now().strftime("%Y%m%d")
        self.path = directory / f"audit-{date}-{session_id}.jsonl"
        self.session_id = session_id
        self._lock = threading.Lock()

    def write(self, event: str, **fields: Any) -> None:
        payload = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "session": self.session_id,
            "event": event,
            **fields,
        }
        line = json.dumps(payload, ensure_ascii=False, default=str)
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                # 审计失败不能中断诊断，但调用方可在状态栏提示（本原型静默降级）。
                pass
