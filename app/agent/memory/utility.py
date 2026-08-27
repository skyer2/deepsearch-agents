"""
Future Utility Gate — 长期记忆写入的「以后真的值得记吗」门槛。

Deep Research 产生的信息默认进入 Evidence / Source Ledger / Working Notes。
只有通过 provenance、时效和复用价值判断的候选，才允许进入 Long-term Memory。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agent.memory.models import MemoryType, MemoryWriteRequest, WriteSource
from app.agent.memory.policy import MemoryPolicy
from app.agent.memory.provenance import TrustTier
from app.agent.memory.validity import is_volatile_fact

_DURABLE_MARKERS = (
    "以后",
    "必须",
    "不要",
    "偏好",
    "习惯",
    "官方来源",
    "markdown",
    "pdf",
    "prefer",
    "always",
    "never",
)


@dataclass
class UtilityDecision:
    keep: bool
    reason: str
    volatile: bool = False


def _source_value(write: MemoryWriteRequest) -> str:
    source = write.write_source
    return source.value if isinstance(source, WriteSource) else str(source)


def passes_utility_gate(write: MemoryWriteRequest, policy: MemoryPolicy) -> UtilityDecision:
    """判断一条候选是否值得进入长期 Memory。"""
    if not getattr(policy, "utility_gate_enabled", True):
        return UtilityDecision(keep=True, reason="gate_disabled")

    source = _source_value(write)
    trust = write.resolved_trust_tier()
    fact = (write.fact or "").strip()
    volatile = is_volatile_fact(fact)

    if source in {
        WriteSource.USER_EXPLICIT.value,
        WriteSource.HITL.value,
        WriteSource.SEED.value,
        WriteSource.CONFIRMATION.value,
    }:
        return UtilityDecision(keep=True, reason="explicit_or_hitl", volatile=False)

    if write.memory_type in {MemoryType.PREFERENCE, MemoryType.PROCEDURAL}:
        return UtilityDecision(keep=True, reason="durable_type", volatile=False)

    if source == WriteSource.STEP_INCREMENTAL.value:
        if not getattr(policy, "step_incremental_write_longterm", False):
            return UtilityDecision(keep=False, reason="step_incremental_deferred", volatile=volatile)
        if trust == TrustTier.UNTRUSTED:
            return UtilityDecision(keep=False, reason="untrusted_external", volatile=volatile)

    lowered = fact.lower()
    if any(marker in lowered for marker in _DURABLE_MARKERS):
        return UtilityDecision(keep=True, reason="durable_language", volatile=False)

    if source == WriteSource.FINALIZE.value:
        prov = write.resolved_provenance()
        if trust == TrustTier.UNTRUSTED and not prov.has_evidence:
            return UtilityDecision(keep=False, reason="unverified_finalize", volatile=volatile)
        if volatile and trust == TrustTier.UNTRUSTED:
            return UtilityDecision(keep=False, reason="volatile_untrusted", volatile=True)
        return UtilityDecision(keep=True, reason="finalize_curated", volatile=volatile)

    if trust == TrustTier.UNTRUSTED:
        return UtilityDecision(keep=False, reason="untrusted_not_reusable", volatile=volatile)
    return UtilityDecision(keep=True, reason="derived_default", volatile=volatile)


def filter_longterm_writes(
    writes: list[MemoryWriteRequest],
    policy: MemoryPolicy,
) -> tuple[list[MemoryWriteRequest], int]:
    """过滤长期写入；被拒的不落 MemoryStore。返回 (kept, rejected_count)。"""
    kept: list[MemoryWriteRequest] = []
    rejected = 0
    for write in writes:
        decision = passes_utility_gate(write, policy)
        if not decision.keep:
            rejected += 1
            continue
        if decision.volatile:
            write.metadata = {**write.metadata, "volatile": True}
        kept.append(write)
    return kept, rejected
