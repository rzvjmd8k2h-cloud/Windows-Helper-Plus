"""开发期工具冒烟测试：直接运行所有只读工具（不经过模型）。"""

from __future__ import annotations

import json
import threading
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from helper_plus.config import AppConfig
from helper_plus.tools import create_registry, ToolContext


def main() -> None:
    config = AppConfig()
    cancel = threading.Event()
    target = Path(sys.executable) if len(sys.argv) < 2 else Path(sys.argv[1])
    ctx = ToolContext(target=target, config=config, cancel_event=cancel)
    registry = create_registry()
    calls = [
        ("get_system_info", {}),
        ("inspect_file", {}),
        ("get_event_logs", {"log": "System", "hours": 24, "max_events": 5}),
        ("get_event_logs", {"log": "Application", "hours": 24, "max_events": 5}),
        ("get_reliability_records", {"limit": 5}),
        ("get_windows_updates", {"limit": 5}),
        ("get_wer_reports", {"limit": 3}),
        ("get_driver_info", {"query": target.stem, "limit": 10}),
        ("get_setupapi_log", {"max_lines": 20}),
    ]
    for name, args in calls:
        result = registry.invoke(name, args, ctx)
        print(f"\n=== {name} ok={result.ok} {result.duration_ms}ms attempts={result.attempts} ===")
        print("SUMMARY:", result.summary)
        if result.error:
            print("ERROR:", result.error)
        preview = json.dumps(result.data, ensure_ascii=False, indent=2)
        print("DATA:", preview[:1800])


if __name__ == "__main__":
    main()
