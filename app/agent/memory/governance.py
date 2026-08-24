"""
【Phase 15】记忆治理 — 冲突合并、版本递增、相似度判定。
【Phase 18】合并保留历史版本、信任等级取高、来源台账走精确去重、矛盾检测。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from app.agent.memory.models import MemoryRecord, MemoryType, MemoryWriteRequest
from app.agent.memory.provenance import coerce_trust_tier, trust_at_least

MAX_FACT_HISTORY = 5

_NUMBER_PATTERN = re.compile(r"-?\d+(?:\.\d+)?%?")
_NEGATION_TOKENS = ("不", "无", "没有", "未", "非", "not ", "no ")


def _token_set(text: str) -> set[str]:
    tokens = {t for t in text.lower().split() if len(t) >= 2}
    compact = "".join(ch for ch in text.lower() if not ch.isspace())
    for i in range(max(0, len(compact) - 1)):
        gram = compact[i : i + 2]
        if any("\u4e00" <= c <= "\u9fff" for c in gram):
            tokens.add(gram)
    return tokens


def token_jaccard(a: str, b: str) -> float:
    sa, sb = _token_set(a), _token_set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def looks_contradictory(a: str, b: str) -> bool:
    """高相似主题但数字显著不同或含否定对 → 视为矛盾，应 SUPERSEDE。"""
    if token_jaccard(a, b) < 0.35:
        return False
    la, lb = a.lower(), b.lower()
    nums_a = _NUMBER_PATTERN.findall(la)
    nums_b = _NUMBER_PATTERN.findall(lb)
    if nums_a and nums_b and set(nums_a) != set(nums_b):
        return True
    for token in _NEGATION_TOKENS:
        in_a = token in la
        in_b = token in lb
        if in_a != in_b and token_jaccard(a, b) >= 0.45:
            return True
    return False


def find_merge_candidate(
    write: MemoryWriteRequest,
    existing: list[MemoryRecord],
    *,
    jaccard_threshold: float = 0.55,
    embedding_threshold: float = 0.88,
    new_embedding: Optional[list[float]] = None,
) -> Optional[MemoryRecord]:
    """查找应合并更新的已有记录。

    优先级：
    1. 同类型 + 同 dedup_key（来源台账精确去重）
    2. 同类型 + 同项目优先的 Jaccard / embedding 相似度
    """
    if write.dedup_key and write.memory_type == MemoryType.SOURCE:
        for record in existing:
            if record.is_deleted:
                continue
            if record.memory_type == MemoryType.SOURCE and record.dedup_key == write.dedup_key:
                return record

    best: Optional[MemoryRecord] = None
    best_score = 0.0
    write_project = (write.project_id or "").strip()
    for record in existing:
        if record.is_deleted or record.superseded_by:
            continue
        if record.memory_type != write.memory_type:
            continue
        j_score = token_jaccard(write.fact, record.fact)
        e_score = 0.0
        if new_embedding and record.embedding:
            e_score = cosine_similarity(new_embedding, record.embedding)
        combined = max(j_score, e_score)
        if write_project and record.project_id == write_project:
            combined += 0.05
        threshold = embedding_threshold if new_embedding and record.embedding else jaccard_threshold
        if combined >= threshold and combined > best_score:
            best_score = combined
            best = record
    return best


def merge_record(existing: MemoryRecord, write: MemoryWriteRequest) -> MemoryRecord:
    """冲突合并：新版本覆盖 fact，保留 id，递增 version，归档旧 fact。"""
    old_fact = existing.fact
    history = list(existing.metadata.get("fact_history") or [])
    if old_fact and old_fact != write.fact.strip():
        history.append(
            {
                "fact": old_fact,
                "version": existing.version,
                "updated_at": existing.updated_at,
                "trust_tier": existing.trust_label(),
            }
        )
        existing.metadata["fact_history"] = history[-MAX_FACT_HISTORY:]

    existing.fact = write.fact.strip()
    existing.version += 1
    existing.confidence = max(existing.confidence, write.confidence)
    existing.updated_at = datetime.now(timezone.utc).isoformat()
    write_trust = write.resolved_trust_tier()
    if trust_at_least(write_trust, existing.trust_tier):
        existing.trust_tier = write_trust
    existing.provenance = write.resolved_provenance() or existing.provenance
    if write.project_id:
        existing.project_id = write.project_id
    if write.dedup_key:
        existing.dedup_key = write.dedup_key
    if write.task:
        existing.task = write.task
    if write.topic:
        existing.topic = write.topic
    if write.session_id:
        existing.session_id = write.session_id
    existing.metadata = {**existing.metadata, **write.metadata, "merged": True}
    return existing
