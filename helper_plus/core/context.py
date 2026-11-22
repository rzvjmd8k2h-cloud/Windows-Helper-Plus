"""长对话上下文压缩与诊断日志。

Planner 每轮接收无状态快照；随着步数增加，证据会快速增长。该模块：
- 永远保留每条证据的 ID/摘要/工具/时间；
- 按预算为最近的、被步骤引用的证据保留完整 data；
- 较旧证据退化为摘要并标记 compressed；
- 生成滚动的诊断日志（步骤、动作、用户问答），避免模型丢失路径；
- 提供粗略 token 估算用于 UI 展示。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from ..models import EvidenceStore, ToolStep, evidence_for_prompt


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    chinese = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    other = len(text) - chinese
    return int(chinese + other / 3.5) + 1


@dataclass
class ContextStats:
    evidence_total: int
    evidence_full: int
    evidence_compressed: int
    estimated_chars: int
    estimated_tokens: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_total": self.evidence_total,
            "evidence_full": self.evidence_full,
            "evidence_compressed": self.evidence_compressed,
            "estimated_chars": self.estimated_chars,
            "estimated_tokens": self.estimated_tokens,
        }


def build_evidence_context(
    store: EvidenceStore,
    *,
    budget_chars: int = 700_000,
    priority_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], ContextStats]:
    """按字符预算压缩证据，返回（送给模型的证据数组, 统计）。"""
    priority_ids = priority_ids or set()
    items = store.items
    if not items:
        return [], ContextStats(0, 0, 0, 0, 0)

    # 先给所有证据保留摘要骨架，保证 E# 引用始终存在。
    skeleton: list[dict[str, Any]] = []
    skeleton_chars = 0
    for evidence in items:
        summary = evidence.summary
        if len(summary) > 700:
            summary = summary[:700] + "…"
        skeleton.append(
            {
                "id": evidence.id,
                "tool": evidence.tool,
                "ok": evidence.ok,
                "summary": summary,
                "error": (evidence.error or "")[:600] or None,
                "collected_at": evidence.created_at,
                "duration_ms": evidence.duration_ms,
                "data": None,
                "compressed": True,
            }
        )
        skeleton_chars += len(json.dumps(skeleton[-1], ensure_ascii=False))

    summary_budget = max(10_000, int(budget_chars * 0.30))
    if skeleton_chars > summary_budget:
        # 摘要本身也超预算时，从最早证据开始进一步缩简。
        for item in skeleton[:-8]:
            item["summary"] = item["summary"][:160] + "…"
            item["error"] = None
        skeleton_chars = len(json.dumps(skeleton, ensure_ascii=False))

    data_budget = max(20_000, budget_chars - skeleton_chars)
    by_id = {item["id"]: item for item in skeleton}
    full_count = 0
    used = 0
    # 优先：最近 12 条 + 最近步骤引用 + 外部传入的 focus，再加入其余证据直到预算。
    priority_order: list[str] = []
    for evidence in reversed(items[-12:]):
        if evidence.id not in priority_order:
            priority_order.append(evidence.id)
    for ref in priority_ids:
        if ref in by_id and ref not in priority_order:
            priority_order.append(ref)

    for evidence in items:
        if evidence.id not in priority_order:
            priority_order.append(evidence.id)
    for evidence_id in priority_order:
        if evidence_id not in by_id:
            continue
        evidence = store.get(evidence_id)
        if evidence is None:
            continue
        full = evidence_for_prompt(evidence, max_chars=12_000)
        serialized = json.dumps(full, ensure_ascii=False)
        if used + len(serialized) > data_budget and full_count >= 4:
            continue
        by_id[evidence_id].update(
            {
                "summary": full.get("summary") or by_id[evidence_id]["summary"],
                "error": full.get("error"),
                "data": full.get("data"),
                "compressed": False,
            }
        )
        used += len(serialized)
        full_count += 1

    result = [by_id[item.id] for item in items if item.id in by_id]
    rendered = json.dumps(result, ensure_ascii=False)
    stats = ContextStats(
        evidence_total=len(items),
        evidence_full=full_count,
        evidence_compressed=len(items) - full_count,
        estimated_chars=len(rendered),
        estimated_tokens=estimate_tokens(rendered),
    )
    return result, stats


def build_diagnosis_journal(
    steps: list[ToolStep],
    *,
    interactions: list[dict[str, Any]] | None = None,
    actions: list[dict[str, Any]] | None = None,
    limit: int = 80,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for step in steps[-limit:]:
        entries.append(
            {
                "ts": step.created_at,
                "type": "tool",
                "step": step.sequence,
                "origin": step.origin,
                "tool": step.tool,
                "basis": step.basis,
                "evidence": step.evidence_id,
                "ok": step.ok,
                "duration_ms": step.duration_ms,
                "summary": (step.error or step.narration or "")[:400],
            }
        )
    for item in (interactions or [])[-20:]:
        entries.append({"type": "user_answer", **{k: v for k, v in item.items() if k != "answer"}})
        entries[-1]["answer"] = str(item.get("answer") or "")[:800]
    for item in (actions or [])[-20:]:
        entries.append(
            {
                "type": "action",
                "ts": item.get("finished_at") or item.get("started_at"),
                "title": item.get("action", {}).get("title"),
                "command": (item.get("command") or "")[:1000],
                "ok": item.get("ok"),
                "returncode": item.get("returncode"),
                "elevated": item.get("elevated"),
                "error": item.get("error"),
                "duration_ms": item.get("duration_ms"),
            }
        )
    entries.sort(key=lambda entry: str(entry.get("ts") or ""))
    return entries
