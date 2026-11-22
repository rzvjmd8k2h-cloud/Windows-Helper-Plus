"""Client for the optional SYSTEM-only read-only evidence broker.

The GUI and Planner stay at the normal user's privilege level.  If the
administrator-installed scheduled task is running, this client asks it to
perform a fixed, whitelisted inspection of the selected file as
``NT AUTHORITY\\SYSTEM`` and receives structured evidence through a file
queue in ProgramData.

Protocol is deliberately small:
  request:  {version, nonce, operation: "inspect_target", target}
  response: {version, nonce, ok, broker: {identity, sid, ...}, results, error}
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any

from . import __version__
from .config import app_data_dir  # noqa: F401  (kept for easy debugging hooks)
from .windows import run_process


BROKER_TASK_NAME = "WindowsHelperPlusSystemBroker"
BROKER_SERVICE_NAME = "WindowsHelperPlusSystemBroker"
_BROKER_ROOT_ENV = "HELPER_PLUS_SYSTEM_BROKER_ROOT"
_SYSTEM_IDENTITY_RE = re.compile(r"(?i)(^|\\)SYSTEM$")
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_HEARTBEAT_AGE = 30.0


class BrokerError(RuntimeError):
    pass


class BrokerUnavailable(BrokerError):
    pass


class BrokerTimeout(BrokerError):
    pass


@dataclass(frozen=True)
class BrokerInfo:
    available: bool
    identity: str = ""
    sid: str = ""
    pid: int | None = None
    processed: int = 0
    last_seen: datetime | None = None
    detail: str = ""
    root: str = ""

    @property
    def is_system(self) -> bool:
        return bool(_SYSTEM_IDENTITY_RE.search(self.identity or ""))


def broker_root() -> Path:
    override = os.environ.get(_BROKER_ROOT_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    program_data = os.environ.get("ProgramData") or r"C:\ProgramData"
    return Path(program_data) / "WindowsHelperPlus" / "SystemBroker"


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # .NET "o" format can carry 7 fractional digits; trim to 6 for older Python.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def current_user_sid() -> str:
    """Best-effort SID of the interactive user, for installer ACLs."""
    try:
        result = run_process(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            timeout=5,
        )
        match = re.search(r'"(S-1-5-[0-9-]+)"', result.stdout)
        if match:
            return match.group(1)
    except Exception:  # noqa: BLE001
        pass
    return ""


class SystemBrokerClient:
    def __init__(
        self,
        root: Path | str | None = None,
        *,
        timeout_seconds: float = 20.0,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.root = Path(root).expanduser() if root else broker_root()
        self.requests_dir = self.root / "requests"
        self.responses_dir = self.root / "responses"
        self.logs_dir = self.root / "logs"
        self.status_path = self.responses_dir / "broker-status.json"
        self.timeout_seconds = max(2.0, min(float(timeout_seconds), 60.0))
        self.cancel_event = cancel_event or threading.Event()

    # ------------------------------------------------------------------ status
    def probe(self) -> BrokerInfo:
        if not self.status_path.exists():
            if not self.root.exists():
                detail = "SYSTEM 代理未安装（未找到 ProgramData 代理目录）"
            elif not self.responses_dir.exists():
                detail = "SYSTEM 代理目录不完整"
            else:
                detail = "SYSTEM 代理未启动（没有心跳文件）"
            return BrokerInfo(available=False, detail=detail, root=str(self.root))
        try:
            raw = self.status_path.read_text(encoding="utf-8-sig")
            data = json.loads(raw)
        except (OSError, ValueError) as exc:
            return BrokerInfo(available=False, detail=f"无法读取代理心跳：{exc}", root=str(self.root))
        if not isinstance(data, dict):
            return BrokerInfo(available=False, detail="代理心跳格式无效", root=str(self.root))
        identity = str(data.get("identity") or "")
        updated = _parse_iso(data.get("updated_at"))
        now = datetime.now(timezone.utc)
        if not _SYSTEM_IDENTITY_RE.search(identity):
            return BrokerInfo(
                available=False,
                identity=identity,
                detail=f"代理身份不是 SYSTEM（当前：{identity or '未知'}）",
                root=str(self.root),
            )
        if updated is None:
            return BrokerInfo(available=False, identity=identity, detail="代理心跳时间缺失", root=str(self.root))
        age = (now - updated.astimezone(timezone.utc)).total_seconds()
        if age > _MAX_HEARTBEAT_AGE:
            return BrokerInfo(
                available=False,
                identity=identity,
                detail=f"代理心跳已过期（{age:.0f} 秒）",
                root=str(self.root),
            )
        return BrokerInfo(
            available=True,
            identity=identity,
            sid=str(data.get("sid") or ""),
            pid=int(data["pid"]) if isinstance(data.get("pid"), int) else None,
            processed=int(data["processed"]) if isinstance(data.get("processed"), int) else 0,
            last_seen=updated,
            detail="SYSTEM 特权只读代理在线",
            root=str(self.root),
        )

    # --------------------------------------------------------------- evidence
    def collect_privileged_evidence(
        self,
        target: str | Path,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        info = self.probe()
        if not info.available:
            raise BrokerUnavailable(info.detail)
        target_text = str(target)
        if not target_text or len(target_text) > 1024:
            raise BrokerError("SYSTEM 代理目标路径无效。")
        if not self.requests_dir.is_dir() or not self.responses_dir.is_dir():
            raise BrokerUnavailable("SYSTEM 代理请求/响应目录不可用。")
        nonce = secrets.token_hex(16)
        request_path = self.requests_dir / f"req-{nonce}.json"
        response_path = self.responses_dir / f"resp-{nonce}.json"
        request = {
            "version": 1,
            "nonce": nonce,
            "operation": "inspect_target",
            "target": target_text,
            "requested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "client": f"WindowsHelperPlus/{__version__}",
        }
        try:
            self._write_request(request_path, request)
            response = self._wait_response(response_path, nonce, timeout_seconds or self.timeout_seconds)
        finally:
            try:
                request_path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            response_path.unlink(missing_ok=True)
        except OSError:
            pass
        if not isinstance(response, dict) or response.get("nonce") != nonce:
            raise BrokerError("SYSTEM 代理响应 nonce 不匹配。")
        broker = response.get("broker") or {}
        identity = str(broker.get("identity") or "") if isinstance(broker, dict) else ""
        if not _SYSTEM_IDENTITY_RE.search(identity):
            raise BrokerError(f"SYSTEM 代理响应身份异常：{identity or '未知'}")
        if not response.get("ok"):
            raise BrokerError(f"SYSTEM 代理拒绝请求：{response.get('error') or '未知错误'}")
        results = response.get("results") or []
        if not isinstance(results, list) or not results:
            raise BrokerError("SYSTEM 代理没有返回证据。")
        return response

    def _write_request(self, path: Path, payload: dict[str, Any]) -> None:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        tmp = path.with_name(path.name + ".tmp")
        try:
            with tmp.open("x", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise BrokerUnavailable(f"无法写入 SYSTEM 代理请求目录：{exc}") from exc

    def _wait_response(self, path: Path, nonce: str, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + max(2.0, timeout)
        while time.monotonic() < deadline:
            if self.cancel_event.is_set():
                raise BrokerError("SYSTEM 代理请求已取消。")
            if path.exists():
                try:
                    if path.stat().st_size > _MAX_RESPONSE_BYTES:
                        raise BrokerError("SYSTEM 代理响应过大，已拒绝。")
                    data = json.loads(path.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError) as exc:
                    raise BrokerError(f"无法读取 SYSTEM 代理响应：{exc}") from exc
                if not isinstance(data, dict):
                    raise BrokerError("SYSTEM 代理响应格式无效。")
                if data.get("nonce") != nonce:
                    # 旧文件/错误文件，删除并继续等正确响应。
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                else:
                    return data
            time.sleep(0.1)
        raise BrokerTimeout(f"等待 SYSTEM 代理响应超时（{timeout:.0f} 秒）。")

    def status_text(self) -> str:
        info = self.probe()
        if info.available:
            return f"SYSTEM 代理：已连接（{info.identity}）"
        return f"SYSTEM 代理：未就绪（{info.detail}）"

    @staticmethod
    def format_age(info: BrokerInfo) -> str:
        if not info.last_seen:
            return "—"
        age = datetime.now(timezone.utc) - info.last_seen.astimezone(timezone.utc)
        if age < timedelta(seconds=5):
            return "刚刚"
        return f"{int(age.total_seconds())} 秒前"
