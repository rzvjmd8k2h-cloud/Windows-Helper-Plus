"""核心数据结构：工具结果、证据、执行步骤、诊断记录。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from pathlib import Path
import uuid
from typing import Any


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


@dataclass
class ToolResult:
    """单个只读取证工具的结构化输出。"""

    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    source: str = "Windows 原生接口"
    native_command: str = ""
    error: str | None = None
    raw_excerpt: str = ""
    duration_ms: int = 0
    transient: bool = False
    attempts: int = 1

    @classmethod
    def failure(
        cls,
        summary: str,
        error: str,
        *,
        source: str = "Windows 原生接口",
        native_command: str = "",
        transient: bool = False,
        duration_ms: int = 0,
        raw_excerpt: str = "",
    ) -> "ToolResult":
        return cls(
            ok=False,
            summary=summary,
            error=error,
            source=source,
            native_command=native_command,
            transient=transient,
            duration_ms=duration_ms,
            raw_excerpt=raw_excerpt,
        )


@dataclass
class Evidence:
    id: str
    tool: str
    source: str
    target: str | None
    created_at: str
    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    native_command: str = ""
    raw_excerpt: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def short(self) -> str:
        if self.ok:
            return self.summary
        return f"{self.summary}（失败：{self.error or '未知错误'}）"


@dataclass
class ToolStep:
    sequence: int
    tool: str
    arguments: dict[str, Any]
    basis: list[str]
    narration: str
    why: str
    evidence_id: str
    ok: bool
    origin: str = "planner"  # baseline | planner
    duration_ms: int = 0
    error: str | None = None
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EvidenceStore:
    """按 E1、E2… 分配稳定引用 ID，供 Planner 每一步引用。"""

    def __init__(self) -> None:
        self._items: list[Evidence] = []

    def __len__(self) -> int:
        return len(self._items)

    @property
    def items(self) -> list[Evidence]:
        return list(self._items)

    @property
    def ids(self) -> set[str]:
        return {item.id for item in self._items}

    def add(
        self,
        tool: str,
        result: ToolResult,
        *,
        target: str | None = None,
    ) -> Evidence:
        evidence = Evidence(
            id=f"E{len(self._items) + 1}",
            tool=tool,
            source=result.source,
            target=target,
            created_at=now_iso(),
            ok=result.ok,
            summary=result.summary,
            data=result.data,
            error=result.error,
            native_command=result.native_command,
            raw_excerpt=result.raw_excerpt,
            duration_ms=result.duration_ms,
        )
        self._items.append(evidence)
        return evidence

    def by_tool(self, tool: str) -> list[Evidence]:
        return [item for item in self._items if item.tool == tool]

    def get(self, evidence_id: str) -> Evidence | None:
        for item in self._items:
            if item.id == evidence_id:
                return item
        return None

    def to_list(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self._items]


def _compact(value: Any, *, depth: int = 0, max_str: int = 900, max_items: int = 30) -> Any:
    """压缩证据，控制送进模型的上下文体积。"""
    if depth > 5:
        return "…"
    if isinstance(value, str):
        text = value.replace("\r\n", "\n").replace("\r", "\n")
        if len(text) > max_str:
            return text[:max_str] + f"…（共 {len(text)} 字符）"
        return text
    if isinstance(value, dict):
        items = list(value.items())[:max_items]
        return {str(k): _compact(v, depth=depth + 1, max_str=max_str, max_items=max_items) for k, v in items}
    if isinstance(value, (list, tuple)):
        truncated = list(value[:max_items])
        result = [_compact(v, depth=depth + 1, max_str=max_str, max_items=max_items) for v in truncated]
        if len(value) > max_items:
            result.append(f"…另有 {len(value) - max_items} 项未展示")
        return result
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _compact(str(value), depth=depth + 1, max_str=max_str, max_items=max_items)


def evidence_for_prompt(evidence: Evidence, *, max_chars: int = 5000) -> dict[str, Any]:
    data = _compact(evidence.data, max_str=700, max_items=18)
    encoded = json.dumps(data, ensure_ascii=False)
    if len(encoded) > max_chars:
        data = {"note": "该证据数据较长，已省略细节；请查看摘要。", "preview": encoded[:max_chars]}
    return {
        "id": evidence.id,
        "tool": evidence.tool,
        "ok": evidence.ok,
        "summary": evidence.summary,
        "error": evidence.error,
        "collected_at": evidence.created_at,
        "data": data,
    }


@dataclass
class DiagnosticRecord:
    target: str | None
    issue: str
    mode: str
    model: str
    reasoning_effort: str
    target_kind: str = "system"
    record_id: str = field(default_factory=lambda: new_id("DIAG"))
    started_at: str = field(default_factory=now_iso)
    finished_at: str | None = None
    status: str = "running"  # running | completed | partial | error | cancelled
    evidence: list[dict[str, Any]] = field(default_factory=list)
    tool_steps: list[dict[str, Any]] = field(default_factory=list)
    planner: list[dict[str, Any]] = field(default_factory=list)
    interactions: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    context_turns: list[dict[str, Any]] = field(default_factory=list)
    local_context: dict[str, Any] | None = None
    confirmed_user_goal: str = ""
    case_path: str | None = None
    plan: dict[str, Any] | None = None
    report: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    generator: str = "Windows Helper Plus / v2（AI 分析 + 受控修复）"

    def finish(self, status: str) -> None:
        self.status = status
        self.finished_at = now_iso()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.record_id}.json"
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
        return path
