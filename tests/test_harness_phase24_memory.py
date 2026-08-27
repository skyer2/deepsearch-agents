"""Phase 24: Deep Research Memory 生产门禁 — 确认晋升、CJK 召回、时效、Utility Gate。"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.memory.backend.sqlite_backend import SqliteMemoryBackend
from app.agent.memory.consolidation import should_promote
from app.agent.memory.eval import evaluate_retrieval
from app.agent.memory.extractor import MemoryExtractor
from app.agent.memory.governance import looks_contradictory
from app.agent.memory.identity import MemoryIdentity
from app.agent.memory.models import MemoryRecord, MemoryType, MemoryWriteRequest, WriteSource
from app.agent.memory.policy import MemoryPolicy, identity_allows_write
from app.agent.memory.provenance import Provenance, TrustTier
from app.agent.memory.recall.hybrid import hybrid_recall
from app.agent.memory.recall.tokenizer import keyword_overlap
from app.agent.memory.store import MemoryStore
from app.agent.memory.utility import passes_utility_gate
from app.agent.memory.validity import record_is_expired, source_needs_refresh
from app.config.loader import reload_harness_config


def _policy(**kwargs) -> MemoryPolicy:
    defaults = dict(
        provider="sqlite",
        ttl_days=90,
        min_fact_chars=10,
        max_facts_per_remember=5,
        embedding_enabled=False,
        require_explicit_identity=True,
        min_recall_trust=TrustTier.DERIVED.value,
        utility_gate_enabled=True,
        step_incremental_write_longterm=False,
        consolidation_durable=True,
        consolidation_enabled=True,
        source_ledger_enabled=True,
        source_freshness_days=7,
        volatile_semantic_ttl_days=7,
    )
    defaults.update(kwargs)
    return MemoryPolicy(**defaults)


async def _run():
    cfg = reload_harness_config()
    assert cfg.memory_min_recall_trust == "derived"
    assert cfg.memory_require_explicit_identity is True
    assert cfg.memory_step_incremental_write_longterm is False
    print("[OK] production defaults: derived recall + explicit identity")

    ephemeral = MemoryIdentity(user_id="sess-1", ephemeral=True)
    assert not identity_allows_write(ephemeral, _policy())
    print("[OK] require_explicit_identity blocks session fallback")

    rec = MemoryRecord(
        fact="Figure 2026 年计划生产 100 万台",
        trust_tier=TrustTier.DERIVED,
        recall_count=9,
        metadata={"seen_sessions": ["a", "b", "c"]},
    )
    assert should_promote(rec, min_sessions=2) is False
    rec.human_confirmed = True
    assert should_promote(rec, min_sessions=2) is True
    rec.human_confirmed = False
    rec.confirmed_by_source_ids = ["https://a.example/1", "https://b.example/2"]
    assert should_promote(rec, min_sessions=2) is True
    print("[OK] trust promotion requires independent confirmation, not recall_count")

    step_write = MemoryWriteRequest(
        fact="工业机器人2025年库存周转天数下降到 42 天。",
        memory_type=MemoryType.EPISODIC,
        write_source=WriteSource.STEP_INCREMENTAL,
        provenance=Provenance(source_kind="url", source_urls=["https://example.com/r"], step_type="network_search"),
    )
    assert passes_utility_gate(step_write, _policy()).keep is False
    pref = MemoryWriteRequest(
        fact="用户要求以后所有市场报告必须使用官方来源",
        memory_type=MemoryType.PREFERENCE,
        write_source=WriteSource.USER_EXPLICIT,
    )
    assert passes_utility_gate(pref, _policy()).keep is True
    print("[OK] utility gate defers step incremental, keeps preference")

    extractor = MemoryExtractor(model=None)
    candidates = extractor.extract_step_writes(
        "工业机器人2025年库存周转天数下降到 42 天。https://example.com/r",
        "network_search",
        provenance=Provenance(
            source_kind="url",
            source_urls=["https://example.com/r"],
            step_type="network_search",
            citation_count=1,
        ),
    )
    assert candidates
    print("[OK] step extractor still produces candidates for evidence/ledger")

    assert keyword_overlap("人形机器人市场规模", "2025年人形机器人全球市场规模约80亿美元") > 0.3
    print("[OK] CJK keyword overlap")

    assert not looks_contradictory("2025年营收100亿", "2026年营收120亿")
    assert looks_contradictory("2025年营收100亿", "2025年营收120亿")
    print("[OK] conflict uses valid_time")

    metrics = evaluate_retrieval(["a", "c", "d"], ["a", "b"], k=2)
    assert metrics.recall_at_k == 0.5
    assert metrics.precision_at_k == 0.5
    assert metrics.mrr == 1.0
    print("[OK] true Recall@K / MRR")

    pref_old = MemoryRecord(
        fact="用户以后输出 Markdown",
        memory_type=MemoryType.PREFERENCE,
        created_at=(datetime.now(timezone.utc) - timedelta(days=400)).isoformat(),
        write_source=WriteSource.USER_EXPLICIT,
        trust_tier=TrustTier.TRUSTED,
    )
    assert record_is_expired(pref_old, _policy()) is False
    volatile = MemoryRecord(
        fact="某公司预计2026年产量10万台",
        memory_type=MemoryType.SEMANTIC,
        created_at=(datetime.now(timezone.utc) - timedelta(days=20)).isoformat(),
        metadata={"volatile": True},
    )
    assert record_is_expired(volatile, _policy()) is True
    print("[OK] type-specific TTL")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        policy = _policy()
        backend = SqliteMemoryBackend(Path(tmp) / "memory.db", policy)
        store = MemoryStore(backend=backend, policy=policy)
        alice = MemoryIdentity(tenant_id="acme", user_id="alice", project_id="robots", session_id="s1")

        saved_step = await store.remember_writes([step_write], user_id=alice.user_id, identity=alice)
        assert saved_step == 0
        print("[OK] untrusted step incremental does not enter long-term memory")

        await store.remember_writes([pref], user_id=alice.user_id, identity=alice)
        db_write = MemoryWriteRequest(
            fact="内部库显示工业机器人SKU-9库存 1200 台",
            memory_type=MemoryType.SEMANTIC,
            write_source=WriteSource.FINALIZE,
            project_id="robots",
            provenance=Provenance(source_kind="sql", step_type="database_query", evidence_ids=["src-1"]),
            as_of="2026-08-01T00:00:00+00:00",
            valid_time="2026",
            idempotency_key="idem-sku9",
        )
        first = await store.remember_writes([db_write], user_id=alice.user_id, identity=alice)
        second = await store.remember_writes(
            [
                MemoryWriteRequest(
                    fact="内部库显示工业机器人SKU-9库存 1200 台（复核）",
                    memory_type=MemoryType.SEMANTIC,
                    write_source=WriteSource.FINALIZE,
                    project_id="robots",
                    provenance=Provenance(
                        source_kind="sql", step_type="database_query", evidence_ids=["src-2"]
                    ),
                    idempotency_key="idem-sku9",
                )
            ],
            user_id=alice.user_id,
            identity=alice,
        )
        assert first == 1 and second == 0
        print("[OK] idempotent write")

        recalled = await store.recall_with_metrics("工业机器人库存", alice.user_id, identity=alice)
        assert recalled.mean_recall_score >= 0
        assert all(r.trust_tier != TrustTier.UNTRUSTED for r in recalled.records)
        assert any("SKU-9" in r.fact for r in recalled.records)
        sku = next(r for r in store.list_records(alice.user_id, identity=alice) if "SKU-9" in r.fact)
        assert sku.as_of and sku.valid_time == "2026"
        print("[OK] derived-only recall + as_of/valid_time persisted")

        hybrid = await hybrid_recall(
            "人形机器人市场规模",
            [
                MemoryRecord(
                    fact="2025年人形机器人全球市场规模约80亿美元",
                    memory_type=MemoryType.SEMANTIC,
                    trust_tier=TrustTier.DERIVED,
                    provenance=Provenance(evidence_ids=["e1"]),
                )
            ],
            policy=policy,
            top_k=3,
        )
        assert hybrid.records and hybrid.keyword_hits >= 1
        print("[OK] CJK hybrid keyword channel")

        recorded = await store.record_sources(
            ["https://example.com/report"],
            identity=alice,
            source_kind="url",
            metadata={"content_fingerprint": "abc", "query_purpose": "market-size"},
        )
        assert recorded >= 1
        ledger = store.list_sources(identity=alice)
        assert ledger[0].content_fingerprint == "abc"
        ledger[0].last_checked_at = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        assert source_needs_refresh(ledger[0], freshness_days=7) is True
        print("[OK] source ledger freshness")

        job_id = store.enqueue_consolidation(user_id=alice.user_id, identity=alice)
        assert job_id
        drained = await store.drain_jobs()
        assert drained["claimed"] >= 1
        print("[OK] durable consolidation job")

        ok = await store.confirm_record(
            sku.id,
            user_id=alice.user_id,
            identity=alice,
            source_id="https://internal.example/sql",
            human=True,
        )
        assert ok
        after = next(r for r in store.list_records(alice.user_id, identity=alice) if r.id == sku.id)
        assert after.human_confirmed or after.trust_tier == TrustTier.TRUSTED
        print("[OK] human confirmation promotes trust")

    print("[OK] postgres backend importable")
    from app.agent.memory.backend.postgres_backend import PostgresMemoryBackend

    try:
        PostgresMemoryBackend("", _policy())
        raise AssertionError("empty DSN must fail")
    except RuntimeError:
        pass


if __name__ == "__main__":
    import asyncio

    asyncio.run(_run())
    print("\n=== Phase 24 memory tests passed ===")
