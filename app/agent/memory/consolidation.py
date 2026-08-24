"""
【Phase 18】记忆巩固 — ADD / UPDATE / SUPERSEDE / DELETE / NOOP + 衰减 + 晋升。

对齐 Mem0 的写入动作语义，但面向深度研搜做了两处特化：
1. SUPERSEDE 保留旧记录（软删 + 取代链），以便审计「结论何时被推翻」
2. 低信任记忆不会被晋升；用户/HITL 写入默认 TRUSTED，可覆盖同主题低信任 fact
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

    if looks_contradictory(write.fact, candidate.fact):
        return ConsolidationDecision(
            action=ConsolidationAction.SUPERSEDE,
            target=candidate,
            reason="contradiction",
        )
    return ConsolidationDecision(
        action=ConsolidationAction.UPDATE,
        target=candidate,
        reason="similar_refresh",
    )


def _TRUST_RANK(tier: TrustTier) -> int:
    return {TrustTier.UNTRUSTED: 0, TrustTier.DERIVED: 1, TrustTier.TRUSTED: 2}[tier]


def apply_update(existing: MemoryRecord, write: MemoryWriteRequest) -> MemoryRecord:
    merged = merge_record(existing, write)
    merged.trust_tier = write.resolved_trust_tier()
    merged.provenance = write.resolved_provenance() or merged.provenance
    if write.project_id:
        merged.project_id = write.project_id
    if write.dedup_key:
        merged.dedup_key = write.dedup_key
    merged.metadata = {**merged.metadata, "consolidated": ConsolidationAction.UPDATE.value}
    return merged


def decay_confidence(record: MemoryRecord, *, half_life_days: int, floor: float) -> float:
    if half_life_days <= 0:
        return record.confidence
    age = record.age_days()
    if age <= 0:
        return record.confidence
    # 每过一个半衰期打五折，但不低于 floor；被 recall 过的记忆衰减更慢
    halves = age / float(half_life_days)
    recalled_boost = min(1.0, 0.15 * record.recall_count)
    decayed = record.confidence * (0.5 ** halves) * (1.0 + recalled_boost)
    return max(floor, min(1.0, decayed))


def should_promote(record: MemoryRecord, *, min_sessions: int) -> bool:
    """跨 session 多次命中且本身已是 derived → 晋升 trusted。"""
    if coerce_trust_tier(record.trust_tier) != TrustTier.DERIVED:
        return False
    if record.write_source in {WriteSource.USER_EXPLICIT, WriteSource.HITL, WriteSource.SEED}:
        return False
    sessions = record.metadata.get("seen_sessions") or []
    if isinstance(sessions, list) and len({str(s) for s in sessions}) >= min_sessions:
        return True
    return record.recall_count >= max(3, min_sessions + 1)


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
