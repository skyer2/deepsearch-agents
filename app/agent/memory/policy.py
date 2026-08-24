"""
【Phase 15】记忆策略 — 租户、召回权重、治理阈值、PII。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.config.loader import get_harness_config


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
    )


def resolve_memory_tenant_id() -> str:
    explicit = os.getenv("HARNESS_MEMORY_TENANT_ID", "").strip()
    if explicit:
        return explicit
    return "default"


def resolve_memory_user_id(session_id: str) -> str:
    """
    解析长期记忆 user_id。
    优先 HARNESS_MEMORY_USER_ID（企业：真实用户 ID）；
    否则用 session_id（demo：同 thread_id 跨任务共享记忆）。
    """
    explicit = os.getenv("HARNESS_MEMORY_USER_ID", "").strip()
    if explicit:
        return explicit
    return session_id
