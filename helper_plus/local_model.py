"""本地 Qwen 上下文模型运行时（llama.cpp llama-server）。

职责边界：
- 只做上下文整理、历史筛选、状态/完成度摘要、日志翻译与病历归档；
- 不产生最终诊断结论、不决定工具调用、不执行动作；
- 主模型（DeepSeek）必须二次确认用户目标，并独立使用工具/证据生成 final。

运行条件（本机）：Windows x64、CPU、8GB RAM；模型 Qwen3-1.7B-Q8_0.gguf。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any
import urllib.error
import urllib.request

from .config import AppConfig, PACKAGE_ROOT
from .windows import CREATE_NO_WINDOW


DEFAULT_SERVER = PACKAGE_ROOT / "runtime" / "llama-b9200" / "llama-server.exe"
DEFAULT_MODEL = PACKAGE_ROOT / "models" / "Qwen3-1.7B-Q8_0.gguf"
DEFAULT_PORT = 8124


class LocalModelError(RuntimeError):
    pass


def _extract_json(text: str) -> dict[str, Any]:
    value = (text or "").strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    start = value.find("{")
    end = value.rfind("}")
    if start < 0 or end <= start:
        raise LocalModelError("本地模型没有返回 JSON 对象。")
    try:
        parsed = json.loads(value[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LocalModelError(f"本地模型 JSON 解析失败：{exc}") from exc
    if not isinstance(parsed, dict):
        raise LocalModelError("本地模型 JSON 根节点必须是对象。")
    return parsed


@dataclass
class LocalModelStatus:
    available: bool
    detail: str
    server_exe: str
    model_path: str
    port: int


class LocalModelClient:
    """llama-server 的 OpenAI 兼容客户端（仅本地 loopback）。"""

    _lock = threading.Lock()
    _process: subprocess.Popen | None = None
    _log_handle = None

    def __init__(self, config: AppConfig | None = None, log_dir: Path | None = None) -> None:
        self.config = config or AppConfig()
        self.server_exe = Path(
            getattr(self.config, "local_model_server_exe", str(DEFAULT_SERVER))
            or str(DEFAULT_SERVER)
        ).expanduser()
        self.model_path = Path(
            getattr(self.config, "local_model_path", str(DEFAULT_MODEL))
            or str(DEFAULT_MODEL)
        ).expanduser()
        self.port = int(getattr(self.config, "local_model_port", DEFAULT_PORT) or DEFAULT_PORT)
        self.context_size = int(getattr(self.config, "local_model_context", 8192) or 8192)
        self.threads = int(getattr(self.config, "local_model_threads", 6) or 6)
        self.timeout = int(getattr(self.config, "local_model_timeout_seconds", 300) or 300)
        from .config import app_data_dir

        self.log_dir = Path(log_dir) if log_dir else app_data_dir() / "local_model"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.base_url = f"http://127.0.0.1:{self.port}"

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "local_model_enabled", True))

    def health(self, timeout: float = 2.0) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            return str(payload.get("status") or "").lower() == "ok"
        except Exception:  # noqa: BLE001
            return False

    def status(self) -> LocalModelStatus:
        available = self.enabled
        detail = "本地 Qwen 未启用"
        if available:
            if self.health():
                available = True
                detail = "本地 Qwen 已在线"
            elif not self.server_exe.exists():
                available = False
                detail = f"缺少 llama-server：{self.server_exe}"
            elif not self.model_path.exists():
                available = False
                detail = f"缺少 Qwen 模型：{self.model_path}"
            else:
                available = False
                detail = "本地 Qwen 未启动（首次上下文任务会自动启动）"
        return LocalModelStatus(
            available=available,
            detail=detail,
            server_exe=str(self.server_exe),
            model_path=str(self.model_path),
            port=self.port,
        )

    def ensure_server(self) -> bool:
        if not self.enabled:
            return False
        if self.health():
            return True
        with LocalModelClient._lock:
            if self.health():
                return True
            if not self.server_exe.exists() or not self.model_path.exists():
                return False
            log_path = self.log_dir / "llama-server.log"
            try:
                handle = log_path.open("ab", buffering=0)
                args = [
                    str(self.server_exe),
                    "-m",
                    str(self.model_path),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self.port),
                    "-c",
                    str(self.context_size),
                    "-t",
                    str(self.threads),
                    "--jinja",
                    "--no-webui",
                ]
                LocalModelClient._process = subprocess.Popen(
                    args,
                    cwd=str(self.server_exe.parent),
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                LocalModelClient._log_handle = handle
            except OSError as exc:
                raise LocalModelError(f"启动本地 Qwen 失败：{exc}") from exc
            deadline = time.monotonic() + min(180, max(30, self.timeout))
            while time.monotonic() < deadline:
                if self.health(timeout=1.0):
                    return True
                proc = LocalModelClient._process
                if proc is not None and proc.poll() is not None:
                    raise LocalModelError(
                        f"本地 Qwen 进程退出（exit={proc.returncode}），日志：{log_path}"
                    )
                time.sleep(0.25)
            raise LocalModelError(f"本地 Qwen 启动超时，日志：{log_path}")

    def chat(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 900,
        temperature: float = 0.1,
        json_mode: bool = False,
    ) -> str:
        if not self.ensure_server():
            raise LocalModelError("本地 Qwen 不可用。")
        body = {
            "model": "qwen3-1.7b",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            # Qwen3 默认开启思考；上下文整理与日志翻译只需要短 JSON/Markdown，
            # 思考会耗尽 max_tokens 并导致空结果或超时，这里显式关闭。
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if json_mode:
            # 由 llama-server 的 JSON 语法约束输出，避免 1.7B 模型漏引号/逗号导致解析失败。
            body["response_format"] = {"type": "json_object"}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            raise LocalModelError(f"本地 Qwen HTTP {exc.code}：{exc.read()[:500]!r}") from exc
        except Exception as exc:  # noqa: BLE001
            raise LocalModelError(f"本地 Qwen 请求失败：{exc}") from exc
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LocalModelError(f"本地 Qwen 响应格式异常：{payload}") from exc
        return str(content or "")

    def chat_json(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 900,
        temperature: float = 0.1,
    ) -> dict[str, Any]:
        text = self.chat(
            system=system,
            user=user,
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=True,
        )
        return _extract_json(text)

    def stop(self) -> None:
        with LocalModelClient._lock:
            proc = LocalModelClient._process
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=8)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
            LocalModelClient._process = None
            handle = LocalModelClient._log_handle
            if handle is not None:
                try:
                    handle.close()
                except Exception:  # noqa: BLE001
                    pass
            LocalModelClient._log_handle = None
