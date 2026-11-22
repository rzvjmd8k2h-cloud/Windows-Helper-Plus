"""narration 流式提取器测试。"""

from __future__ import annotations

import json

from helper_plus.llm.narration import NarrationExtractor


def _extract(raw: str, chunk_sizes: list[int]) -> str:
    extractor = NarrationExtractor()
    out: list[str] = []
    position = 0
    for size in chunk_sizes:
        out.append(extractor.feed(raw[position : position + size]))
        position += size
    if position < len(raw):
        out.append(extractor.feed(raw[position:]))
    return "".join(out)


def test_extracts_utf8_narration():
    raw = json.dumps(
        {
            "narration": "正在读取 [E1] 事件日志，并检查 Windows 原生证据。",
            "action": "call_tool",
        },
        ensure_ascii=False,
    )
    assert _extract(raw, [1, 1, 2, 3, 5, 8, 13]) == "正在读取 [E1] 事件日志，并检查 Windows 原生证据。"


def test_handles_escapes_and_unicode_escape_across_chunks():
    narration = '含 "引号"、反斜杠 \\ 与换行\n以及 emoji 😀 和中文。'
    raw = json.dumps({"narration": narration, "action": "final"}, ensure_ascii=True)
    # 故意切成很小的块，覆盖 \\uXXXX 被截断的情况。
    sizes = [1] * min(30, len(raw)) + [7]
    assert _extract(raw, sizes) == narration


def test_stops_after_closing_quote():
    raw = '{"narration":"第一段", "action":"call_tool", "basis":["E1"]}'
    extractor = NarrationExtractor()
    first = extractor.feed(raw[:18])
    second = extractor.feed(raw[18:])
    assert "第一段" in first + second
    assert extractor.done
    assert extractor.feed('后面不该出现的内容') == ""


def test_malformed_structure_does_not_crash():
    extractor = NarrationExtractor()
    assert extractor.feed('{"narration": 123,') == ""
    assert extractor.done
