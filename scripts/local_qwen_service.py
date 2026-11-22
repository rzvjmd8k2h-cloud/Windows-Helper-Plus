"""Windows Helper Plus 本地 Qwen 开机自启动/预检服务。

用途：
- 用户登录时启动 llama-server 并预热，使本地上下文官不必等首次分析才加载；
- 检查本地 Qwen 健康状态和待翻译日志（只报告，不阻塞主程序）；
- 可选 ``--translate-pending N`` 在后台补翻缺少 Markdown 的旧日志（N=0 表示不翻译）。

不删除任何日志；状态写入 Log/local-model/startup-status.json。
"""

from __future__ import annotations

from datetime import datetime
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONUTF8", "1")

from helper_plus.config import AppConfig, app_data_dir  # noqa: E402
from helper_plus.local_context import LocalContextAgent, log_root  # noqa: E402
from helper_plus.local_model import LocalModelClient, LocalModelError  # noqa: E402


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _status_path() -> Path:
    path = log_root() / "local-model" / "startup-status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _pending_translations() -> list[Path]:
    reports = app_data_dir() / "reports"
    runs = log_root() / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    pending = []
    for report in reports.glob("*.json"):
        target = runs / f"{report.stem}.md"
        if not target.exists() or target.stat().st_size < 80:
            pending.append(report)
    return sorted(pending, key=lambda p: p.stat().st_mtime)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--autostart", action="store_true")
    parser.add_argument("--translate-pending", type=int, default=0)
    args = parser.parse_args(argv)
    status = {
        "started_at": _now(),
        "autostart": bool(args.autostart),
        "server": None,
        "warmup": None,
        "pending_translations": [],
        "translated": [],
        "errors": [],
    }
    lock_path = log_root() / "local-model" / "startup.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        status["errors"].append("另一个本地 Qwen 启动服务已在运行")
        _status_path().write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    try:
        config = AppConfig()
        client = LocalModelClient(config, log_dir=log_root() / "local-model")
        status["server"] = client.status().__dict__
        try:
            if not client.ensure_server():
                raise LocalModelError("本地 Qwen 不可用")
            # 预热：最小 JSON 请求，让模型权重/上下文进入内存。
            start = time.monotonic()
            response = client.chat(
                system="只输出 JSON。 /no_think",
                user='输出 {"ok": true} /no_think',
                max_tokens=40,
                temperature=0.1,
            )
            status["warmup"] = {
                "ok": True,
                "elapsed_seconds": round(time.monotonic() - start, 1),
                "preview": response[:200],
            }
        except Exception as exc:  # noqa: BLE001
            status["errors"].append(f"{type(exc).__name__}: {exc}")
        pending = _pending_translations()
        status["pending_translations"] = [p.stem for p in pending]
        if args.translate_pending > 0 and pending:
            agent = LocalContextAgent(client, root=log_root())
            for report in pending[: args.translate_pending]:
                try:
                    record = json.loads(report.read_text(encoding="utf-8"))
                    markdown, error = agent.translate_record(record, None)
                    status["translated"].append(
                        {"record_id": report.stem, "chars": len(markdown), "error": error}
                    )
                except Exception as exc:  # noqa: BLE001
                    status["errors"].append(f"{report.stem}: {type(exc).__name__}: {exc}")
        status["finished_at"] = _now()
        _status_path().write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
