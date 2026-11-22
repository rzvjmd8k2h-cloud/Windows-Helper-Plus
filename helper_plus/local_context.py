"""本地 Qwen 上下文官、病历归档与日志 Markdown 翻译。

本地模型只负责“上下文/历史/日志”的整理，不产生最终诊断，不执行动作。
主模型仍必须二次确认用户目标、独立调用工具、生成最终 final。
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

from .local_model import LocalModelClient, LocalModelError


LOCAL_SYSTEM_PROMPT = r"""你是 Windows Helper Plus 的本地上下文官（Qwen3-1.7B）。只做上下文整理：不诊断、不决定工具、不生成命令、不执行操作、不认定根因。

必须只输出一个 JSON 对象，不要 Markdown 围栏。字段：
{
  "user_goal_candidate": "用户目标候选表述",
  "goal_clarity": "clear|ambiguous|contradictory",
  "goal_notes": "目标是否清楚/矛盾；简短",
  "session_status": {"status": "当前状态", "round": 0, "evidence_count": 0, "phase": "阶段"},
  "completed": ["已完成/已验证的事实"],
  "unfinished": ["未完成/未验证"],
  "blocked": ["失败/被阻塞的检查"],
  "open_questions": ["需用户补充的物理/型号/现场信息"],
  "related_prior_cases": [{"case_id": "..", "why": "关系", "lesson": "经验"}],
  "recent_turn_decisions": {"keep_turn_ids": ["T.."], "drop_turn_ids": ["T.."], "reason": "简短"},
  "relevant_recent_messages": [{"turn_id": "T..", "role": "user|assistant|tool", "summary": ".."}],
  "context_summary": "不超过 200 字",
  "risks": ["风险"],
  "recommended_focus": ["主模型下一步应确认/补齐的方向"]
}

约束：每个数组最多 4 项；不得编造输入没有的事实；user_goal_candidate 只是候选，主模型二次确认；信息不足时如实写 unfinished/open_questions；不输出 JSON 之外文字。 /no_think
"""

LOG_MD_SYSTEM_PROMPT = r"""你是 Windows Helper Plus 的日志整理员（Qwen3-1.7B）。把单次诊断 JSON 记录翻译成中文 Markdown 日志。

要求：
- 只输出 Markdown，不要额外解释，不要编造输入没有的内容。
- 章节：# 任务、## 状态与时间、## 执行计划、## 时间线、## 完成、## 未完成/风险、## 证据索引（保留 E#）、## 修复动作与结果、## 结论/下一步。
- 保留记录 ID、目标、模型、开始/结束/用时、证据/步骤/动作数量；动作命令用行内代码；失败/取消要写明。
- 简洁，总长度尽量不超过 1200 字。 /no_think
"""


def log_root() -> Path:
    override = os.environ.get("HELPER_PLUS_LOG_DIR", "").strip()
    if override:
        root = Path(override).expanduser().resolve()
    else:
        from .config import PACKAGE_ROOT, app_data_dir

        # 打包后安装目录通常只读，日志放到用户可写目录，方便用户直接查看。
        root = (
            (app_data_dir() / "Log")
            if getattr(sys, "frozen", False)
            else (PACKAGE_ROOT / "Log")
        )
    root.mkdir(parents=True, exist_ok=True)
    return root


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:160] or "record"


class CaseArchive:
    """病历归档：每次运行都可写，completed 的病例也会进入主模型参考。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or log_root()) / "病历"
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.jsonl"

    def _append_index(self, entry: dict[str, Any]) -> None:
        with self.index_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def load_index(self, limit: int = 40) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        if not self.index_path.exists():
            return entries
        try:
            with self.index_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict):
                        entries.append(item)
        except OSError:
            return []
        return entries[-limit:]

    def related_cases(
        self,
        *,
        record_id: str,
        target: str | None,
        issue: str,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        entries = [item for item in self.load_index(limit=200) if item.get("record_id") != record_id]
        tokens = {
            token.lower()
            for token in re.split(r"[^\w\u4e00-\u9fff]+", f"{target or ''} {issue or ''}")
            if len(token) >= 2
        }
        scored: list[tuple[int, dict[str, Any]]] = []
        for item in entries:
            text = " ".join(
                str(item.get(key) or "")
                for key in ("target", "issue", "goal", "summary", "status")
            ).lower()
            score = sum(1 for token in tokens if token and token in text)
            if item.get("status") == "completed":
                score += 1
            scored.append((score, item))
        scored.sort(key=lambda pair: (pair[0], str(pair[1].get("created_at") or "")), reverse=True)
        selected = [item for score, item in scored[:limit] if score > 0]
        if not selected:
            selected = [item for _, item in scored[:3]]
        result = []
        for item in selected[-limit:]:
            result.append(
                {
                    "case_id": item.get("record_id"),
                    "target": item.get("target"),
                    "issue": item.get("issue"),
                    "status": item.get("status"),
                    "goal": item.get("goal"),
                    "summary": (item.get("summary") or "")[:800],
                    "completed": (item.get("completed") or [])[:8],
                    "unfinished": (item.get("unfinished") or [])[:8],
                    "lesson": item.get("lesson") or "",
                    "path": item.get("path"),
                }
            )
        return result

    def save(
        self,
        *,
        record: dict[str, Any],
        local_context: dict[str, Any] | None,
        main_goal: str,
        markdown: str,
        translation_error: str | None = None,
    ) -> Path:
        record_id = str(record.get("record_id") or "unknown")
        case_path = self.root / f"{record_id}.md"
        case_path.write_text(markdown, encoding="utf-8")
        local = local_context or {}
        entry = {
            "record_id": record_id,
            "created_at": record.get("finished_at") or record.get("started_at") or _now_iso(),
            "status": record.get("status"),
            "target": record.get("target"),
            "target_kind": record.get("target_kind"),
            "issue": record.get("issue"),
            "goal": main_goal or local.get("user_goal_candidate") or "",
            "summary": (record.get("report") or {}).get("summary") or local.get("context_summary") or "",
            "completed": local.get("completed") or [],
            "unfinished": local.get("unfinished") or [],
            "blocked": local.get("blocked") or [],
            "open_questions": local.get("open_questions") or [],
            "risks": local.get("risks") or [],
            "evidence_count": len(record.get("evidence") or []),
            "actions": [
                {
                    "title": (item.get("action") or {}).get("title"),
                    "ok": item.get("ok"),
                    "elevated": item.get("elevated"),
                    "returncode": item.get("returncode"),
                }
                for item in record.get("actions") or []
                if isinstance(item, dict)
            ],
            "local_model": {
                "goal_clarity": local.get("goal_clarity"),
                "goal_notes": local.get("goal_notes"),
            },
            "translation_error": translation_error,
            "path": str(case_path),
        }
        (self.root / f"{record_id}.json").write_text(
            json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._append_index(entry)
        return case_path


class LocalContextAgent:
    """本地 Qwen：上下文整理 + 近期消息筛选 + 日志 MD + 病历。"""

    def __init__(
        self,
        client: LocalModelClient | None = None,
        *,
        root: Path | None = None,
        max_evidence: int = 8,
        max_steps: int = 10,
    ) -> None:
        self.client = client or LocalModelClient()
        self.root = root or log_root()
        self.archive = CaseArchive(self.root)
        self.runs_dir = self.root / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.max_evidence = max_evidence
        self.max_steps = max_steps
        self.last_error: str | None = None
        self.last_context: dict[str, Any] | None = None

    def _compact_evidence(self, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items = []
        for item in evidence[-self.max_evidence :]:
            data = item.get("data") if isinstance(item.get("data"), dict) else {}
            items.append(
                {
                    "id": item.get("id"),
                    "tool": item.get("tool"),
                    "ok": item.get("ok"),
                    "summary": str(item.get("summary") or "")[:180],
                    "error": str(item.get("error") or "")[:120],
                    "data_keys": list(data.keys())[:20] if isinstance(data, dict) else [],
                }
            )
        return items

    def _compact_steps(self, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "sequence": item.get("sequence"),
                "tool": item.get("tool"),
                "basis": item.get("basis"),
                "evidence_id": item.get("evidence_id"),
                "ok": item.get("ok"),
                "origin": item.get("origin"),
                "duration_ms": item.get("duration_ms"),
                "created_at": item.get("created_at"),
            }
            for item in steps[-self.max_steps :]
        ]

    @staticmethod
    def _compact_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """本地模型只需要判断近期消息 keep/drop，不需要每条全文。"""
        result = []
        for item in turns[-6:]:
            if not isinstance(item, dict):
                continue
            result.append(
                {
                    "id": item.get("id"),
                    "role": item.get("role"),
                    "kind": item.get("kind"),
                    "content": str(item.get("content") or "")[:320],
                }
            )
        return result

    @staticmethod
    def _compact_interactions(interactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for item in interactions[-4:]:
            if not isinstance(item, dict):
                continue
            result.append(
                {
                    "question": str(item.get("question") or "")[:220],
                    "answer": str(item.get("answer") or "")[:300],
                }
            )
        return result

    @staticmethod
    def _compact_plan(plan: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(plan, dict):
            return None
        return {
            "goal": str(plan.get("goal") or "")[:500],
            "confirmed_user_goal": str(plan.get("confirmed_user_goal") or "")[:500],
            "steps": [str(item)[:240] for item in (plan.get("steps") or [])[:10]],
        }

    @staticmethod
    def _compact_prior_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for item in cases[:3]:
            if not isinstance(item, dict):
                continue
            result.append(
                {
                    "record_id": item.get("record_id"),
                    "issue": str(item.get("issue") or "")[:240],
                    "summary": str(item.get("summary") or "")[:320],
                    "goal": str(item.get("goal") or "")[:240],
                }
            )
        return result

    def analyze(
        self,
        *,
        record_id: str,
        target: str,
        target_kind: str,
        issue: str,
        status: str,
        elapsed_seconds: float,
        round_number: int,
        plan: dict[str, Any] | None,
        evidence: list[dict[str, Any]],
        steps: list[dict[str, Any]],
        interactions: list[dict[str, Any]],
        actions: list[dict[str, Any]],
        recent_turns: list[dict[str, Any]],
        main_goal: str = "",
    ) -> dict[str, Any] | None:
        cases = self.archive.related_cases(record_id=record_id, target=target, issue=issue)
        payload = {
            "record_id": record_id,
            "now": _now_iso(),
            "target": target,
            "target_kind": target_kind,
            "issue": issue,
            "main_model_confirmed_goal": main_goal or None,
            "session": {
                "status": status,
                "elapsed_seconds": round(elapsed_seconds, 1),
                "round": round_number,
            },
            "plan": self._compact_plan(plan),
            "evidence": self._compact_evidence(evidence),
            "tool_steps": self._compact_steps(steps),
            "interactions": self._compact_interactions(interactions),
            "actions": [
                {
                    "title": str((item.get("action") or {}).get("title") or "")[:160],
                    "ok": item.get("ok"),
                    "elevated": item.get("elevated"),
                    "returncode": item.get("returncode"),
                    "error": str(item.get("error") or "")[:160],
                }
                for item in actions[-6:]
                if isinstance(item, dict)
            ],
            "recent_turns": self._compact_turns(recent_turns),
            "prior_cases": self._compact_prior_cases(cases),
        }
        try:
            result = self.client.chat_json(
                system=LOCAL_SYSTEM_PROMPT,
                user=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                max_tokens=600,
                temperature=0.1,
            )
        except LocalModelError as exc:
            self.last_error = str(exc)
            return None
        self.last_error = None
        self.last_context = result
        return result

    def translate_record(self, record: dict[str, Any], local_context: dict[str, Any] | None) -> tuple[str, str | None]:
        """返回 (Markdown, 错误)。本地模型不可用时仍生成基础 Markdown，不丢日志。"""
        compact = {
            "record_id": record.get("record_id"),
            "target": record.get("target"),
            "target_kind": record.get("target_kind"),
            "issue": record.get("issue"),
            "status": record.get("status"),
            "model": record.get("model"),
            "reasoning_effort": record.get("reasoning_effort"),
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "elapsed_seconds": record.get("elapsed_seconds"),
            "plan_goal": (record.get("plan") or {}).get("goal"),
            "plan_steps": (record.get("plan") or {}).get("steps"),
            "evidence": [
                {"id": item.get("id"), "tool": item.get("tool"), "ok": item.get("ok"), "summary": str(item.get("summary") or "")[:180]}
                for item in (record.get("evidence") or [])[:10]
            ],
            "tool_steps": [
                {"sequence": item.get("sequence"), "tool": item.get("tool"), "evidence_id": item.get("evidence_id"), "ok": item.get("ok"), "duration_ms": item.get("duration_ms")}
                for item in (record.get("tool_steps") or [])[:12]
            ],
            "interactions": [
                {"question": str(item.get("question") or "")[:220], "answer": str(item.get("answer") or "")[:300]}
                for item in (record.get("interactions") or [])[-3:]
                if isinstance(item, dict)
            ],
            "actions": [
                {
                    "title": (item.get("action") or {}).get("title"),
                    "command": str(item.get("command") or "")[:400],
                    "ok": item.get("ok"),
                    "elevated": item.get("elevated"),
                    "returncode": item.get("returncode"),
                    "stdout": str(item.get("stdout") or "")[:400],
                    "error": item.get("error"),
                }
                for item in (record.get("actions") or [])[-3:]
                if isinstance(item, dict)
            ],
            "report": {
                "headline": (record.get("report") or {}).get("headline"),
                "summary": str((record.get("report") or {}).get("summary") or "")[:1200],
                "next_steps": (record.get("report") or {}).get("next_steps"),
            },
            "errors": (record.get("errors") or [])[:3],
            "local_context": {
                "user_goal_candidate": (local_context or {}).get("user_goal_candidate"),
                "context_summary": (local_context or {}).get("context_summary"),
                "completed": (local_context or {}).get("completed"),
                "unfinished": (local_context or {}).get("unfinished"),
            },
        }
        error: str | None = None
        try:
            markdown = self.client.chat(
                system=LOG_MD_SYSTEM_PROMPT,
                user=json.dumps(compact, ensure_ascii=False, separators=(",", ":")),
                max_tokens=420,
                temperature=0.1,
            )
        except LocalModelError as exc:
            error = str(exc)
            markdown = self._fallback_markdown(record, local_context, error)
        if not markdown.strip():
            error = error or "本地模型返回空 Markdown"
            markdown = self._fallback_markdown(record, local_context, error)
        # 本地模型负责人类可读翻译；完整证据/动作/时间线由程序追加，避免 1.7B 输出过长。
        appendix = self._deterministic_appendix(record)
        if appendix.strip() and "## 完整证据索引" not in markdown:
            markdown = markdown.rstrip() + "\n\n" + appendix
        record_id = _safe_id(str(record.get("record_id") or "unknown"))
        run_path = self.runs_dir / f"{record_id}.md"
        run_path.write_text(markdown, encoding="utf-8")
        return markdown, error

    def _deterministic_appendix(self, record: dict[str, Any]) -> str:
        lines = ["## 完整证据索引", ""]
        for item in record.get("evidence") or []:
            lines.append(
                f"- {item.get('id')} `{item.get('tool')}` "
                f"{'成功' if item.get('ok') else '失败'}：{item.get('summary') or ''}"
            )
        lines.extend(["", "## 完整时间线", ""])
        for item in record.get("tool_steps") or []:
            lines.append(
                f"- #{item.get('sequence')} `{item.get('tool')}` "
                f"{'成功' if item.get('ok') else '失败'}，{item.get('duration_ms') or 0} ms，"
                f"证据 {item.get('evidence_id')}"
            )
        actions = record.get("actions") or []
        if actions:
            lines.extend(["", "## 完整动作记录", ""])
            for item in actions:
                action = item.get("action") or {}
                lines.append(
                    f"- {action.get('title') or '动作'}：{'成功' if item.get('ok') else '失败'}"
                    f"（exit={item.get('returncode')}, elevated={item.get('elevated')}）"
                )
                if item.get("command"):
                    lines.append(f"  - 命令：`{item.get('command')}`")
        return "\n".join(lines) + "\n"

    def _fallback_markdown(
        self,
        record: dict[str, Any],
        local_context: dict[str, Any] | None,
        error: str | None,
    ) -> str:
        report = record.get("report") or {}
        lines = [
            f"# 诊断日志 {record.get('record_id')}",
            "",
            f"- 目标：{record.get('target')}",
            f"- 状态：{record.get('status')}",
            f"- 模型：{record.get('model')} / {record.get('reasoning_effort')}",
            f"- 时间：{record.get('started_at')} → {record.get('finished_at')}",
            f"- 用时：{record.get('elapsed_seconds')} 秒",
            f"- 证据/步骤/动作：{len(record.get('evidence') or [])}/{len(record.get('tool_steps') or [])}/{len(record.get('actions') or [])}",
            "",
        ]
        if error:
            lines.extend([f"> 本地 Qwen 翻译失败，已使用规则模板：{error}", ""])
        if report:
            lines.extend([f"## 结论\n\n**{report.get('headline') or ''}**\n", str(report.get("summary") or ""), ""])
        lines.append("## 证据索引")
        for item in record.get("evidence") or []:
            lines.append(f"- {item.get('id')} `{item.get('tool')}`：{item.get('summary') or ''}")
        lines.append("")
        lines.append("## 动作")
        for item in record.get("actions") or []:
            action = item.get("action") or {}
            lines.append(
                f"- {action.get('title') or '动作'}：{'成功' if item.get('ok') else '失败'}"
                f"（exit={item.get('returncode')}, elevated={item.get('elevated')}）"
            )
        if local_context:
            lines.extend(["", "## 本地上下文", f"- 候选目标：{local_context.get('user_goal_candidate')}", f"- 摘要：{local_context.get('context_summary')}"])
        return "\n".join(lines) + "\n"

    def finalize(
        self,
        *,
        record: dict[str, Any],
        local_context: dict[str, Any] | None,
        main_goal: str,
    ) -> Path:
        markdown, error = self.translate_record(record, local_context)
        case_path = self.archive.save(
            record=record,
            local_context=local_context,
            main_goal=main_goal,
            markdown=markdown,
            translation_error=error,
        )
        self.last_error = error
        return case_path
