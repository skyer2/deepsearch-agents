"""Phase 12/15 兼容测试 — 更新为 MemoryStore 新 API。"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.context_builder import ContextBuilder
from app.agent.memory.backend.json_backend import JsonMemoryBackend
from app.agent.memory.models import MemoryRecord, MemoryType
from app.agent.memory.policy import MemoryPolicy, resolve_memory_user_id
from app.agent.memory.store import MemoryStore
from app.config.loader import reload_harness_config


async def _run():
    with tempfile.TemporaryDirectory() as tmp:
        policy = MemoryPolicy(
            provider="local",
            ttl_days=90,
            min_fact_chars=10,
            max_facts_per_remember=3,
            embedding_enabled=False,
            utility_gate_enabled=False,
            min_recall_trust="untrusted",
        )
        backend = JsonMemoryBackend(Path(tmp), policy)
        store = MemoryStore(backend=backend, policy=policy)
        uid = "user_a"
        saved = await store.remember(
            ["机器人行业2025年增速约15%", "短"],
            user_id=uid,
            metadata={"task": "research robots"},
        )
        assert saved == 1
        records = await store.recall("机器人行业", uid, top_k=5)
        assert len(records) >= 1
        assert "15%" in records[0].fact

        old = MemoryRecord(
            fact="过期事实",
            created_at=(datetime.now(timezone.utc) - timedelta(days=100)).isoformat(),
        )
        all_records = backend._load("default", uid)
        all_records.append(old)
        backend._save("default", uid, all_records)
        active = store.list_records(uid)
        assert all(not r.is_expired(90) for r in active)
        assert len(active) == 1
        print("[OK] memory ttl filter")

    ctx = ContextBuilder().build_memory_context(
        ["用户偏好 PDF 交付"],
        records=[
            MemoryRecord(
                fact="用户偏好 PDF 交付",
                memory_type=MemoryType.PREFERENCE,
                created_at="2026-01-01T00:00:00+00:00",
            )
        ],
        wrap_untrusted=True,
    )
    assert "<untrusted" in ctx and "user_memory" in ctx
    print("[OK] memory untrusted wrap")

    os.environ["HARNESS_MEMORY_USER_ID"] = "enterprise_user_1"
    assert resolve_memory_user_id("session_xyz") == "enterprise_user_1"
    del os.environ["HARNESS_MEMORY_USER_ID"]
    assert resolve_memory_user_id("session_xyz") == "session_xyz"
    print("[OK] memory user id resolve")

    cfg = reload_harness_config()
    assert cfg.memory_ttl_days == 90
    assert cfg.memory_wrap_untrusted is True
    assert cfg.memory_provider == "sqlite"
    print("[OK] config phase15")


if __name__ == "__main__":
    import asyncio

    asyncio.run(_run())
    print("\n=== Phase 12/15 memory tests passed ===")
