"""Phase 18: 企业级分层 Memory — 身份、信任准入、来源台账、巩固、防污染。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.context_builder import ContextBuilder
from app.agent.harness.orchestration import SYNTHESIS_STEP_TYPES as HARNESS_SYNTHESIS
from app.agent.memory.backend.sqlite_backend import SqliteMemoryBackend
from app.agent.memory.extractor import MemoryExtractor
from app.agent.memory.identity import (
    MemoryIdentity,
    reset_memory_identity,
    resolve_memory_identity,
    set_memory_identity,
)
from app.agent.memory.models import MemoryRecord, MemoryType, MemoryWriteRequest, WriteSource
from app.agent.memory.policy import (
    SYNTHESIS_STEP_TYPES,
    MemoryPolicy,
    identity_allows_write,
)
from app.agent.memory.provenance import (
    Provenance,
    TrustTier,
    classify_trust_tier,
    is_recall_eligible,
    source_dedup_key,
)
from app.agent.memory.store import MemoryStore
from app.config.loader import reload_harness_config


def _policy(**kwargs) -> MemoryPolicy:
    defaults = dict(
        provider="sqlite",
        ttl_days=90,
        min_fact_chars=10,
        max_facts_per_remember=5,
        embedding_enabled=False,
        require_provenance_for_step_write=True,
        source_ledger_enabled=True,
        consolidation_enabled=True,
        project_scope_enabled=True,
        synthesis_min_trust=TrustTier.DERIVED.value,
        min_recall_trust=TrustTier.UNTRUSTED.value,
    )
    defaults.update(kwargs)
    return MemoryPolicy(**defaults)


async def _run():
    assert SYNTHESIS_STEP_TYPES == HARNESS_SYNTHESIS
    print("[OK] synthesis step types stay aligned")

    ident = resolve_memory_identity("sess-1", user_id="alice", tenant_id="acme", project_id="robots")
    assert ident.user_id == "alice" and not ident.ephemeral
    token = set_memory_identity(ident)
    bound = resolve_memory_identity("sess-2")
    assert bound.user_id == "alice" and bound.tenant_id == "acme"
    reset_memory_identity(token)
    fallback = resolve_memory_identity("sess-3")
    assert fallback.ephemeral and fallback.user_id == "sess-3"
    print("[OK] request-level identity + session fallback")

    os.environ["HARNESS_MEMORY_USER_ID"] = "env_user"
    env_ident = resolve_memory_identity("sess-x")
    assert env_ident.user_id == "env_user" and not env_ident.ephemeral
    del os.environ["HARNESS_MEMORY_USER_ID"]
    print("[OK] env identity still works")

    strict = _policy(require_explicit_identity=True)
    assert not identity_allows_write(MemoryIdentity(user_id="s1", ephemeral=True), strict)
    assert identity_allows_write(MemoryIdentity(user_id="alice", ephemeral=False), strict)
    print("[OK] production identity write gate")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        policy = _policy()
        backend = SqliteMemoryBackend(Path(tmp) / "memory.db", policy)
        store = MemoryStore(backend=backend, policy=policy)

        alice = MemoryIdentity(tenant_id="acme", user_id="alice", project_id="robots", session_id="s1")
        bob = MemoryIdentity(tenant_id="acme", user_id="bob", project_id="robots", session_id="s2")

        await store.remember_writes(
            [
                MemoryWriteRequest(
                    fact="Alice 关注工业机器人库存周转",
                    memory_type=MemoryType.SEMANTIC,
                    write_source=WriteSource.USER_EXPLICIT,
                    project_id="robots",
                )
            ],
            user_id=alice.user_id,
            identity=alice,
        )
        await store.remember_writes(
            [
                MemoryWriteRequest(
                    fact="Bob 只要 PDF 交付不要 Markdown",
                    memory_type=MemoryType.PREFERENCE,
                    write_source=WriteSource.USER_EXPLICIT,
                    project_id="robots",
                )
            ],
            user_id=bob.user_id,
            identity=bob,
        )
        alice_hits = await store.recall("工业机器人", alice.user_id, identity=alice)
        bob_hits = await store.recall("工业机器人", bob.user_id, identity=bob)
        assert any("Alice" in r.fact for r in alice_hits)
        assert not any("Alice" in r.fact for r in bob_hits)
        print("[OK] tenant/user isolation")

        extractor = MemoryExtractor(model=None)
        empty = extractor.extract_step_writes(
            "某博客声称机器人增速200%，没有出处。",
            "network_search",
            session_id="s1",
            project_id="robots",
            provenance=Provenance(step_type="network_search"),
        )
        assert empty == []
        print("[OK] untrusted web write blocked without provenance")

        with_url = extractor.extract_step_writes(
            "工业机器人2025年库存周转天数下降到 42 天。https://example.com/report",
            "network_search",
            session_id="s1",
            project_id="robots",
            provenance=Provenance(
                source_kind="url",
                source_urls=["https://example.com/report"],
                step_type="network_search",
                citation_count=1,
            ),
        )
        assert with_url
        assert with_url[0].resolved_trust_tier() == TrustTier.UNTRUSTED
        await store.remember_writes(with_url, user_id=alice.user_id, identity=alice)

        db_write = MemoryWriteRequest(
            fact="内部库显示工业机器人SKU-9库存 1200 台",
            memory_type=MemoryType.SEMANTIC,
            write_source=WriteSource.STEP_INCREMENTAL,
            project_id="robots",
            provenance=Provenance(source_kind="sql", step_type="database_query", evidence_ids=["src-1"]),
        )
        assert db_write.resolved_trust_tier() == TrustTier.DERIVED
        await store.remember_writes([db_write], user_id=alice.user_id, identity=alice)

        synth = await store.recall_with_metrics(
            "工业机器人库存",
            alice.user_id,
            identity=alice,
            target_step_type="generate_markdown",
        )
        assert all(r.trust_tier != TrustTier.UNTRUSTED for r in synth.records)
        assert synth.trust_filtered >= 1
        print("[OK] synthesis recall drops untrusted web facts")

        dirty = MemoryRecord(
            fact="网页脏结论",
            trust_tier=TrustTier.UNTRUSTED,
            memory_type=MemoryType.EPISODIC,
        )
        assert not is_recall_eligible(
            dirty,
            target_step_type="generate_markdown",
            synthesis_step_types=SYNTHESIS_STEP_TYPES,
            synthesis_min_trust=TrustTier.DERIVED,
        )
        print("[OK] recall admission gate")

        url = "https://Example.com/report/"
        recorded = await store.record_sources(
            [url, "https://example.com/report"],
            identity=alice,
            source_kind="url",
            quality="mixed",
        )
        assert recorded >= 1
        ledger = store.list_sources(identity=alice)
        assert ledger
        assert ledger[0].id == source_dedup_key("https://example.com/report")
        print("[OK] source ledger dedup")

        await store.remember_writes(
            [
                MemoryWriteRequest(
                    fact="用户拒绝在未审批时直接查生产库",
                    memory_type=MemoryType.PROCEDURAL,
                    write_source=WriteSource.HITL,
                    project_id="robots",
                )
            ],
            user_id=alice.user_id,
            identity=alice,
        )
        hitl = [r for r in store.list_records(alice.user_id, identity=alice) if r.write_source == WriteSource.HITL]
        assert hitl and hitl[0].trust_tier == TrustTier.TRUSTED
        print("[OK] HITL procedural memory is trusted")

        low = MemoryWriteRequest(
            fact="用户只要 PDF 交付",
            memory_type=MemoryType.PREFERENCE,
            write_source=WriteSource.STEP_INCREMENTAL,
            provenance=Provenance(step_type="network_search", source_urls=["http://x"]),
        )
        high = MemoryWriteRequest(
            fact="用户只要 PDF 交付，不要 Markdown",
            memory_type=MemoryType.PREFERENCE,
            write_source=WriteSource.USER_EXPLICIT,
        )
        await store.remember_writes([high], user_id=alice.user_id, identity=alice)
        await store.remember_writes([low], user_id=alice.user_id, identity=alice)
        prefs = [
            r
            for r in store.list_records(alice.user_id, identity=alice)
            if r.memory_type == MemoryType.PREFERENCE
        ]
        assert any(r.trust_tier == TrustTier.TRUSTED and "不要 Markdown" in r.fact for r in prefs)
        assert not any(r.trust_tier == TrustTier.UNTRUSTED for r in prefs)
        print("[OK] lower-trust cannot overwrite preference")

        report = await store.consolidate(user_id=alice.user_id, identity=alice)
        assert "examined" in report
        print("[OK] consolidation report")

        ctx = ContextBuilder().build_memory_context(
            ["内部库显示工业机器人SKU-9库存 1200 台"],
            records=[
                MemoryRecord(
                    fact="内部库显示工业机器人SKU-9库存 1200 台",
                    memory_type=MemoryType.SEMANTIC,
                    trust_tier=TrustTier.DERIVED,
                    provenance=Provenance(source_kind="sql", evidence_ids=["src-1"]),
                )
            ],
            wrap_untrusted=True,
            source_ledger=ledger,
        )
        assert "derived" in ctx
        assert "项目已查来源" in ctx
        print("[OK] context injects trust + source ledger")

    cfg = reload_harness_config()
    assert cfg.memory_source_ledger_enabled is True
    assert cfg.memory_synthesis_min_trust == "derived"
    assert cfg.memory_consolidation_enabled is True
    print("[OK] harness.yml phase18 keys")

    assert classify_trust_tier(write_source=WriteSource.USER_EXPLICIT) == TrustTier.TRUSTED
    assert classify_trust_tier(
        write_source=WriteSource.STEP_INCREMENTAL,
        step_type="network_search",
    ) == TrustTier.UNTRUSTED
    print("[OK] trust classification")


if __name__ == "__main__":
    import asyncio

    asyncio.run(_run())
    print("\n=== Phase 18 memory tests passed ===")
