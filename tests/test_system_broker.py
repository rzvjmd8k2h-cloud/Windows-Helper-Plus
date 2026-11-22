"""SYSTEM 特权只读代理：客户端协议测试与 PowerShell 代理冒烟测试。"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from helper_plus.system_broker_client import (
    SystemBrokerClient,
    current_user_sid,
)


def _prepare_root(root: Path) -> tuple[Path, Path, Path]:
    requests = root / "requests"
    responses = root / "responses"
    logs = root / "logs"
    for directory in (requests, responses, logs):
        directory.mkdir(parents=True, exist_ok=True)
    return requests, responses, logs


def _heartbeat(responses: Path, identity: str = r"NT AUTHORITY\SYSTEM") -> None:
    payload = {
        "version": 1,
        "service": "WindowsHelperPlusSystemBroker",
        "identity": identity,
        "sid": "S-1-5-18",
        "pid": 4321,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "processed": 3,
        "computer": "TESTPC",
    }
    (responses / "broker-status.json").write_text(json.dumps(payload), encoding="utf-8")


def test_probe_missing_and_stale(tmp_path):
    assert not SystemBrokerClient(root=tmp_path / "missing").probe().available
    requests, responses, logs = _prepare_root(tmp_path / "broker")
    _heartbeat(responses)
    info = SystemBrokerClient(root=tmp_path / "broker").probe()
    assert info.available and info.is_system
    assert info.identity.endswith("SYSTEM")
    # 非 SYSTEM 身份不可用。
    _heartbeat(responses, identity=r"TESTPC\Administrator")
    assert not SystemBrokerClient(root=tmp_path / "broker").probe().available
    # 过期心跳不可用。
    stale = {
        "version": 1,
        "identity": r"NT AUTHORITY\SYSTEM",
        "updated_at": "2000-01-01T00:00:00+00:00",
        "sid": "S-1-5-18",
        "pid": 1,
        "processed": 0,
    }
    (responses / "broker-status.json").write_text(json.dumps(stale), encoding="utf-8")
    assert not SystemBrokerClient(root=tmp_path / "broker").probe().available


def test_client_request_response_roundtrip(tmp_path):
    root = tmp_path / "broker"
    requests, responses, logs = _prepare_root(root)
    _heartbeat(responses)
    client = SystemBrokerClient(root=root)
    result_payload = {
        "tool": "system_broker_inspect",
        "ok": True,
        "summary": "SYSTEM broker: test.dll",
        "source": "Windows Helper Plus SYSTEM Broker",
        "data": {"sha256": "a" * 64, "signature_status": "Valid", "owner": "NT SERVICE\\TrustedInstaller"},
    }
    errors: list[Exception] = []

    def responder() -> None:
        deadline = time.monotonic() + 10
        try:
            while time.monotonic() < deadline:
                candidates = sorted(requests.glob("req-*.json"))
                if not candidates:
                    time.sleep(0.02)
                    continue
                request = json.loads(candidates[0].read_text(encoding="utf-8"))
                response = {
                    "version": 1,
                    "nonce": request["nonce"],
                    "ok": True,
                    "broker": {"identity": r"NT AUTHORITY\SYSTEM", "sid": "S-1-5-18", "pid": 99},
                    "results": [result_payload],
                    "error": None,
                }
                (responses / f"resp-{request['nonce']}.json").write_text(
                    json.dumps(response), encoding="utf-8"
                )
                candidates[0].unlink(missing_ok=True)
                return
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=responder, daemon=True)
    thread.start()
    response = client.collect_privileged_evidence(r"C:\Windows\System32\kernel32.dll", timeout_seconds=10)
    thread.join(timeout=2)
    assert not errors
    assert response["results"][0]["tool"] == "system_broker_inspect"
    assert client.probe().available


def test_current_user_sid_looks_valid():
    sid = current_user_sid()
    if sys.platform == "win32":
        assert sid.startswith("S-1-")


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell broker 仅 Windows")
def test_powershell_broker_script_once(tmp_path):
    root = tmp_path / "broker"
    requests, responses, logs = _prepare_root(root)
    target = tmp_path / "broker-target.dll"
    target.write_bytes(b"helper plus system broker test")
    nonce = "0123456789abcdef0123456789abcdef"
    request = {
        "version": 1,
        "nonce": nonce,
        "operation": "inspect_target",
        "target": str(target),
    }
    (requests / f"req-{nonce}.json").write_text(json.dumps(request), encoding="utf-8")
    script = Path(__file__).resolve().parents[1] / "scripts" / "system_broker.ps1"
    completed = subprocess.run(
        [
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-DataRoot",
            str(root),
            "-Once",
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert completed.returncode == 0, completed.stderr[:1000]
    response_path = responses / f"resp-{nonce}.json"
    assert response_path.exists()
    response = json.loads(response_path.read_text(encoding="utf-8"))
    assert response["ok"] is True, response.get("error")
    evidence = response["results"][0]
    assert evidence["tool"] == "system_broker_inspect"
    data = evidence["data"]
    expected = hashlib.sha256(target.read_bytes()).hexdigest()
    assert data["sha256"] == expected, (
        f"sha256_error={data.get('sha256_error')!r} "
        f"evidence_ok={evidence.get('ok')!r} "
        f"signature={data.get('signature_status')!r} "
        f"summary={evidence.get('summary')!r}"
    )
    assert not (requests / f"req-{nonce}.json").exists()
