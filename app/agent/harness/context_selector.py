"""
JIT Context Selector — Mandatory 始终在，Optional 按当前任务检索。

ContextBuilder 不再「把所有可能有用的东西拼起来」，而是从 Context Store
选出当前步骤最有价值的一小撮 token。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.agent.harness.evidence_store import EvidenceStore, get_evidence_store
from app.agent.harness.orchestration import SYNTHESIS_STEP_TYPES
from app.agent.harness.research_brief import ResearchBrief

_TOKEN_RE = re.compile(r"[\w\u3400-\u9fff]{2,}")


def _tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")}


def _score(query: str, target: str) -> float:
    q = _tokens(query)
    t = _tokens(target)
    if not q or not t:
        return 0.0
    return len(q & t) / len(q)


@dataclass
class SelectedContext:
    mandatory: dict[str, str]
    optional: dict[str, str]
    evidence_ids: list[str]
    memory_ids: list[str]
    dropped: list[str]

    def layers(self) -> dict[str, str]:
        merged = dict(self.mandatory)
        merged.update(self.optional)
        return merged


def _task_query_text(brief: ResearchBrief | None, task_query: str, objective: str) -> str:
    parts = [
        getattr(brief, "objective", "") if brief else "",
        " ".join(getattr(brief, "entities", []) or []) if brief else "",
        objective,
        task_query,
    ]
    return " ".join(p for p in parts if p)


def select_memory_facts(
    facts: list[str],
    *,
    query: str,
    records: list[Any] | None = None,
    top_k: int = 5,
    min_score: float = 0.05,
) -> tuple[list[str], list[Any], list[str]]:
    if not facts:
        return [], [], []
    scored: list[tuple[float, int, str]] = []
    for i, fact in enumerate(facts):
        score = _score(query, str(fact))
        rec = records[i] if records and i < len(records) else None
        extra = ""
        if rec is not None:
            extra = " ".join(
                str(getattr(rec, key, "") or "")
                for key in ("fact", "type", "memory_type")
            )
            score = max(score, _score(query, extra))
        scored.append((score, i, str(fact)))
    scored.sort(key=lambda item: item[0], reverse=True)
    kept: list[str] = []
    kept_records: list[Any] = []
    dropped: list[str] = []
    for score, idx, fact in scored:
        if len(kept) < top_k and (score >= min_score or not query.strip()):
            kept.append(fact)
            if records and idx < len(records):
                kept_records.append(records[idx])
        else:
            dropped.append(fact)
    if not kept:
        kept = facts[:top_k]
        kept_records = list(records[:top_k]) if records else []
        dropped = facts[top_k:]
    return kept, kept_records, dropped


def select_source_ledger(entries: list[Any], *, query: str, limit: int = 8) -> list[Any]:
    if not entries:
        return []
    if not query.strip():
        return entries[:limit]
    scored: list[tuple[float, Any]] = []
    for entry in entries:
        locator = getattr(entry, "locator", "") or (
            entry.get("locator") if isinstance(entry, dict) else ""
        )
        scored.append((_score(query, str(locator)), entry))
    scored.sort(key=lambda item: item[0], reverse=True)
    picked = [entry for score, entry in scored if score > 0][:limit]
    return picked or entries[:limit]


def format_finding_cards(findings: list[Any], *, max_items: int = 10) -> str:
    if not findings:
        return ""
    lines = ["    【结构化发现 — claim + evidence_id，原文不在窗口内】"]
    for finding in findings[:max_items]:
        claim = getattr(finding, "claim", None) or (
            finding.get("claim") if isinstance(finding, dict) else str(finding)
        )
        ids = getattr(finding, "evidence_ids", None)
        if ids is None and isinstance(finding, dict):
            ids = finding.get("evidence_ids") or []
        cid = getattr(finding, "claim_id", "") or (
            finding.get("claim_id") if isinstance(finding, dict) else ""
        )
        conf = getattr(finding, "confidence", None)
        if conf is None and isinstance(finding, dict):
            conf = finding.get("confidence")
        id_text = ",".join(str(x) for x in (ids or [])[:6]) or "-"
        extra = f" confidence={conf}" if conf is not None else ""
        lines.append(f"  - {cid or 'claim'}: {claim}  evidence=[{id_text}]{extra}")
    lines.append("    数字不确定时调用 read_evidence(evidence_id)，不要臆造。")
    return "\n".join(lines)


def select_step_context(
    *,
    step_type: str,
    task_query: str,
    objective: str = "",
    brief: ResearchBrief | None = None,
    memory_facts: list[str] | None = None,
    memory_records: list[Any] | None = None,
    source_ledger: list[Any] | None = None,
    evidence_store: EvidenceStore | None = None,
    working_notes: str = "",
    jit_enabled: bool = True,
    memory_top_k: int = 5,
    evidence_max_items: int = 12,
    notes_max_chars: int = 1800,
) -> SelectedContext:
    query = _task_query_text(brief, task_query, objective)
    mandatory: dict[str, str] = {}
    optional: dict[str, str] = {}
    dropped: list[str] = []
    evidence_ids: list[str] = []
    memory_ids: list[str] = []

    if brief and not brief.is_empty():
        mandatory["brief"] = brief.to_prompt()

    facts = list(memory_facts or [])
    records = list(memory_records or [])
    ledger = list(source_ledger or [])
    notes = working_notes or ""

    if jit_enabled:
        facts, records, dropped_mem = select_memory_facts(
            facts, query=query, records=records, top_k=memory_top_k
        )
        if dropped_mem:
            dropped.append(f"memory:{len(dropped_mem)}")
        ledger = select_source_ledger(ledger, query=query, limit=8)
        if notes and len(notes) > notes_max_chars:
            dropped.append("notes_trimmed")
            notes = notes[:notes_max_chars] + "\n    [working notes truncated; see working_notes.md]"
    else:
        facts = facts[: memory_top_k * 2]
        records = records[: memory_top_k * 2]
        ledger = ledger[:12]

    store = evidence_store if evidence_store is not None else get_evidence_store()
    findings_block = ""
    evidence_block = ""
    if step_type in SYNTHESIS_STEP_TYPES and store is not None and (store.spans or store.findings):
        findings = store.retrieve_findings(query or objective or task_query, max_items=evidence_max_items)
        findings_block = format_finding_cards(findings, max_items=evidence_max_items)
        evidence_block = store.lookup_block(query=query or objective, max_items=evidence_max_items)
        for finding in findings:
            evidence_ids.extend(list(getattr(finding, "evidence_ids", []) or []))
        for span in store.retrieve(query or objective, max_items=evidence_max_items):
            if span.evidence_id not in evidence_ids:
                evidence_ids.append(span.evidence_id)

    optional["selected_memory_facts"] = "\n".join(facts)  # builder 再格式化
    return SelectedContext(
        mandatory=mandatory,
        optional={
            "notes": notes,
            "findings": findings_block,
            "evidence": evidence_block,
            "memory_facts": "\n".join(facts),
        },
        evidence_ids=evidence_ids,
        memory_ids=[str(getattr(r, "id", i)) for i, r in enumerate(records)],
        dropped=dropped,
    )
