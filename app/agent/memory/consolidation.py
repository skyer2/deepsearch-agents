"""
【Phase 18/24】记忆巩固 — ADD / UPDATE / SUPERSEDE / DELETE / NOOP + 衰减 + 确认晋升。

参考过经典 Memory consolidation 的 ADD/UPDATE/DELETE 思路，但 Deep Research
需要审计历史，所以扩展了 SUPERSEDE。Mem0 最新实现已演进为 ADD-only extraction；
SUPERSEDE 是本仓的 domain decision，不是「对齐 Mem0 最新算法」。

Trust 晋升只接受独立证据或人工确认，recall_count 只用于 utility/衰减，不作为 truth。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from app.agent.memory.governance import find_merge_candidate, looks_contradictory, merge_record
from app.agent.memory.models import MemoryRecord, MemoryWriteRequest, WriteSource
from app.agent.memory.policy import MemoryPolicy
from app.agent.memory.provenance import TrustTier, coerce_trust_tier
from app.agent.memory.validity import extract_fact_frame


class ConsolidationAction(str, Enum):
    ADD = "add"
    UPDATE = "update"
    SUPERSEDE = "supersede"
    DELETE = "delete"
    NOOP = "noop"
    DECAY = "decay"
    PROMOTE = "promote"
    PURGE = "purge"


@dataclass
class ConsolidationDecision:
    action: ConsolidationAction
    target: Optional[MemoryRecord] = None
    reason: str = ""


_CONTRADICTION_PAIRS = ()  # 矛盾检测已下沉到 governance.looks_contradictory


def decide_write_action(
    write: MemoryWriteRequest,
    existing: list[MemoryRecord],
    *,
    policy: MemoryPolicy,
    new_embedding: Optional[list[float]] = None,
) -> ConsolidationDecision:
    """写入时决定 ADD / UPDATE / SUPERSEDE / NOOP。"""
    candidate = find_merge_candidate(
        write,
        existing,
        jaccard_threshold=policy.merge_jaccard_threshold,
        embedding_threshold=policy.merge_embedding_threshold,
        new_embedding=new_embedding,
    )
    if candidate is None:
        return ConsolidationDecision(action=ConsolidationAction.ADD, reason="no_similar")

    if write.fact.strip() == candidate.fact.strip():
        source = write.write_source.value if isinstance(write.write_source, WriteSource) else str(write.write_source)
        if source == WriteSource.CONFIRMATION.value:
            return ConsolidationDecision(
                action=ConsolidationAction.UPDATE,
                target=candidate,
                reason="independent_confirmation",
            )
        return ConsolidationDecision(
            action=ConsolidationAction.NOOP,
            target=candidate,
            reason="exact_duplicate",
        )

    write_trust = write.resolved_trust_tier()
    existing_trust = coerce_trust_tier(candidate.trust_tier)
    if _TRUST_RANK(write_trust) < _TRUST_RANK(existing_trust):
        return ConsolidationDecision(
            action=ConsolidationAction.NOOP,
            target=candidate,
            reason="lower_trust_cannot_overwrite",
        )

    frame_a = extract_fact_frame(write.fact)
    frame_b = extract_fact_frame(candidate.fact)
    write_valid = (write.valid_time or frame_a.valid_time).strip()
    exist_valid = (candidate.valid_time or frame_b.valid_time).strip()
    if looks_contradictory(write.fact, candidate.fact, frame_a=frame_a, frame_b=frame_b):
        return ConsolidationDecision(
            action=ConsolidationAction.SUPERSEDE,
            target=candidate,
            reason="contradiction",
        )
    if write_valid and exist_valid and write_valid != exist_valid:
        return ConsolidationDecision(action=ConsolidationAction.ADD, reason="different_valid_time")
    return ConsolidationDecision(
        action=ConsolidationAction.UPDATE,
        target=candidate,
        reason="similar_refresh",
    )


def _TRUST_RANK(tier: TrustTier) -> int:
    return {TrustTier.UNTRUSTED: 0, TrustTier.DERIVED: 1, TrustTier.TRUSTED: 2}[tier]


def apply_update(existing: MemoryRecord, write: MemoryWriteRequest) -> MemoryRecord:
    merged = merge_record(existing, write)
    source = write.write_source.value if isinstance(write.write_source, WriteSource) else str(write.write_source)
    if source != WriteSource.CONFIRMATION.value:
        merged.trust_tier = write.resolved_trust_tier()
        merged.provenance = write.resolved_provenance() or merged.provenance
    if write.project_id:
        merged.project_id = write.project_id
    if write.dedup_key:
        merged.dedup_key = write.dedup_key
    merge_independent_confirmation(merged, write)
    apply_validity_fields(merged, write)
    if write.human_confirmed:
        merged.human_confirmed = True
        merged.trust_tier = TrustTier.TRUSTED
    merged.metadata = {**merged.metadata, "consolidated": ConsolidationAction.UPDATE.value}
    return merged


def _source_ids_from_write(write: MemoryWriteRequest) -> list[str]:
    prov = write.resolved_provenance()
    ids: list[str] = []
    ids.extend(prov.source_urls)
    ids.extend(prov.evidence_ids)
    locator = prov.primary_locator()
    if locator:
        ids.append(locator)
    return [item for item in ids if item]


def merge_independent_confirmation(record: MemoryRecord, write: MemoryWriteRequest) -> None:
    """新来源独立确认：加入 confirmed_by_source_ids，不把 recall 当证明。"""
    incoming = _source_ids_from_write(write)
    known = list(record.confirmed_by_source_ids)
    if record.provenance.primary_locator() and record.provenance.primary_locator() not in known:
        known.append(record.provenance.primary_locator())
    for sid in incoming:
        if sid not in known:
            known.append(sid)
    record.confirmed_by_source_ids = known
    record.confirmation_count = len(known)
    if write.last_verified_at:
        record.last_verified_at = write.last_verified_at
    elif incoming:
        from datetime import datetime, timezone

        record.last_verified_at = datetime.now(timezone.utc).isoformat()


def apply_validity_fields(record: MemoryRecord, write: MemoryWriteRequest) -> None:
    frame = extract_fact_frame(write.fact)
    record.as_of = write.as_of or record.as_of or record.created_at
    record.valid_from = write.valid_from or record.valid_from
    record.valid_to = write.valid_to or record.valid_to
    record.valid_time = write.valid_time or frame.valid_time or record.valid_time
    record.observed_at = write.observed_at or record.observed_at or record.created_at
    record.source_updated_at = write.source_updated_at or record.source_updated_at
    record.entity = write.entity or frame.entity or record.entity
    record.attribute = write.attribute or frame.attribute or record.attribute
    record.value_text = write.value_text or frame.value or record.value_text
    if write.idempotency_key:
        record.idempotency_key = write.idempotency_key


def decay_confidence(record: MemoryRecord, *, half_life_days: int, floor: float) -> float:
    if half_life_days <= 0:
        return record.confidence
    age = record.age_days()
    if age <= 0:
        return record.confidence
    halves = age / float(half_life_days)
    # recall_count 只作 popularity/utility，减缓衰减，不提高 trust
    recalled_boost = min(1.0, 0.15 * record.recall_count)
    decayed = record.confidence * (0.5 ** halves) * (1.0 + recalled_boost)
    return max(floor, min(1.0, decayed))


def independent_source_count(record: MemoryRecord) -> int:
    sources = {str(s) for s in (record.confirmed_by_source_ids or []) if s}
    locator = record.provenance.primary_locator() if record.provenance else ""
    if locator:
        sources.add(locator)
    for url in record.provenance.source_urls if record.provenance else []:
        if url:
            sources.add(url)
    for eid in record.provenance.evidence_ids if record.provenance else []:
        if eid:
            sources.add(eid)
    return len(sources)


def should_promote(record: MemoryRecord, *, min_sessions: int = 2, min_confirmations: int | None = None) -> bool:
    """独立证据或人工确认才可晋升 trusted。recall_count / 跨 session 看见 ≠ 被证明。"""
    if coerce_trust_tier(record.trust_tier) != TrustTier.DERIVED:
        return False
    if record.write_source in {WriteSource.USER_EXPLICIT, WriteSource.HITL, WriteSource.SEED}:
        return False
    if record.human_confirmed:
        return True
    needed = min_confirmations if min_confirmations is not None else max(2, min_sessions)
    return independent_source_count(record) >= needed


def should_purge(record: MemoryRecord, *, purge_after_days: int) -> bool:
    if purge_after_days <= 0:
        return False
    if not record.is_deleted:
        return False
    try:
        updated = datetime.fromisoformat(record.updated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    age = (datetime.now(timezone.utc) - updated).days
    return age >= purge_after_days


@dataclass
class ConsolidationReport:
    decayed: int = 0
    promoted: int = 0
    purged: int = 0
    examined: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "decayed": self.decayed,
            "promoted": self.promoted,
            "purged": self.purged,
            "examined": self.examined,
        }
