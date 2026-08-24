"""
【Phase 15】MemoryStore 门面 — SQLite/JSON/Mem0 后端 + Hybrid Recall + 治理。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional, Union

from app.agent.memory.backend.base import MemoryBackend
from app.agent.memory.backend.json_backend import JsonMemoryBackend
from app.agent.memory.backend.sqlite_backend import SqliteMemoryBackend
from app.agent.memory.models import (
    MemoryRecord,
    MemoryType,
    MemoryWriteRequest,
    RecallResult,
    WriteSource,
)
from app.agent.memory.policy import MemoryPolicy, get_memory_policy, resolve_memory_tenant_id
from app.config.loader import get_harness_config


class MemoryStore:
    def __init__(
        self,
        storage_dir: Optional[Path] = None,
        policy: Optional[MemoryPolicy] = None,
        backend: Optional[MemoryBackend] = None,
    ):
        base = storage_dir or Path(__file__).resolve().parents[2] / "memory_data"
        self.storage_dir = base
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.policy = policy or get_memory_policy()
        self._mem0 = None
        self._backend = backend or self._build_backend()
        self._init_mem0_if_needed()

    def _build_backend(self) -> MemoryBackend:
        provider = self.policy.provider.lower()
        if provider == "local":
            return JsonMemoryBackend(self.storage_dir, self.policy)
        if provider == "sqlite":
            db_path = self.storage_dir / "memory.db"
            return SqliteMemoryBackend(db_path, self.policy)
        if provider == "mem0":
            return JsonMemoryBackend(self.storage_dir, self.policy)
        return SqliteMemoryBackend(self.storage_dir / "memory.db", self.policy)

    def _init_mem0_if_needed(self) -> None:
        cfg = get_harness_config()
        mem0_on = (
            os.getenv("MEM0_ENABLED", "false").lower() == "true"
            or cfg.memory_provider == "mem0"
        )
        if not mem0_on:
            return
        try:
            from mem0 import Memory

            self._mem0 = Memory()
        except Exception as exc:
            print(f"[Memory] Mem0 unavailable, fallback to configured backend: {exc}")

    def _default_tenant(self, tenant_id: Optional[str]) -> str:
        return tenant_id or resolve_memory_tenant_id()

    async def recall(
        self,
        query: str,
        user_id: str,
        *,
        tenant_id: Optional[str] = None,
        top_k: Optional[int] = None,
        memory_types: Optional[list[MemoryType]] = None,
    ) -> list[MemoryRecord]:
        if not self.policy.enabled:
            return []

        tid = self._default_tenant(tenant_id)
        k = top_k or self.policy.recall_top_k

        if self._mem0 is not None:
            try:
                results = self._mem0.search(query, user_id=user_id, limit=k)
                records = []
                for item in results:
                    fact = item.get("memory", "")
                    if fact:
                        records.append(
                            MemoryRecord(
                                fact=fact,
                                tenant_id=tid,
                                user_id=user_id,
                                source="mem0",
                                write_source=WriteSource.MEM0,
                                metadata={"raw": item},
                            )
                        )
                return records[:k]
            except Exception as exc:
                print(f"[Memory] Mem0 recall failed, fallback backend: {exc}")

        result = await self._backend.recall(
            query,
            tenant_id=tid,
            user_id=user_id,
            top_k=k,
        )
        if memory_types:
            allowed = set(memory_types)
            result.records = [r for r in result.records if r.memory_type in allowed]
        self._last_recall_result = result
        return result.records

    async def recall_with_metrics(
        self,
        query: str,
        user_id: str,
        *,
        tenant_id: Optional[str] = None,
        top_k: Optional[int] = None,
    ) -> RecallResult:
        records = await self.recall(query, user_id, tenant_id=tenant_id, top_k=top_k)
        cached = getattr(self, "_last_recall_result", None)
        if cached and cached.records == records:
            return cached
        return RecallResult(
            records=records,
            recall_at_k=0.0,
            keyword_hits=0,
            embedding_used=False,
        )

    async def remember_writes(
        self,
        writes: list[MemoryWriteRequest],
        *,
        user_id: str,
        tenant_id: Optional[str] = None,
    ) -> int:
        if not self.policy.enabled or not writes:
            return 0

        tid = self._default_tenant(tenant_id)

        if self._mem0 is not None:
            try:
                saved = 0
                for write in writes[: self.policy.max_facts_per_remember]:
                    self._mem0.add(
                        write.fact,
                        user_id=user_id,
                        metadata={
                            "tenant_id": tid,
                            "memory_type": write.memory_type.value,
                            **write.metadata,
                        },
                    )
                    saved += 1
                return saved
            except Exception as exc:
                print(f"[Memory] Mem0 remember failed, fallback backend: {exc}")

        return await self._backend.remember_writes(writes, tenant_id=tid, user_id=user_id)

    async def remember(
        self,
        facts: list[Union[str, MemoryWriteRequest]],
        user_id: str,
        *,
        tenant_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        memory_type: MemoryType = MemoryType.SEMANTIC,
        write_source: WriteSource = WriteSource.FINALIZE,
    ) -> int:
        meta = metadata or {}
        writes: list[MemoryWriteRequest] = []
        for item in facts:
            if isinstance(item, MemoryWriteRequest):
                writes.append(item)
                continue
            writes.append(
                MemoryWriteRequest(
                    fact=str(item),
                    memory_type=memory_type,
                    write_source=write_source,
                    task=str(meta.get("task", "")),
                    topic=str(meta.get("topic", "")),
                    session_id=str(meta.get("session_id", "")),
                    metadata=meta,
                    confidence=float(meta.get("confidence", 0.8)),
                )
            )
        return await self.remember_writes(writes, user_id=user_id, tenant_id=tenant_id)

    def list_records(
        self,
        user_id: str,
        *,
        tenant_id: Optional[str] = None,
        include_deleted: bool = False,
    ) -> list[MemoryRecord]:
        tid = self._default_tenant(tenant_id)
        return self._backend.list_records(
            tenant_id=tid,
            user_id=user_id,
            include_deleted=include_deleted,
        )

    async def delete(self, record_id: str, user_id: str, *, tenant_id: Optional[str] = None) -> bool:
        tid = self._default_tenant(tenant_id)
        return await self._backend.delete_record(record_id, tenant_id=tid, user_id=user_id)

    async def forget_user(self, user_id: str, *, tenant_id: Optional[str] = None) -> int:
        tid = self._default_tenant(tenant_id)
        return await self._backend.delete_all(tenant_id=tid, user_id=user_id)

    def get_audit_log(self):
        backend = self._backend
        if hasattr(backend, "audit"):
            return backend.audit
        return None
