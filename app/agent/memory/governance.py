"""
【Phase 15】记忆治理 — 冲突合并、版本递增、相似度判定。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.agent.memory.models import MemoryRecord, MemoryWriteRequest


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


def find_merge_candidate(
    write: MemoryWriteRequest,
    existing: list[MemoryRecord],
    *,
    jaccard_threshold: float = 0.55,
    embedding_threshold: float = 0.88,
    new_embedding: Optional[list[float]] = None,
) -> Optional[MemoryRecord]:
    """查找应合并更新的已有记录（同类型优先）。"""
    best: Optional[MemoryRecord] = None
    best_score = 0.0
    for record in existing:
        if record.is_deleted:
            continue
        if record.memory_type != write.memory_type:
            continue
        j_score = token_jaccard(write.fact, record.fact)
        e_score = 0.0
        if new_embedding and record.embedding:
            e_score = cosine_similarity(new_embedding, record.embedding)
        combined = max(j_score, e_score)
        threshold = embedding_threshold if new_embedding and record.embedding else jaccard_threshold
        if combined >= threshold and combined > best_score:
            best_score = combined
            best = record
    return best


def merge_record(existing: MemoryRecord, write: MemoryWriteRequest) -> MemoryRecord:
    """冲突合并：新版本覆盖 fact，保留 id，递增 version。"""
    existing.fact = write.fact.strip()
    existing.version += 1
    existing.confidence = max(existing.confidence, write.confidence)
    existing.updated_at = datetime.now(timezone.utc).isoformat()
    if write.task:
        existing.task = write.task
    if write.topic:
        existing.topic = write.topic
    if write.session_id:
        existing.session_id = write.session_id
    existing.metadata = {**existing.metadata, **write.metadata, "merged": True}
    return existing
