"""
【Phase 15】SQLite 记忆后端 — 生产默认；支持 embedding BLOB、版本、软删除、审计。
"""

from __future__ import annotations

import json
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.agent.memory.backend.base import MemoryBackend
from app.agent.memory.governance import find_merge_candidate, merge_record
from app.agent.memory.models import (
    MemoryRecord,
    MemoryType,
    MemoryWriteRequest,
    RecallResult,
    WriteSource,
)
from app.agent.memory.policy import MemoryPolicy
from app.agent.memory.recall.embedding import embed_text
from app.agent.memory.recall.hybrid import hybrid_recall
from app.agent.memory.security import MemoryAuditLog, contains_pii, redact_pii


def _pack_embedding(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack_embedding(blob: bytes) -> list[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"{count}f", blob))


class SqliteMemoryBackend(MemoryBackend):
    def __init__(self, db_path: Path, policy: MemoryPolicy):
        self.db_path = db_path
        self.policy = policy
        self.audit = MemoryAuditLog(db_path)
        self._ensure_schema()
        self._migrate_legacy_json()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    memory_type TEXT NOT NULL,
                    fact TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    confidence REAL NOT NULL DEFAULT 0.8,
                    write_source TEXT NOT NULL,
                    task TEXT,
                    topic TEXT,
                    session_id TEXT,
                    embedding BLOB,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    is_deleted INTEGER NOT NULL DEFAULT 0,
                    metadata TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memories_tenant_user
                ON memories(tenant_id, user_id, is_deleted)
                """
            )
            conn.commit()

    def _migrate_legacy_json(self) -> None:
        legacy_dir = self.db_path.parent
        if not legacy_dir.exists():
            return
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            if count > 0:
                return
        for path in legacy_dir.glob("*.json"):
            user_id = path.stem
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            writes: list[MemoryWriteRequest] = []
            for item in raw:
                if isinstance(item, str):
                    writes.append(
                        MemoryWriteRequest(
                            fact=item,
                            write_source=WriteSource.SEED,
                        )
                    )
                elif isinstance(item, dict):
                    rec = MemoryRecord.from_dict(item)
                    ws = WriteSource.SEED
                    try:
                        ws = WriteSource(rec.write_source.value if hasattr(rec.write_source, "value") else rec.write_source)
                    except ValueError:
                        ws = WriteSource.SEED
                    writes.append(
                        MemoryWriteRequest(
                            fact=rec.fact,
                            memory_type=rec.memory_type,
                            confidence=rec.confidence,
                            write_source=ws,
                            task=rec.task,
                            topic=rec.topic,
                            session_id=rec.session_id,
                            metadata=rec.metadata,
                        )
                    )
            if writes:
                self._import_legacy_writes(user_id, writes)

    def _import_legacy_writes(self, user_id: str, writes: list[MemoryWriteRequest]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            for write in writes:
                if len(write.fact.strip()) < self.policy.min_fact_chars:
                    continue
                record = MemoryRecord(
                    tenant_id="default",
                    user_id=user_id,
                    fact=write.fact.strip(),
                    memory_type=write.memory_type,
                    confidence=write.confidence,
                    write_source=write.write_source,
                    task=write.task,
                    topic=write.topic,
                    session_id=write.session_id,
                    metadata=write.metadata,
                    created_at=now,
                    updated_at=now,
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO memories (
                        id, tenant_id, user_id, memory_type, fact, version, confidence,
                        write_source, task, topic, session_id, embedding,
                        created_at, updated_at, is_deleted, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 0, ?)
                    """,
                    (
                        record.id,
                        record.tenant_id,
                        record.user_id,
                        record.memory_type.value,
                        record.fact,
                        record.version,
                        record.confidence,
                        record.write_source.value,
                        record.task,
                        record.topic,
                        record.session_id,
                        record.created_at,
                        record.updated_at,
                        json.dumps(record.metadata, ensure_ascii=False),
                    ),
                )
            conn.commit()

    def _row_to_record(self, row: sqlite3.Row) -> MemoryRecord:
        embedding = None
        if row["embedding"]:
            embedding = _unpack_embedding(row["embedding"])
        metadata = {}
        if row["metadata"]:
            try:
                metadata = json.loads(row["metadata"])
            except json.JSONDecodeError:
                metadata = {}
        return MemoryRecord(
            id=row["id"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            fact=row["fact"],
            memory_type=MemoryType(row["memory_type"]),
            version=int(row["version"]),
            confidence=float(row["confidence"]),
            write_source=WriteSource(row["write_source"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            task=row["task"] or "",
            topic=row["topic"] or "",
            session_id=row["session_id"] or "",
            is_deleted=bool(row["is_deleted"]),
            metadata=metadata,
            embedding=embedding,
            source="remember",
        )

    def _load_active(self, *, tenant_id: str, user_id: str) -> list[MemoryRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memories
                WHERE tenant_id = ? AND user_id = ? AND is_deleted = 0
                ORDER BY updated_at ASC
                """,
                (tenant_id, user_id),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    async def recall(
        self,
        query: str,
        *,
        tenant_id: str,
        user_id: str,
        top_k: int,
    ) -> RecallResult:
        records = self._load_active(tenant_id=tenant_id, user_id=user_id)
        return await hybrid_recall(
            query,
            records,
            policy=self.policy,
            top_k=top_k,
        )

    async def remember_writes(
        self,
        writes: list[MemoryWriteRequest],
        *,
        tenant_id: str,
        user_id: str,
    ) -> int:
        if not writes:
            return 0
        existing = self._load_active(tenant_id=tenant_id, user_id=user_id)
        saved = 0
        now = datetime.now(timezone.utc).isoformat()

        with self._connect() as conn:
            for write in writes[: self.policy.max_facts_per_remember]:
                fact = write.fact.strip()
                if len(fact) < self.policy.min_fact_chars:
                    continue
                if self.policy.pii_redact_enabled and contains_pii(fact):
                    fact = redact_pii(fact)
                if len(fact) < self.policy.min_fact_chars:
                    continue

                embedding = None
                if self.policy.embedding_enabled:
                    embedding = await embed_text(fact)

                candidate = find_merge_candidate(
                    write,
                    existing,
                    jaccard_threshold=self.policy.merge_jaccard_threshold,
                    embedding_threshold=self.policy.merge_embedding_threshold,
                    new_embedding=embedding,
                )
                if candidate:
                    merged = merge_record(candidate, write)
                    if embedding:
                        merged.embedding = embedding
                    conn.execute(
                        """
                        UPDATE memories SET
                            fact=?, version=?, confidence=?, updated_at=?,
                            task=?, topic=?, session_id=?, embedding=?, metadata=?
                        WHERE id=? AND tenant_id=? AND user_id=?
                        """,
                        (
                            merged.fact,
                            merged.version,
                            merged.confidence,
                            merged.updated_at,
                            merged.task,
                            merged.topic,
                            merged.session_id,
                            _pack_embedding(embedding) if embedding else None,
                            json.dumps(merged.metadata, ensure_ascii=False),
                            merged.id,
                            tenant_id,
                            user_id,
                        ),
                    )
                    self.audit.log(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        action="merge",
                        record_id=merged.id,
                        detail={"version": merged.version, "source": write.write_source.value},
                    )
                    saved += 1
                    continue

                record = MemoryRecord(
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
                    embedding=embedding,
                    created_at=now,
                    updated_at=now,
                )
                conn.execute(
                    """
                    INSERT INTO memories (
                        id, tenant_id, user_id, memory_type, fact, version, confidence,
                        write_source, task, topic, session_id, embedding,
                        created_at, updated_at, is_deleted, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        record.id,
                        tenant_id,
                        user_id,
                        record.memory_type.value,
                        record.fact,
                        record.version,
                        record.confidence,
                        record.write_source.value,
                        record.task,
                        record.topic,
                        record.session_id,
                        _pack_embedding(embedding) if embedding else None,
                        record.created_at,
                        record.updated_at,
                        json.dumps(record.metadata, ensure_ascii=False),
                    ),
                )
                existing.append(record)
                self.audit.log(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    action="remember",
                    record_id=record.id,
                    detail={
                        "memory_type": record.memory_type.value,
                        "source": write.write_source.value,
                    },
                )
                saved += 1
            conn.commit()
        return saved

    def list_records(
        self,
        *,
        tenant_id: str,
        user_id: str,
        include_deleted: bool = False,
    ) -> list[MemoryRecord]:
        with self._connect() as conn:
            if include_deleted:
                rows = conn.execute(
                    """
                    SELECT * FROM memories
                    WHERE tenant_id = ? AND user_id = ?
                    ORDER BY updated_at DESC
                    """,
                    (tenant_id, user_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM memories
                    WHERE tenant_id = ? AND user_id = ? AND is_deleted = 0
                    ORDER BY updated_at DESC
                    """,
                    (tenant_id, user_id),
                ).fetchall()
        records = [self._row_to_record(r) for r in rows]
        return [r for r in records if not r.is_expired(self.policy.ttl_days)]

    async def delete_record(
        self,
        record_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE memories SET is_deleted = 1, updated_at = ?
                WHERE id = ? AND tenant_id = ? AND user_id = ? AND is_deleted = 0
                """,
                (datetime.now(timezone.utc).isoformat(), record_id, tenant_id, user_id),
            )
            conn.commit()
            if cur.rowcount > 0:
                self.audit.log(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    action="delete",
                    record_id=record_id,
                )
                return True
        return False

    async def delete_all(
        self,
        *,
        tenant_id: str,
        user_id: str,
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE memories SET is_deleted = 1, updated_at = ?
                WHERE tenant_id = ? AND user_id = ? AND is_deleted = 0
                """,
                (datetime.now(timezone.utc).isoformat(), tenant_id, user_id),
            )
            conn.commit()
            count = cur.rowcount
        if count:
            self.audit.log(
                tenant_id=tenant_id,
                user_id=user_id,
                action="delete_all",
                detail={"count": count},
            )
        return count
