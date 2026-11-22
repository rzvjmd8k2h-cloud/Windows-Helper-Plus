"""DeepSeek Chat Completions 流式客户端（OpenAI 兼容格式）。

使用标准库 urllib，避免额外依赖；采用 DeepSeek 当前文档的：
- ``thinking: {"type": "enabled"}``
- ``reasoning_effort: "max"``（最新 DeepSeek 模型默认支持）
- ``response_format: {"type": "json_object"}``
- 流式 SSE 的 ``reasoning_content`` / ``content`` 增量。

本模块不把 API Key 写进日志或异常文本。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import socket
import time
import threading
from typing import Any, Callable
import urllib.error
import urllib.request

from ..config import AppConfig


class LLMError(RuntimeError):
    """模型调用失败（可直接展示给用户，含可操作建议）。"""


class LLMConfigurationError(LLMError):
    """缺少 API Key / 配置错误。"""


class LLMHTTPError(LLMError):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"DeepSeek API HTTP {status}：{detail}")


@dataclass
class LLMStreamResult:
    content: str = ""
    reasoning: str = ""
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    response_id: str | None = None
    elapsed_ms: int = 0
    attempts: int = 1


ReasoningCallback = Callable[[str], None]
ContentCallback = Callable[[str], None]
AttemptCallback = Callable[[int], None]


def build_request_body(
    config: AppConfig,
    messages: list[dict[str, Any]],
    *,
    stream: bool = True,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "stream": stream,
        "response_format": {"type": "json_object"},
    }
    # 用户要求取消 token 上限：0 表示不发送 max_tokens，交给 DeepSeek 服务端默认。
    if config.max_tokens > 0:
        body["max_tokens"] = config.max_tokens
    if config.thinking:
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = config.reasoning_effort
    return body


def parse_sse_payload(payload: str) -> dict[str, Any] | None:
    """解析一条 SSE data；``[DONE]`` 返回 None。异常事件抛错。"""
    payload = payload.strip()
    if not payload:
        return None
    if payload == "[DONE]":
        return {"__done__": True}
    try:
        obj = json.loads(payload)
    except ValueError as exc:
        raise LLMError(f"无法解析模型流式响应：{exc}") from exc
    if not isinstance(obj, dict):
        raise LLMError("模型流式响应格式异常。")
    if obj.get("error"):
        error = obj["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise LLMError(f"DeepSeek API 返回错误：{message}")
    return obj


class DeepSeekClient:
    def __init__(self, config: AppConfig, api_key: str, cancel_event: threading.Event) -> None:
        self.config = config
        self.api_key = (api_key or "").strip()
        self.cancel_event = cancel_event
        self.endpoint = config.base_url.rstrip("/") + "/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "WindowsHelperPlus/0.1 (+read-only-diagnostic)",
        }

    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        on_reasoning: ReasoningCallback | None = None,
        on_content: ContentCallback | None = None,
        on_attempt: AttemptCallback | None = None,
    ) -> LLMStreamResult:
        if not self.api_key:
            raise LLMConfigurationError(
                "尚未配置 DeepSeek API Key。请在“设置”中填写，或设置环境变量 DEEPSEEK_API_KEY。"
            )
        body = build_request_body(self.config, messages)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last_error: Exception | None = None
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            if self.cancel_event.is_set():
                raise LLMError("诊断已取消。")
            if on_attempt:
                on_attempt(attempt)
            started = time.monotonic()
            emitted = False
            emitted_content = False

            def _reasoning(text: str) -> None:
                nonlocal emitted
                emitted = True
                if on_reasoning:
                    on_reasoning(text)

            def _content(text: str) -> None:
                nonlocal emitted, emitted_content
                emitted = True
                emitted_content = True
                if on_content:
                    on_content(text)

            try:
                result = self._stream_once(data, _reasoning, _content)
                result.attempts = attempt
                result.elapsed_ms = int((time.monotonic() - started) * 1000)
                if result.finish_reason == "length":
                    result = self._continue_truncated_json(
                        messages,
                        result,
                        on_reasoning=_reasoning,
                        on_content=_content,
                    )
                return result
            except LLMConfigurationError:
                raise
            except LLMHTTPError as exc:
                last_error = exc
                retryable = exc.status in {408, 409, 425, 429, 500, 502, 503, 504}
                if emitted or not retryable or attempt >= max_attempts:
                    raise
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                last_error = exc
                if self.cancel_event.is_set():
                    raise LLMError("诊断已取消。") from exc
                if emitted or attempt >= max_attempts:
                    raise LLMError(
                        "无法连接 DeepSeek API。请检查网络、代理和 Base URL。"
                        f"（{type(exc).__name__}: {exc}）"
                    ) from exc
            except (OSError, ValueError) as exc:
                if self.cancel_event.is_set():
                    raise LLMError("诊断已取消。") from exc
                if emitted or attempt >= max_attempts:
                    raise LLMError(f"读取 DeepSeek 流式响应失败：{type(exc).__name__}: {exc}") from exc
                last_error = exc
            except LLMError as exc:
                # DeepSeek JSON Output 偶尔返回空内容；即使已流式输出 reasoning，
                # 只要没有输出 content，也可以安全重试一次。
                retryable_empty = "空内容" in str(exc) or "无法解析模型流式响应" in str(exc)
                if emitted_content or attempt >= max_attempts or not retryable_empty:
                    raise
                last_error = exc
            if attempt < max_attempts:
                # 指数退避，但可被“停止”按钮立即打断。
                if self.cancel_event.wait(1.2 * attempt):
                    raise LLMError("诊断已取消。")
        raise LLMError(f"DeepSeek API 调用失败：{last_error}")

    def _continue_truncated_json(
        self,
        messages: list[dict[str, Any]],
        initial: LLMStreamResult,
        *,
        on_reasoning: ReasoningCallback,
        on_content: ContentCallback,
    ) -> LLMStreamResult:
        """服务端单次输出上限导致 finish_reason=length 时，自动续写直到 JSON 完整。

        用户要求不设应用级 token/轮次上限，因此这里不使用“请调大 max_tokens”
        作为失败终点，而是把已输出内容作为 assistant 前缀，请模型从断点继续。
        续写时关闭 thinking，避免思考内容再次占满单次输出预算。
        """
        partial = initial.content
        reasoning = initial.reasoning
        finish_reason = initial.finish_reason
        usage = initial.usage
        response_id = initial.response_id
        attempts = initial.attempts
        no_progress = 0
        for continuation_index in range(1, 33):
            if self.cancel_event.is_set():
                raise LLMError("诊断已取消。")
            if partial.strip():
                cont_messages = list(messages) + [
                    {"role": "assistant", "content": partial},
                    {
                        "role": "user",
                        "content": (
                            "上一条 JSON 回复因服务端单次输出上限被截断，finish_reason=length。"
                            "请从截断处直接继续输出剩余 JSON 字符。不要重复已输出部分，"
                            "不要解释，不要 Markdown 代码块，不要重新开始 JSON，只输出剩余内容。"
                        ),
                    },
                ]
            else:
                # 初次响应的 content 为空（思考吃满输出预算）时，不存在“断点”，
                # 不能再要求“续写”；必须重新输出完整 JSON，否则会缺少开头的 {。
                cont_messages = list(messages) + [
                    {
                        "role": "user",
                        "content": (
                            "上一次输出没有产生任何 JSON 内容。请重新完整输出一个 JSON 对象："
                            "必须以 { 开始、以 } 结束，只输出 JSON，不要解释，不要 Markdown，"
                            "不要续写，不要省略开头。"
                        ),
                    }
                ]
            body = build_request_body(self.config, cont_messages)
            # 续写只补全 JSON，不需要再思考；否则 reasoning 可能再次吃满输出预算。
            body.pop("reasoning_effort", None)
            body["thinking"] = {"type": "disabled"}
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            before_len = len(partial)
            cont = self._stream_once(data, on_reasoning, on_content)
            partial += cont.content
            reasoning += cont.reasoning
            finish_reason = cont.finish_reason
            usage = cont.usage or usage
            response_id = cont.response_id or response_id
            attempts += cont.attempts
            if cont.finish_reason != "length":
                break
            if len(partial) <= before_len:
                no_progress += 1
            else:
                no_progress = 0
            if no_progress >= 3:
                raise LLMError(
                    "模型连续 3 次续写没有产生新内容（服务端仍返回 finish_reason=length）。"
                    "这不是应用级轮次限制；请稍后重试，或临时降低思考强度。"
                )
        else:
            raise LLMError(
                "已自动续写 32 次仍被服务端单次输出上限截断。"
                "这不是应用级轮次限制；请稍后重试，或临时降低思考强度/缩短输入。"
            )
        if finish_reason == "length":
            raise LLMError(
                "模型输出在自动续写后仍被服务端截断（finish_reason=length）；"
                "这不是应用级轮次限制，请稍后重试。"
            )
        return LLMStreamResult(
            content=partial,
            reasoning=reasoning,
            finish_reason=finish_reason,
            usage=usage,
            response_id=response_id,
            elapsed_ms=initial.elapsed_ms,
            attempts=attempts,
        )

    def _stream_once(
        self,
        data: bytes,
        on_reasoning: ReasoningCallback,
        on_content: ContentCallback,
    ) -> LLMStreamResult:
        request = urllib.request.Request(
            self.endpoint,
            data=data,
            headers=self._headers(),
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request, timeout=self.config.timeout_seconds)
        except urllib.error.HTTPError as exc:
            raw = b""
            try:
                raw = exc.read()
            except Exception:  # noqa: BLE001
                pass
            raise LLMHTTPError(exc.code, _extract_api_error(raw, exc.reason)) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout):
            raise

        # 另一个线程在用户点击“停止”时关闭响应流，让阻塞中的 readline 尽快返回。
        watcher_stop = threading.Event()

        def _watch_cancel() -> None:
            while not watcher_stop.wait(0.15):
                if self.cancel_event.is_set():
                    try:
                        response.close()
                    except Exception:  # noqa: BLE001
                        pass
                    return

        watcher = threading.Thread(target=_watch_cancel, name="hp-llm-cancel", daemon=True)
        watcher.start()

        try:
            with response:
                content_type = response.headers.get("Content-Type", "")
                if "text/event-stream" not in content_type.lower():
                    raw = response.read(20000)
                    # 有些代理会返回非流式 JSON；给出可读错误。
                    raise LLMError(
                        "DeepSeek API 没有返回流式内容（Content-Type="
                        f"{content_type or '未知'}）。响应摘要：{_extract_api_error(raw, '')}"
                    )
                content_parts: list[str] = []
                reasoning_parts: list[str] = []
                finish_reason: str | None = None
                usage: dict[str, Any] = {}
                response_id: str | None = None
                for raw_line in response:
                    if self.cancel_event.is_set():
                        raise LLMError("诊断已取消。")
                    try:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                    except Exception:  # noqa: BLE001
                        continue
                    if not line or line.startswith(":") or not line.startswith("data:"):
                        continue
                    event = parse_sse_payload(line[len("data:") :].strip())
                    if not event:
                        continue
                    if event.get("__done__"):
                        break
                    response_id = event.get("id") or response_id
                    if event.get("usage"):
                        usage = event["usage"]
                    choices = event.get("choices") or []
                    for choice in choices:
                        if not isinstance(choice, dict):
                            continue
                        delta = choice.get("delta") or {}
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if isinstance(reasoning, str) and reasoning:
                            reasoning_parts.append(reasoning)
                            on_reasoning(reasoning)
                        text = delta.get("content")
                        if isinstance(text, str) and text:
                            content_parts.append(text)
                            on_content(text)
                        if choice.get("finish_reason"):
                            finish_reason = str(choice["finish_reason"])
                content = "".join(content_parts)
                reasoning = "".join(reasoning_parts)
                if not content.strip() and finish_reason != "length":
                    raise LLMError(
                        "模型返回了空内容。DeepSeek 的 JSON Output 偶尔会出现空内容，请重试；"
                        "若反复发生，可关闭/调整 JSON 输出或更换模型。"
                    )
                # finish_reason=length 不在这里抛错：上层 stream 会自动续写，
                # 不把服务端单次输出上限变成一轮对话的硬失败。
                if self.cancel_event.is_set():
                    raise LLMError("诊断已取消。")
                return LLMStreamResult(
                    content=content,
                    reasoning=reasoning,
                    finish_reason=finish_reason,
                    usage=usage,
                    response_id=response_id,
                )
        finally:
            watcher_stop.set()


def _extract_api_error(raw: bytes, fallback: Any) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    if text:
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                error = obj.get("error")
                if isinstance(error, dict) and error.get("message"):
                    return str(error["message"])[:800]
                if obj.get("message"):
                    return str(obj["message"])[:800]
        except ValueError:
            pass
        return text[:800]
    return str(fallback or "未知错误")
