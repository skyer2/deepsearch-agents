"""
【Phase 15】记忆策略 — 租户、召回权重、治理阈值、PII。
【Phase 18】分层策略 — 项目域、信任准入、巩固/衰减、来源台账、步级召回。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.agent.memory.identity import (
    DEFAULT_PROJECT_ID,
    DEFAULT_TENANT_ID,
    MemoryIdentity,
    resolve_memory_identity,
)
from app.agent.memory.provenance import TrustTier, coerce_trust_tier
from app.config.loader import get_harness_config

# 写报告/汇总类步骤：只接受高信任记忆，避免脏结论进入最终交付物。
# 与 harness.orchestration.SYNTHESIS_STEP_TYPES 保持一致（memory 层不能反向依赖
# harness 包，否则 harness/__init__ 会构成循环导入）；一致性由 Phase 18 测试守护。
SYNTHESIS_STEP_TYPES = frozenset({"generate_markdown", "summarize", "convert_pdf"})


@dataclass
class MemoryPolicy:
    enabled: bool = True
    provider: str = "sqlite"
    recall_top_k: int = 5
    ttl_days: int = 90
    max_facts_per_remember: int = 5
    wrap_untrusted: bool = True
    min_fact_chars: int = 12
    embedding_enabled: bool = True
    recall_keyword_weight: float = 0.4
    recall_embedding_weight: float = 0.6
    merge_jaccard_threshold: float = 0.55
    merge_embedding_threshold: float = 0.88
    pii_redact_enabled: bool = True
    step_incremental_enabled: bool = True
    remember_on_partial: bool = False
    # 【Phase 18】身份与分层
    project_scope_enabled: bool = True
    require_explicit_identity: bool = False
    # 【Phase 18】信任准入（反持久化注入）
    min_recall_trust: str = TrustTier.UNTRUSTED.value
    synthesis_min_trust: str = TrustTier.DERIVED.value
    require_provenance_for_step_write: bool = True
    # 【Phase 18】来源台账
    source_ledger_enabled: bool = True
    source_ledger_max_inject: int = 8
    # 【Phase 18】合成步二次召回
    step_recall_enabled: bool = True
    step_recall_top_k: int = 3
    # 【Phase 18】离线巩固
    consolidation_enabled: bool = True
    consolidation_async: bool = True
    consolidation_half_life_days: int = 30
    consolidation_min_confidence: float = 0.25
    consolidation_promote_min_sessions: int = 2
    purge_after_days: int = 180


def get_memory_policy() -> MemoryPolicy:
    cfg = get_harness_config()
    return MemoryPolicy(
        enabled=cfg.memory_enabled,
        provider=cfg.memory_provider,
        recall_top_k=cfg.memory_recall_top_k,
        ttl_days=cfg.memory_ttl_days,
        max_facts_per_remember=cfg.memory_max_facts_per_remember,
        wrap_untrusted=cfg.memory_wrap_untrusted,
        min_fact_chars=cfg.memory_min_fact_chars,
        embedding_enabled=cfg.memory_embedding_enabled,
        recall_keyword_weight=cfg.memory_recall_keyword_weight,
        recall_embedding_weight=cfg.memory_recall_embedding_weight,
        merge_jaccard_threshold=cfg.memory_merge_jaccard_threshold,
        merge_embedding_threshold=cfg.memory_merge_embedding_threshold,
        pii_redact_enabled=cfg.memory_pii_redact_enabled,
        step_incremental_enabled=cfg.memory_step_incremental_enabled,
        remember_on_partial=cfg.memory_remember_on_partial,
        project_scope_enabled=cfg.memory_project_scope_enabled,
        require_explicit_identity=cfg.memory_require_explicit_identity,
        min_recall_trust=cfg.memory_min_recall_trust,
        synthesis_min_trust=cfg.memory_synthesis_min_trust,
        require_provenance_for_step_write=cfg.memory_require_provenance_for_step_write,
        source_ledger_enabled=cfg.memory_source_ledger_enabled,
        source_ledger_max_inject=cfg.memory_source_ledger_max_inject,
        step_recall_enabled=cfg.memory_step_recall_enabled,
        step_recall_top_k=cfg.memory_step_recall_top_k,
        consolidation_enabled=cfg.memory_consolidation_enabled,
        consolidation_async=cfg.memory_consolidation_async,
        consolidation_half_life_days=cfg.memory_consolidation_half_life_days,
        consolidation_min_confidence=cfg.memory_consolidation_min_confidence,
        consolidation_promote_min_sessions=cfg.memory_consolidation_promote_min_sessions,
        purge_after_days=cfg.memory_purge_after_days,
    )


def resolve_memory_tenant_id() -> str:
    """向后兼容入口；实际优先级见 identity.resolve_memory_identity。"""
    explicit = os.getenv("HARNESS_MEMORY_TENANT_ID", "").strip()
    if explicit:
        return explicit
    from app.agent.memory.identity import get_memory_identity

    bound = get_memory_identity()
    return bound.tenant_id if bound else DEFAULT_TENANT_ID


def resolve_memory_user_id(session_id: str) -> str:
    """
    向后兼容入口：优先 HARNESS_MEMORY_USER_ID，其次上下文身份，最后退化 session_id。
    新代码应直接使用 identity.resolve_memory_identity 获取完整四元组。
    """
    explicit = os.getenv("HARNESS_MEMORY_USER_ID", "").strip()
    if explicit:
        return explicit
    from app.agent.memory.identity import get_memory_identity

    bound = get_memory_identity()
    if bound and bound.is_identified:
        return bound.user_id
    return session_id


def resolve_memory_project_id() -> str:
    explicit = os.getenv("HARNESS_MEMORY_PROJECT_ID", "").strip()
    if explicit:
        return explicit
    from app.agent.memory.identity import get_memory_identity

    bound = get_memory_identity()
    return bound.project_id if bound else DEFAULT_PROJECT_ID


def identity_allows_write(identity: MemoryIdentity, policy: MemoryPolicy) -> bool:
    """生产环境可拒绝「session 退化身份」写入，避免多用户记忆串库。"""
    if not policy.enabled:
        return False
    if identity.ephemeral and policy.require_explicit_identity:
        return False
    return True


def synthesis_min_trust_tier(policy: MemoryPolicy) -> TrustTier:
    return coerce_trust_tier(policy.synthesis_min_trust, default=TrustTier.DERIVED)


def min_recall_trust_tier(policy: MemoryPolicy) -> TrustTier:
    return coerce_trust_tier(policy.min_recall_trust, default=TrustTier.UNTRUSTED)


__all__ = [
    "MemoryIdentity",
    "MemoryPolicy",
    "SYNTHESIS_STEP_TYPES",
    "get_memory_policy",
    "identity_allows_write",
    "min_recall_trust_tier",
    "resolve_memory_identity",
    "resolve_memory_project_id",
    "resolve_memory_tenant_id",
    "resolve_memory_user_id",
    "synthesis_min_trust_tier",
]
