"""从流式 JSON 中增量提取 narration 字段，用于友好的实时打印。

模型被要求先输出 ``{"narration":"...", ...}``。直接展示原始 JSON 很丑，因此在
不等待完整响应的前提下，逐字符解码 narration 字符串并送到 UI；同时原始 JSON
仍保留在“原始响应”页供审计。
"""

from __future__ import annotations


_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
_KEY = '"narration"'


class NarrationExtractor:
    def __init__(self, key: str = "narration") -> None:
        self._key = f'"{key}"'
        self._buffer = ""
        self._pos = 0
        self._state = "seek_key"
        self._done = False

    @property
    def started(self) -> bool:
        return self._state not in {"seek_key", "seek_colon", "seek_quote"}

    @property
    def done(self) -> bool:
        return self._done

    def feed(self, chunk: str) -> str:
        if not chunk or self._done:
            return ""
        self._buffer += chunk
        out: list[str] = []
        while self._pos < len(self._buffer):
            if self._state == "seek_key":
                index = self._buffer.find(self._key, self._pos)
                if index < 0:
                    # 保留可能跨 chunk 的部分 key。
                    self._pos = max(0, len(self._buffer) - len(self._key) + 1)
                    break
                self._pos = index + len(self._key)
                self._state = "seek_colon"
                continue

            if self._state == "seek_colon":
                char = self._buffer[self._pos]
                if char == ":":
                    self._pos += 1
                    self._state = "seek_quote"
                elif char.isspace():
                    self._pos += 1
                else:
                    # 结构异常：放弃流式提取，不影响完整 JSON 解析。
                    self._done = True
                    break
                continue

            if self._state == "seek_quote":
                char = self._buffer[self._pos]
                if char == '"':
                    self._pos += 1
                    self._state = "in_string"
                    continue
                if char.isspace():
                    self._pos += 1
                    continue
                self._done = True
                break

            if self._state == "in_string":
                char = self._buffer[self._pos]
                if char == "\\":
                    if self._pos + 1 >= len(self._buffer):
                        # 转义序列被 chunk 边界截断，等待下一块。
                        break
                    escaped = self._buffer[self._pos + 1]
                    if escaped == "u":
                        if self._pos + 6 > len(self._buffer):
                            break
                        hex_text = self._buffer[self._pos + 2 : self._pos + 6]
                        try:
                            codepoint = int(hex_text, 16)
                        except ValueError:
                            out.append("\ufffd")
                            self._pos += 6
                            continue
                        if 0xD800 <= codepoint <= 0xDBFF:
                            # JSON 用 UTF-16 surrogate pair 表示 BMP 外字符；
                            # 必须等第二个 \\uXXXX 到齐后再解码，否则会输出乱码。
                            if self._pos + 12 > len(self._buffer):
                                break
                            if self._buffer[self._pos + 6 : self._pos + 8] != "\\u":
                                out.append("\ufffd")
                                self._pos += 6
                                continue
                            low_text = self._buffer[self._pos + 8 : self._pos + 12]
                            try:
                                low = int(low_text, 16)
                            except ValueError:
                                out.append("\ufffd")
                                self._pos += 6
                                continue
                            if 0xDC00 <= low <= 0xDFFF:
                                out.append(chr(0x10000 + ((codepoint - 0xD800) << 10) + (low - 0xDC00)))
                                self._pos += 12
                                continue
                            out.append("\ufffd")
                            self._pos += 6
                            continue
                        if 0xDC00 <= codepoint <= 0xDFFF:
                            out.append("\ufffd")
                        else:
                            out.append(chr(codepoint))
                        self._pos += 6
                    else:
                        out.append(_ESCAPES.get(escaped, escaped))
                        self._pos += 2
                elif char == '"':
                    self._pos += 1
                    self._both_done()
                    break
                else:
                    out.append(char)
                    self._pos += 1
                continue

            break
        return "".join(out)

    def _both_done(self) -> None:
        self._done = True
        self._state = "done"
        # 防止 buffer 无限增长。
        self._buffer = self._buffer[-4096:]
        self._pos = 0
