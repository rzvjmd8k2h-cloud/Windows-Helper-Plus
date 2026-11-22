"""DeepSeek 客户端：请求体、SSE 解析与本地假服务器集成测试。"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from helper_plus.config import AppConfig
from helper_plus.llm.client import (
    DeepSeekClient,
    LLMHTTPError,
    LLMStreamResult,
    build_request_body,
    parse_sse_payload,
)


def test_request_body_uses_latest_model_and_max_thinking():
    config = AppConfig(model="deepseek-flash", reasoning_effort="max", thinking=True)
    body = build_request_body(config, [{"role": "user", "content": "json"}])
    assert body["model"] == "deepseek-flash"
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "max"
    assert body["response_format"] == {"type": "json_object"}
    assert body["stream"] is True
    assert "temperature" not in body
    # 用户要求取消 token 上限：默认 0 时不能发送 max_tokens。
    assert "max_tokens" not in body


def test_parse_sse_payload():
    assert parse_sse_payload("[DONE]") == {"__done__": True}
    assert parse_sse_payload("") is None
    assert parse_sse_payload('{"choices":[]}') == {"choices": []}
    with pytest.raises(Exception):
        parse_sse_payload("not-json")


class _ServerThread:
    def __init__(self, handler_cls):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.requests: list[dict] = []
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _make_handler(requests, *, status=200):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            requests.append({"path": self.path, "body": json.loads(raw.decode("utf-8"))})
            if status != 200:
                payload = json.dumps({"error": {"message": "invalid api key"}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.end_headers()
            events = [
                {
                    "id": "mock-1",
                    "choices": [
                        {"delta": {"reasoning_content": "先思考。"}, "finish_reason": None}
                    ],
                },
                {
                    "id": "mock-1",
                    "choices": [
                        {
                            "delta": {
                                "content": '{"narration":"测试 [E1]",'
                                '"action":"final",'
                                '"basis":["E1"],'
                                '"final":{}}'
                            },
                            "finish_reason": None,
                        }
                    ],
                },
                {"id": "mock-1", "choices": [], "usage": {"total_tokens": 42}},
            ]
            for event in events:
                self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        def log_message(self, format, *args):  # noqa: A002
            return

    return Handler


def test_stream_against_local_sse_server(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    requests: list[dict] = []
    server = _ServerThread(_make_handler(requests))
    try:
        config = AppConfig(base_url=server.base_url)
        reasoning: list[str] = []
        content: list[str] = []
        client = DeepSeekClient(config, "sk-test", threading.Event())
        result = client.stream(
            [{"role": "user", "content": "json"}],
            on_reasoning=reasoning.append,
            on_content=content.append,
        )
    finally:
        server.close()
    assert isinstance(result, LLMStreamResult)
    assert result.reasoning == "先思考。"
    assert result.usage["total_tokens"] == 42
    assert "".join(content).startswith('{"narration"')
    assert requests[0]["path"] == "/chat/completions"
    assert requests[0]["body"]["model"] == "deepseek-flash"
    assert requests[0]["body"]["reasoning_effort"] == "max"


def test_stream_auto_continues_after_finish_reason_length(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    bodies: list[dict] = []
    calls = {"n": 0}
    client = DeepSeekClient(AppConfig(), "sk-test", threading.Event())

    def fake_stream_once(data, on_reasoning, on_content):
        calls["n"] += 1
        bodies.append(json.loads(data.decode("utf-8")))
        if calls["n"] == 1:
            on_content('{"cause":')
            return LLMStreamResult(
                content='{"cause":', reasoning="先思考", finish_reason="length"
            )
        on_content('"done"}')
        return LLMStreamResult(content='"done"}', finish_reason="stop")

    monkeypatch.setattr(client, "_stream_once", fake_stream_once)
    content: list[str] = []
    result = client.stream(
        [{"role": "user", "content": "只输出 JSON"}],
        on_content=content.append,
    )
    assert result.content == '{"cause":"done"}'
    assert result.finish_reason == "stop"
    assert "".join(content) == '{"cause":"done"}'
    assert calls["n"] == 2
    assert "max_tokens" not in bodies[0]
    assert "max_tokens" not in bodies[1]
    assert bodies[1]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in bodies[1]


def test_stream_restarts_full_json_when_initial_content_empty(monkeypatch):
    bodies: list[dict] = []
    calls = {"n": 0}
    client = DeepSeekClient(AppConfig(), "sk-test", threading.Event())

    def fake_stream_once(data, on_reasoning, on_content):
        calls["n"] += 1
        bodies.append(json.loads(data.decode("utf-8")))
        if calls["n"] == 1:
            return LLMStreamResult(content="", reasoning="思考吃满", finish_reason="length")
        on_content('{"narration":"ok"}')
        return LLMStreamResult(content='{"narration":"ok"}', finish_reason="stop")

    monkeypatch.setattr(client, "_stream_once", fake_stream_once)
    result = client.stream([{"role": "user", "content": "只输出 JSON"}])
    assert result.content == '{"narration":"ok"}'
    assert result.finish_reason == "stop"
    assert calls["n"] == 2
    assert bodies[1]["thinking"] == {"type": "disabled"}
    assert not any(message.get("role") == "assistant" for message in bodies[1]["messages"])


def test_http_error_is_reported(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    requests: list[dict] = []
    server = _ServerThread(_make_handler(requests, status=401))
    try:
        config = AppConfig(base_url=server.base_url)
        client = DeepSeekClient(config, "bad-key", threading.Event())
        with pytest.raises(LLMHTTPError) as exc_info:
            client.stream([{"role": "user", "content": "json"}])
    finally:
        server.close()
    assert exc_info.value.status == 401
    assert "invalid api key" in str(exc_info.value).lower()
    assert "bad-key" not in str(exc_info.value)
