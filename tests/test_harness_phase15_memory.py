"""Phase 15: 生产级 Memory — SQLite + Hybrid + 治理 + API（无需 LLM/Embedding）。"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.context_builder import ContextBuilder
from app.agent.memory.backend.json_backend import JsonMemoryBackend
from app.agent.memory.backend.sqlite_backend import SqliteMemoryBackend
from app.agent.memory.models import MemoryRecord, MemoryType, MemoryWriteRequest, WriteSource
from app.agent.memory.policy import MemoryPolicy, resolve_memory_tenant_id, resolve_memory_user_id
from app.agent.memory.security import contains_pii, redact_pii
from app.agent.memory.store import MemoryStore
from app.agent.memory.governance import token_jaccard


async def _run():
    policy = MemoryPolicy(
        provider="sqlite",
        ttl_days=90,
        min_fact_chars=10,
        max_facts_per_remember=5,
        embedding_enabled=False,
        utility_gate_enabled=False,
        min_recall_trust="untrusted",
    )

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        sqlite_backend = SqliteMemoryBackend(tmp_path / "memory.db", policy)
        store = MemoryStore(backend=sqlite_backend, policy=policy)
        uid = "user_a"
        tid = "tenant_test"

        saved = await store.remember(
            ["机器人行业2025年增速约15%", "短"],
            user_id=uid,
            tenant_id=tid,
            metadata={"task": "research robots"},
        )
        assert saved == 1

        result = await store.recall_with_metrics("机器人行业", uid, tenant_id=tid, top_k=5)
        assert len(result.records) >= 1
        assert "15%" in result.records[0].fact

        # 数字变化视为矛盾 → SUPERSEDE（旧记录软删，新记录带取代链）
        superseded = await store.remember_writes(
            [
                MemoryWriteRequest(
                    fact="机器人行业2025年增速约15.5%",
                    memory_type=MemoryType.SEMANTIC,
                    write_source=WriteSource.FINALIZE,
                )
            ],
            user_id=uid,
            tenant_id=tid,
        )
        assert superseded == 1
        records = store.list_records(uid, tenant_id=tid)
        assert len(records) == 1
        assert "15.5%" in records[0].fact
        assert records[0].supersedes  # 指向被取代的旧 id

        # 同主题非矛盾刷新 → UPDATE，version 递增
        refreshed = await store.remember_writes(
            [
                MemoryWriteRequest(
                    fact="机器人行业2025年增速约15.5%，主要来自工业场景",
                    memory_type=MemoryType.SEMANTIC,
                    write_source=WriteSource.FINALIZE,
                )
            ],
            user_id=uid,
            tenant_id=tid,
        )
        assert refreshed == 1
        records = store.list_records(uid, tenant_id=tid)
        assert len(records) == 1
        assert records[0].version >= 2

        rid = records[0].id
        assert await store.delete(rid, uid, tenant_id=tid)
        assert store.list_records(uid, tenant_id=tid) == []
        print("[OK] sqlite merge + delete")

    assert token_jaccard("机器人行业增速", "机器人行业2025年增速") > 0.3
    assert contains_pii("联系 test@example.com")
    assert "[REDACTED_EMAIL]" in redact_pii("联系 test@example.com")
    print("[OK] governance + pii")

    ctx = ContextBuilder().build_memory_context(
        ["用户偏好 PDF 交付"],
        records=[
            MemoryRecord(
                fact="用户偏好 PDF 交付",
                memory_type=MemoryType.PREFERENCE,
                version=2,
                created_at="2026-01-01T00:00:00+00:00",
                recall_score=0.82,
            )
        ],
        wrap_untrusted=True,
    )
    assert "<untrusted" in ctx and "preference" in ctx
    print("[OK] context typed memory")

    os.environ["HARNESS_MEMORY_TENANT_ID"] = "tenant_acme"
    assert resolve_memory_tenant_id() == "tenant_acme"
    del os.environ["HARNESS_MEMORY_TENANT_ID"]
    print("[OK] tenant resolve")

    json_policy = MemoryPolicy(
        provider="local",
        embedding_enabled=False,
        min_fact_chars=10,
        utility_gate_enabled=False,
        min_recall_trust="untrusted",
    )
    with tempfile.TemporaryDirectory() as tmp2:
        jb = JsonMemoryBackend(Path(tmp2), json_policy)
        jstore = MemoryStore(backend=jb, policy=json_policy)
        await jstore.remember(["JSON backend fact test"], user_id="u1")
        recalled = await jstore.recall("JSON backend", "u1")
        assert recalled
        print("[OK] json fallback backend")


if __name__ == "__main__":
    import asyncio

    asyncio.run(_run())
    print("\n=== Phase 15 memory tests passed ===")
