"""
【Phase 15】Hybrid Recall — 关键词 + Embedding + Rerank。
【Phase 18】信任分级加权 + 项目域加成 + 合成步准入过滤 + 指标。
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

from app.agent.memory.governance import cosine_similarity
from app.agent.memory.models import MemoryRecord, MemoryType, RecallResult, RECALLABLE_TYPES
from app.agent.memory.policy import MemoryPolicy, SYNTHESIS_STEP_TYPES
from app.agent.memory.provenance import (
    TRUST_RECALL_WEIGHT,
    TrustTier,
    coerce_trust_tier,
    is_recall_eligible,
)
from app.agent.memory.recall.embedding import embed_text


def _keyword_score(query: str, fact: str) -> float:
    q_tokens = [t for t in query.lower().split() if t]
    fact_lower = fact.lower()
    if q_tokens:
        hits = sum(1 for t in q_tokens if t in fact_lower)
        return hits / len(q_tokens)
    compact_q = "".join(ch for ch in query.lower() if not ch.isspace())
    if len(compact_q) < 2:
        return 0.0
    grams = [compact_q[i : i + 2] for i in range(len(compact_q) - 1)]
    if not grams:
        return 0.0
    hits = sum(1 for g in grams if g in fact_lower)
    return hits / len(grams)


def _recency_boost(created_at: str) -> float:
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        age_days = max(0, (datetime.now(timezone.utc) - created).days)
        return 1.0 / (1.0 + math.log1p(age_days))
    except ValueError:
        return 0.5


def _type_boost(memory_type: MemoryType, query: str) -> float:
    q = query.lower()
    if memory_type == MemoryType.PREFERENCE and any(
        k in q for k in ("偏好", "喜欢", "习惯", "prefer", "交付")
    ):
        return 0.15
    if memory_type == MemoryType.PROCEDURAL and any(
        k in q for k in ("流程", "步骤", "怎么", "how to", "改过", "拒绝")
    ):
        return 0.1
    return 0.0


def _project_boost(record: MemoryRecord, project_id: str, policy: MemoryPolicy) -> float:
    if not getattr(policy, "project_scope_enabled", True):
        return 0.0
    if not project_id or project_id == "default":
        return 0.0
    if record.project_id == project_id:
        return 0.12
    return 0.0


async def hybrid_recall(
    query: str,
    records: list[MemoryRecord],
    *,
    policy: MemoryPolicy,
    top_k: int,
    memory_types: Optional[list[MemoryType]] = None,
    project_id: str = "",
    target_step_type: str = "",
) -> RecallResult:
    allowed_types = set(memory_types) if memory_types else set(RECALLABLE_TYPES)
    active = [
        r
        for r in records
        if not r.is_deleted
        and not r.superseded_by
        and not r.is_expired(policy.ttl_days)
        and r.memory_type in allowed_types
    ]

    candidates = len(active)
    synthesis_types = getattr(policy, "synthesis_step_types", SYNTHESIS_STEP_TYPES) or SYNTHESIS_STEP_TYPES
    eligible: list[MemoryRecord] = []
    trust_filtered = 0
    for record in active:
        if is_recall_eligible(
            record,
            min_trust=getattr(policy, "min_recall_trust", TrustTier.UNTRUSTED),
            target_step_type=target_step_type,
            synthesis_step_types=synthesis_types,
            synthesis_min_trust=getattr(policy, "synthesis_min_trust", TrustTier.DERIVED),
        ):
            eligible.append(record)
        else:
            trust_filtered += 1

    empty = RecallResult(
        records=[],
        recall_at_k=0.0,
        keyword_hits=0,
        embedding_used=False,
        candidates=candidates,
        trust_filtered=trust_filtered,
    )
    if not eligible:
        return empty

    query_embedding = await embed_text(query) if policy.embedding_enabled else None
    embedding_used = query_embedding is not None

    kw_weight = policy.recall_keyword_weight
    emb_weight = policy.recall_embedding_weight
    if not embedding_used:
        kw_weight, emb_weight = 1.0, 0.0

    scored: list[tuple[float, MemoryRecord, float]] = []
    keyword_hits = 0
    for record in eligible:
        kw = _keyword_score(query, record.fact)
        if kw > 0:
            keyword_hits += 1
        emb = 0.0
        if query_embedding and record.embedding:
            emb = cosine_similarity(query_embedding, record.embedding)
        base = kw_weight * kw + emb_weight * emb
        trust = coerce_trust_tier(record.trust_tier)
        trust_weight = TRUST_RECALL_WEIGHT.get(trust, 0.8)
        rerank = (
            base
            + 0.1 * _recency_boost(record.created_at)
            + _type_boost(record.memory_type, query)
            + _project_boost(record, project_id, policy)
        )
        rerank *= 0.5 + 0.5 * min(1.0, record.confidence)
        rerank *= trust_weight
        record.recall_score = round(rerank, 4)
        scored.append((rerank, record, kw))

    scored.sort(key=lambda x: x[0], reverse=True)

    if scored and scored[0][0] <= 0 and not embedding_used:
        fallback = sorted(eligible, key=lambda r: r.updated_at)[-top_k:]
        for r in fallback:
            r.recall_score = 0.01
        selected = fallback[-top_k:]
    else:
        selected = [r for _, r, _ in scored[:top_k]]

    recall_at_k = 0.0
    if selected:
        recall_at_k = sum(r.recall_score or 0.0 for r in selected) / len(selected)

    by_trust = dict(Counter(coerce_trust_tier(r.trust_tier).value for r in selected))
    by_type = dict(Counter(r.type_label() for r in selected))
    return RecallResult(
        records=selected,
        recall_at_k=round(recall_at_k, 4),
        keyword_hits=keyword_hits,
        embedding_used=embedding_used,
        candidates=candidates,
        trust_filtered=trust_filtered,
        by_trust=by_trust,
        by_type=by_type,
    )
