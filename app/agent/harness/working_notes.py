"""
抗压缩工作笔记：压缩可以有损，已确认事实 / 来源必须能读回来。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional


NOTES_FILENAME = "working_notes.md"


def render_working_notes(
    *,
    task_query: str,
    step_results: list[Any],
    evidence_sources: Optional[list[Any]] = None,
    max_facts: int = 20,
    max_sources: int = 12,
) -> str:
    """从本任务已完成步骤生成给模型看的笔记（短、可 pin）。"""
    facts: list[str] = []
    sources: list[str] = []
    seen_facts: set[str] = set()
    seen_sources: set[str] = set()

    for result in step_results:
        payload = (getattr(result, "metadata", None) or {}).get("worker_payload") or {}
        if isinstance(payload, dict):
            for fact in payload.get("facts") or []:
                key = str(fact).strip().lower()
                if key and key not in seen_facts:
                    seen_facts.add(key)
                    facts.append(str(fact).strip())
            for src in payload.get("sources") or []:
                key = str(src).strip().lower()
                if key and key not in seen_sources:
                    seen_sources.add(key)
                    sources.append(str(src).strip())

    for src in evidence_sources or []:
        locator = getattr(src, "locator", None)
        if locator is None and isinstance(src, dict):
            locator = src.get("locator")
        excerpt = getattr(src, "excerpt", None)
        if excerpt is None and isinstance(src, dict):
            excerpt = src.get("excerpt")
        sid = getattr(src, "source_id", None)
        if sid is None and isinstance(src, dict):
            sid = src.get("source_id")
        if locator:
            key = str(locator).strip().lower()
            if key not in seen_sources:
                seen_sources.add(key)
                label = f"{sid} {locator}" if sid else str(locator)
                sources.append(label)
        if excerpt:
            key = str(excerpt).strip().lower()
            if key and key not in seen_facts:
                seen_facts.add(key)
                facts.append(str(excerpt).strip()[:200])

    lines = [
        "    【工作笔记 — 压缩后仍须遵守】",
        f"    任务: {task_query[:120]}",
        "    已确认事实:",
    ]
    if facts:
        for fact in facts[:max_facts]:
            lines.append(f"      - {fact[:240]}")
    else:
        lines.append("      - （尚无结构化事实）")
    lines.append("    已登记来源:")
    if sources:
        for src in sources[:max_sources]:
            lines.append(f"      - {src[:200]}")
    else:
        lines.append("      - （尚无来源）")
    lines.append("    规则: 写报告不得使用笔记未出现的精确数字；引用必须对应已登记来源。")
    return "\n".join(lines)


def write_working_notes_file(session_dir: Path, notes: str) -> Path:
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / NOTES_FILENAME
    path.write_text(notes.replace("    ", "", 1) if notes.startswith("    ") else notes, encoding="utf-8")
    return path
