"""
【Phase 15】JSON 记忆后端 — 开发降级；无 embedding / 审计 / 软删除完整能力。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.agent.memory.backend.base import MemoryBackend
from app.agent.memory.models import (
    MemoryRecord,
    MemoryWriteRequest,
    RecallResult,
    WriteSource,
)
from app.agent.memory.policy import MemoryPolicy
from app.agent.memory.recall.hybrid import hybrid_recall
from app.agent.memory.security import contains_pii, redact_pii


class JsonMemoryBackend(MemoryBackend):
    def __init__(self, storage_dir: Path, policy: MemoryPolicy):
        self.storage_dir = storage_dir
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.policy = policy

    def _path(self, tenant_id: str, user_id: str) -> Path:
        safe_t = "".join(c if c.isalnum() or c in "-_" else "_" for c in tenant_id)
        safe_u = "".join(c if c.isalnum() or c in "-_" else "_" for c in user_id)
        return self.storage_dir / f"{safe_t}__{safe_u}.json"

    def _load(self, tenant_id: str, user_id: str) -> list[MemoryRecord]:
        path = self._path(tenant_id, user_id)
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        records: list[MemoryRecord] = []
        for item in raw:
            if isinstance(item, str):
                records.append(MemoryRecord(fact=item, tenant_id=tenant_id, user_id=user_id))
            elif isinstance(item, dict):
                rec = MemoryRecord.from_dict(item)
                rec.tenant_id = tenant_id
                rec.user_id = user_id
                records.append(rec)
        return records

    def _save(self, tenant_id: str, user_id: str, records: list[MemoryRecord]) -> None:
        path = self._path(tenant_id, user_id)
        payload = [r.to_dict() for r in records if not r.is_deleted]
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    async def recall(
        self,
        query: str,
        *,
        tenant_id: str,
        user_id: str,
        top_k: int,
    ) -> RecallResult:
        records = self._load(tenant_id, user_id)
        return await hybrid_recall(query, records, policy=self.policy, top_k=top_k)

    async def remember_writes(
        self,
        writes: list[MemoryWriteRequest],
        *,
        tenant_id: str,
        user_id: str,
    ) -> int:
        records = self._load(tenant_id, user_id)
        existing_facts = {r.fact for r in records if not r.is_deleted}
        saved = 0
        now = datetime.now(timezone.utc).isoformat()
        for write in writes[: self.policy.max_facts_per_remember]:
            fact = write.fact.strip()
            if len(fact) < self.policy.min_fact_chars:
                continue
            if self.policy.pii_redact_enabled and contains_pii(fact):
                fact = redact_pii(fact)
            if fact in existing_facts:
                continue
            records.append(
                MemoryRecord(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    fact=fact,
                    memory_type=write.memory_type,
                    confidence=write.confidence,
                    write_source=write.write_source,
                    task=write.task,
                    topic=write.topic,
                    session_id=write.session_id,
                    metadata=write.metadata,
                    created_at=now,
                    updated_at=now,
                    source="remember",
                )
            )
            existing_facts.add(fact)
            saved += 1
        self._save(tenant_id, user_id, records)
        return saved

    def list_records(
        self,
        *,
        tenant_id: str,
        user_id: str,
        include_deleted: bool = False,
    ) -> list[MemoryRecord]:
        records = self._load(tenant_id, user_id)
        if not include_deleted:
            records = [r for r in records if not r.is_deleted]
        return [r for r in records if not r.is_expired(self.policy.ttl_days)]

    async def delete_record(
        self,
        record_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> bool:
        records = self._load(tenant_id, user_id)
        changed = False
        for record in records:
            if record.id == record_id and not record.is_deleted:
                record.is_deleted = True
                record.updated_at = datetime.now(timezone.utc).isoformat()
                changed = True
                break
        if changed:
            self._save(tenant_id, user_id, records)
        return changed

    async def delete_all(
        self,
        *,
        tenant_id: str,
        user_id: str,
    ) -> int:
        records = self._load(tenant_id, user_id)
        count = 0
        now = datetime.now(timezone.utc).isoformat()
        for record in records:
            if not record.is_deleted:
                record.is_deleted = True
                record.updated_at = now
                count += 1
        if count:
            self._save(tenant_id, user_id, records)
        return count
