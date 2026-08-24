"""
【Phase 15】MemoryStore 门面 — SQLite/JSON/Mem0 后端 + Hybrid Recall + 治理。
【Phase 18】请求级身份、写入门、类型/项目下推、来源台账、巩固入口、召回指标修复。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional, Union

from app.agent.memory.backend.base import MemoryBackend
from app.agent.memory.backend.json_backend import JsonMemoryBackend
from app.agent.memory.backend.sqlite_backend import SqliteMemoryBackend
from app.agent.memory.identity import MemoryIdentity, resolve_memory_identity
from app.agent.memory.models import (
    MemoryRecord,
    MemoryType,
    MemoryWriteRequest,
    RecallResult,
    SourceLedgerEntry,
    WriteSource,
)
from app.agent.memory.policy import (
    MemoryPolicy,
    get_memory_policy,
    identity_allows_write,
)
from app.agent.memory.provenance import (
    source_dedup_key,
)
from app.config.loader import get_harness_config

_SHARED_STORE: Optional["MemoryStore"] = None


def get_memory_store() -> "MemoryStore":
    """进程级单例，避免 API 每次 new MemoryStore() 重复开库。"""
    global _SHARED_STORE
    if _SHARED_STORE is None:
        _SHARED_STORE = MemoryStore()
    return _SHARED_STORE


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
        self._last_recall_result: Optional[RecallResult] = None
        self._init_mem0_if_needed()

    def _build_backend(self) -> MemoryBackend:
        provider = self.policy.provider.lower()
        if provider == "local":
            return JsonMemoryBackend(self.storage_dir, self.policy)
        if provider == "sqlite":
            return SqliteMemoryBackend(self.storage_dir / "memory.db", self.policy)
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

    def _identity(
        self,
        *,
        user_id: str,
        tenant_id: Optional[str],
        project_id: Optional[str] = None,
        session_id: str = "",
        identity: Optional[MemoryIdentity] = None,
    ) -> MemoryIdentity:
        if identity is not None:
            return identity
        return resolve_memory_identity(
            session_id or user_id,
            user_id=user_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )

    async def recall(
        self,
        query: str,
        user_id: str,
        *,
        tenant_id: Optional[str] = None,
        top_k: Optional[int] = None,
        memory_types: Optional[list[MemoryType]] = None,
        project_id: Optional[str] = None,
        target_step_type: str = "",
        identity: Optional[MemoryIdentity] = None,
        session_id: str = "",
    ) -> list[MemoryRecord]:
        result = await self.recall_with_metrics(
            query,
            user_id,
            tenant_id=tenant_id,
            top_k=top_k,
            memory_types=memory_types,
            project_id=project_id,
            target_step_type=target_step_type,
            identity=identity,
            session_id=session_id,
        )
        return result.records

    async def recall_with_metrics(
        self,
        query: str,
        user_id: str,
        *,
        tenant_id: Optional[str] = None,
        top_k: Optional[int] = None,
        memory_types: Optional[list[MemoryType]] = None,
        project_id: Optional[str] = None,
        target_step_type: str = "",
        identity: Optional[MemoryIdentity] = None,
        session_id: str = "",
    ) -> RecallResult:
        empty = RecallResult(records=[], recall_at_k=0.0, keyword_hits=0, embedding_used=False)
        if not self.policy.enabled:
            self._last_recall_result = empty
            return empty

        ident = self._identity(
            user_id=user_id,
            tenant_id=tenant_id,
            project_id=project_id,
            session_id=session_id,
            identity=identity,
        )
        k = top_k or self.policy.recall_top_k

        if self._mem0 is not None:
            try:
                results = self._mem0.search(query, user_id=ident.user_id, limit=k)
                records = []
                for item in results:
                    fact = item.get("memory", "")
                    if fact:
                        records.append(
                            MemoryRecord(
                                fact=fact,
                                tenant_id=ident.tenant_id,
                                user_id=ident.user_id,
                                project_id=ident.project_id,
                                source="mem0",
                                write_source=WriteSource.MEM0,
                                metadata={"raw": item},
                            )
                        )
                result = RecallResult(
                    records=records[:k],
                    recall_at_k=1.0 if records else 0.0,
                    keyword_hits=len(records),
                    embedding_used=True,
                )
                self._last_recall_result = result
                return result
            except Exception as exc:
                print(f"[Memory] Mem0 recall failed, fallback backend: {exc}")

        result = await self._backend.recall(
            query,
            tenant_id=ident.tenant_id,
            user_id=ident.user_id,
            top_k=k,
            project_id=ident.project_id,
            memory_types=memory_types,
            target_step_type=target_step_type,
        )
        self._last_recall_result = result
        if result.records:
            await self._backend.mark_recalled(
                [r.id for r in result.records],
                tenant_id=ident.tenant_id,
                user_id=ident.user_id,
                session_id=ident.session_id,
            )
        return result

    async def remember_writes(
        self,
        writes: list[MemoryWriteRequest],
        *,
        user_id: str,
        tenant_id: Optional[str] = None,
        project_id: Optional[str] = None,
        identity: Optional[MemoryIdentity] = None,
        session_id: str = "",
    ) -> int:
        if not self.policy.enabled or not writes:
            return 0

        ident = self._identity(
            user_id=user_id,
            tenant_id=tenant_id,
            project_id=project_id,
            session_id=session_id,
            identity=identity,
        )
        if not identity_allows_write(ident, self.policy):
            print(
                f"[Memory] skip write: identity not allowed "
                f"(ephemeral={ident.ephemeral}, user={ident.user_id})"
            )
            return 0

        for write in writes:
            if not write.project_id:
                write.project_id = ident.project_id
            if not write.session_id:
                write.session_id = ident.session_id

        if self._mem0 is not None:
            try:
                saved = 0
                for write in writes[: self.policy.max_facts_per_remember]:
                    self._mem0.add(
                        write.fact,
                        user_id=ident.user_id,
                        metadata={
                            "tenant_id": ident.tenant_id,
                            "project_id": ident.project_id,
                            "memory_type": write.memory_type.value,
                            **write.metadata,
                        },
                    )
                    saved += 1
                return saved
            except Exception as exc:
                print(f"[Memory] Mem0 remember failed, fallback backend: {exc}")

        return await self._backend.remember_writes(
            writes,
            tenant_id=ident.tenant_id,
            user_id=ident.user_id,
            project_id=ident.project_id,
        )

    async def remember(
        self,
        facts: list[Union[str, MemoryWriteRequest]],
        user_id: str,
        *,
        tenant_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        memory_type: MemoryType = MemoryType.SEMANTIC,
        write_source: WriteSource = WriteSource.FINALIZE,
        project_id: Optional[str] = None,
        identity: Optional[MemoryIdentity] = None,
        session_id: str = "",
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
                    session_id=str(meta.get("session_id", session_id)),
                    metadata=meta,
                    confidence=float(meta.get("confidence", 0.8)),
                    project_id=str(meta.get("project_id", project_id or "")),
                )
            )
        return await self.remember_writes(
            writes,
            user_id=user_id,
            tenant_id=tenant_id,
            project_id=project_id,
            identity=identity,
            session_id=session_id,
        )

    def list_records(
        self,
        user_id: str,
        *,
        tenant_id: Optional[str] = None,
        include_deleted: bool = False,
        project_id: Optional[str] = None,
        identity: Optional[MemoryIdentity] = None,
    ) -> list[MemoryRecord]:
        ident = self._identity(
            user_id=user_id,
            tenant_id=tenant_id,
            project_id=project_id,
            identity=identity,
        )
        return self._backend.list_records(
            tenant_id=ident.tenant_id,
            user_id=ident.user_id,
            include_deleted=include_deleted,
            project_id=ident.project_id,
        )

    async def delete(self, record_id: str, user_id: str, *, tenant_id: Optional[str] = None) -> bool:
        ident = self._identity(user_id=user_id, tenant_id=tenant_id)
        return await self._backend.delete_record(
            record_id, tenant_id=ident.tenant_id, user_id=ident.user_id
        )

    async def forget_user(self, user_id: str, *, tenant_id: Optional[str] = None) -> int:
        ident = self._identity(user_id=user_id, tenant_id=tenant_id)
        return await self._backend.delete_all(tenant_id=ident.tenant_id, user_id=ident.user_id)

    async def record_sources(
        self,
        locators: list[str],
        *,
        identity: MemoryIdentity,
        source_kind: str = "url",
        quality: str = "unknown",
        session_id: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> int:
        if not self.policy.source_ledger_enabled or not locators:
            return 0
        entries: list[SourceLedgerEntry] = []
        seen: set[str] = set()
        for locator in locators:
            key = source_dedup_key(locator, kind=source_kind)
            if not key or key in seen:
                continue
            seen.add(key)
            entries.append(
                SourceLedgerEntry(
                    id=key,
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    project_id=identity.project_id,
                    source_kind=source_kind,
                    locator=locator,
                    quality=quality,
                    session_id=session_id or identity.session_id,
                    metadata=metadata or {},
                )
            )
        return await self._backend.upsert_source_ledger(entries)

    def list_sources(
        self,
        *,
        identity: MemoryIdentity,
        limit: Optional[int] = None,
    ) -> list[SourceLedgerEntry]:
        if not self.policy.source_ledger_enabled:
            return []
        cap = limit or self.policy.source_ledger_max_inject
        return self._backend.list_source_ledger(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            project_id=identity.project_id,
            limit=cap,
        )

    async def consolidate(
        self,
        *,
        user_id: str,
        tenant_id: Optional[str] = None,
        project_id: Optional[str] = None,
        identity: Optional[MemoryIdentity] = None,
    ) -> dict[str, int]:
        if not self.policy.consolidation_enabled:
            return {"decayed": 0, "promoted": 0, "purged": 0, "examined": 0}
        ident = self._identity(
            user_id=user_id,
            tenant_id=tenant_id,
            project_id=project_id,
            identity=identity,
        )
        return await self._backend.consolidate(
            tenant_id=ident.tenant_id,
            user_id=ident.user_id,
            project_id=ident.project_id,
        )

    def get_audit_log(self):
        backend = self._backend
        if hasattr(backend, "audit"):
            return backend.audit
        return None
