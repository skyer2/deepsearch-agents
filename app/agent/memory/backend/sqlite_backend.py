"""
【Phase 15】SQLite 记忆后端 — 生产默认；支持 embedding BLOB、版本、软删除、审计。
【Phase 18】WAL、schema 迁移、项目域、信任等级、来源台账、巩固动作、异步 I/O。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import struct
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from app.agent.memory.backend.base import MemoryBackend
from app.agent.memory.consolidation import (
    ConsolidationAction,
    ConsolidationReport,
    apply_update,
    decay_confidence,
    decide_write_action,
    should_promote,
    should_purge,
)
from app.agent.memory.models import (
    MemoryRecord,
    MemoryType,
    MemoryWriteRequest,
    RecallResult,
    SourceLedgerEntry,
    WriteSource,
)
from app.agent.memory.policy import MemoryPolicy
from app.agent.memory.provenance import Provenance, TrustTier, coerce_trust_tier
from app.agent.memory.recall.embedding import embed_text
from app.agent.memory.recall.hybrid import hybrid_recall
from app.agent.memory.security import MemoryAuditLog, contains_pii, redact_pii

_NEW_COLUMNS: list[tuple[str, str]] = [
    ("project_id", "TEXT NOT NULL DEFAULT 'default'"),
    ("trust_tier", "TEXT NOT NULL DEFAULT 'derived'"),
    ("provenance", "TEXT"),
    ("dedup_key", "TEXT NOT NULL DEFAULT ''"),
    ("supersedes", "TEXT"),
    ("superseded_by", "TEXT NOT NULL DEFAULT ''"),
    ("recall_count", "INTEGER NOT NULL DEFAULT 0"),
    ("last_recalled_at", "TEXT NOT NULL DEFAULT ''"),
]


def _pack_embedding(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack_embedding(blob: bytes) -> list[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"{count}f", blob))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteMemoryBackend(MemoryBackend):
    def __init__(self, db_path: Path, policy: MemoryPolicy):
        self.db_path = db_path
        self.policy = policy
        self.audit = MemoryAuditLog(db_path)
        self._ensure_schema()
        self._migrate_legacy_json()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
        finally:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            conn.close()

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
            existing_cols = {
                row["name"] for row in conn.execute("PRAGMA table_info(memories)").fetchall()
            }
            for name, decl in _NEW_COLUMNS:
                if name not in existing_cols:
                    conn.execute(f"ALTER TABLE memories ADD COLUMN {name} {decl}")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memories_tenant_user
                ON memories(tenant_id, user_id, is_deleted)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memories_project
                ON memories(tenant_id, user_id, project_id, is_deleted)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS source_ledger (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    locator TEXT NOT NULL,
                    quality TEXT NOT NULL DEFAULT 'unknown',
                    hit_count INTEGER NOT NULL DEFAULT 1,
                    last_used_at TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    session_id TEXT,
                    metadata TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_source_ledger_scope
                ON source_ledger(tenant_id, user_id, project_id)
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
                    writes.append(MemoryWriteRequest(fact=item, write_source=WriteSource.SEED))
                elif isinstance(item, dict):
                    rec = MemoryRecord.from_dict(item)
                    writes.append(
                        MemoryWriteRequest(
                            fact=rec.fact,
                            memory_type=rec.memory_type,
                            confidence=rec.confidence,
                            write_source=WriteSource.SEED,
                            task=rec.task,
                            topic=rec.topic,
                            session_id=rec.session_id,
                            metadata=rec.metadata,
                            project_id=rec.project_id,
                            trust_tier=rec.trust_tier,
                            provenance=rec.provenance,
                        )
                    )
            if writes:
                self._import_legacy_writes(user_id, writes)

    def _import_legacy_writes(self, user_id: str, writes: list[MemoryWriteRequest]) -> None:
        now = _now()
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
                    project_id=write.project_id or "default",
                    trust_tier=write.resolved_trust_tier(),
                    provenance=write.resolved_provenance(),
                )
                self._insert_record(conn, record)
            conn.commit()

    def _row_to_record(self, row: sqlite3.Row) -> MemoryRecord:
        keys = row.keys()
        embedding = None
        if "embedding" in keys and row["embedding"]:
            embedding = _unpack_embedding(row["embedding"])
        metadata: dict[str, Any] = {}
        if row["metadata"]:
            try:
                metadata = json.loads(row["metadata"])
            except json.JSONDecodeError:
                metadata = {}
        provenance = Provenance.from_dict(
            json.loads(row["provenance"]) if "provenance" in keys and row["provenance"] else None
        )
        supersedes: list[str] = []
        if "supersedes" in keys and row["supersedes"]:
            try:
                supersedes = json.loads(row["supersedes"])
            except json.JSONDecodeError:
                supersedes = []
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
            project_id=row["project_id"] if "project_id" in keys and row["project_id"] else "default",
            trust_tier=coerce_trust_tier(row["trust_tier"] if "trust_tier" in keys else None),
            provenance=provenance,
            dedup_key=row["dedup_key"] if "dedup_key" in keys and row["dedup_key"] else "",
            supersedes=supersedes,
            superseded_by=row["superseded_by"] if "superseded_by" in keys and row["superseded_by"] else "",
            recall_count=int(row["recall_count"] or 0) if "recall_count" in keys else 0,
            last_recalled_at=row["last_recalled_at"] if "last_recalled_at" in keys and row["last_recalled_at"] else "",
        )

    def _insert_record(self, conn: sqlite3.Connection, record: MemoryRecord) -> None:
        conn.execute(
            """
            INSERT INTO memories (
                id, tenant_id, user_id, memory_type, fact, version, confidence,
                write_source, task, topic, session_id, embedding,
                created_at, updated_at, is_deleted, metadata,
                project_id, trust_tier, provenance, dedup_key, supersedes,
                superseded_by, recall_count, last_recalled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                _pack_embedding(record.embedding) if record.embedding else None,
                record.created_at,
                record.updated_at,
                json.dumps(record.metadata, ensure_ascii=False),
                record.project_id or "default",
                record.trust_label(),
                json.dumps(record.provenance.to_dict(), ensure_ascii=False),
                record.dedup_key,
                json.dumps(record.supersedes, ensure_ascii=False),
                record.superseded_by,
                record.recall_count,
                record.last_recalled_at,
            ),
        )

    def _update_record(self, conn: sqlite3.Connection, record: MemoryRecord) -> None:
        conn.execute(
            """
            UPDATE memories SET
                fact=?, version=?, confidence=?, updated_at=?,
                task=?, topic=?, session_id=?, embedding=?, metadata=?,
                project_id=?, trust_tier=?, provenance=?, dedup_key=?,
                supersedes=?, superseded_by=?, is_deleted=?
            WHERE id=? AND tenant_id=? AND user_id=?
            """,
            (
                record.fact,
                record.version,
                record.confidence,
                record.updated_at,
                record.task,
                record.topic,
                record.session_id,
                _pack_embedding(record.embedding) if record.embedding else None,
                json.dumps(record.metadata, ensure_ascii=False),
                record.project_id or "default",
                record.trust_label(),
                json.dumps(record.provenance.to_dict(), ensure_ascii=False),
                record.dedup_key,
                json.dumps(record.supersedes, ensure_ascii=False),
                record.superseded_by,
                1 if record.is_deleted else 0,
                record.id,
                record.tenant_id,
                record.user_id,
            ),
        )

    def _load_active(self, *, tenant_id: str, user_id: str, project_id: str = "") -> list[MemoryRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memories
                WHERE tenant_id = ? AND user_id = ? AND is_deleted = 0
                ORDER BY updated_at ASC
                """,
                (tenant_id, user_id),
            ).fetchall()
        records = [self._row_to_record(r) for r in rows]
        # 项目域是加权项，不是硬隔离：同用户其他项目的结论仍可召回，只是分数更低
        return records

    async def recall(
        self,
        query: str,
        *,
        tenant_id: str,
        user_id: str,
        top_k: int,
        project_id: str = "",
        memory_types: Optional[list[MemoryType]] = None,
        target_step_type: str = "",
    ) -> RecallResult:
        records = await asyncio.to_thread(
            self._load_active, tenant_id=tenant_id, user_id=user_id, project_id=project_id
        )
        return await hybrid_recall(
            query,
            records,
            policy=self.policy,
            top_k=top_k,
            memory_types=memory_types,
            project_id=project_id,
            target_step_type=target_step_type,
        )

    async def remember_writes(
        self,
        writes: list[MemoryWriteRequest],
        *,
        tenant_id: str,
        user_id: str,
        project_id: str = "",
    ) -> int:
        if not writes:
            return 0
        existing = await asyncio.to_thread(
            self._load_active, tenant_id=tenant_id, user_id=user_id, project_id=project_id
        )
        prepared: list[tuple[MemoryWriteRequest, str, Optional[list[float]]]] = []
        for write in writes[: self.policy.max_facts_per_remember]:
            if project_id and not write.project_id:
                write.project_id = project_id
            fact = write.fact.strip()
            if len(fact) < self.policy.min_fact_chars:
                continue
            if self.policy.pii_redact_enabled and contains_pii(fact):
                fact = redact_pii(fact)
                write.fact = fact
            if len(fact) < self.policy.min_fact_chars:
                continue
            embedding = None
            if self.policy.embedding_enabled:
                embedding = await embed_text(fact)
            prepared.append((write, fact, embedding))

        return await asyncio.to_thread(
            self._commit_writes,
            prepared,
            existing,
            tenant_id,
            user_id,
        )

    def _commit_writes(
        self,
        prepared: list[tuple[MemoryWriteRequest, str, Optional[list[float]]]],
        existing: list[MemoryRecord],
        tenant_id: str,
        user_id: str,
    ) -> int:
        saved = 0
        now = _now()
        with self._connect() as conn:
            for write, fact, embedding in prepared:
                decision = decide_write_action(
                    write, existing, policy=self.policy, new_embedding=embedding
                )
                if decision.action == ConsolidationAction.NOOP:
                    self.audit.log(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        action="noop",
                        record_id=decision.target.id if decision.target else None,
                        detail={"reason": decision.reason},
                        conn=conn,
                    )
                    continue

                if decision.action == ConsolidationAction.UPDATE and decision.target:
                    merged = apply_update(decision.target, write)
                    if embedding:
                        merged.embedding = embedding
                    self._update_record(conn, merged)
                    self.audit.log(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        action="merge",
                        record_id=merged.id,
                        detail={"version": merged.version, "source": write.write_source.value},
                        conn=conn,
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
                    project_id=write.project_id or "default",
                    trust_tier=write.resolved_trust_tier(),
                    provenance=write.resolved_provenance(),
                    dedup_key=write.dedup_key,
                )

                if decision.action == ConsolidationAction.SUPERSEDE and decision.target:
                    old = decision.target
                    old.is_deleted = True
                    old.superseded_by = record.id
                    old.updated_at = now
                    self._update_record(conn, old)
                    record.supersedes = [old.id]
                    record.metadata = {**record.metadata, "supersede_reason": decision.reason}
                    self.audit.log(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        action="supersede",
                        record_id=record.id,
                        detail={"old_id": old.id, "reason": decision.reason},
                        conn=conn,
                    )

                self._insert_record(conn, record)
                existing.append(record)
                if decision.action != ConsolidationAction.SUPERSEDE:
                    self.audit.log(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        action="remember",
                        record_id=record.id,
                        detail={
                            "memory_type": record.memory_type.value,
                            "source": write.write_source.value,
                            "trust_tier": record.trust_label(),
                        },
                        conn=conn,
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
        project_id: str = "",
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
        if project_id and project_id != "default":
            preferred = [r for r in records if r.project_id == project_id]
            others = [r for r in records if r.project_id != project_id]
            records = preferred + others
        return [r for r in records if include_deleted or not r.is_expired(self.policy.ttl_days)]

    async def delete_record(
        self,
        record_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> bool:
        def _delete() -> bool:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE memories SET is_deleted = 1, updated_at = ?
                    WHERE id = ? AND tenant_id = ? AND user_id = ? AND is_deleted = 0
                    """,
                    (_now(), record_id, tenant_id, user_id),
                )
                conn.commit()
                if cur.rowcount > 0:
                    self.audit.log(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        action="delete",
                        record_id=record_id,
                        conn=conn,
                    )
                    return True
            return False

        return await asyncio.to_thread(_delete)

    async def delete_all(
        self,
        *,
        tenant_id: str,
        user_id: str,
    ) -> int:
        def _delete_all() -> int:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE memories SET is_deleted = 1, updated_at = ?
                    WHERE tenant_id = ? AND user_id = ? AND is_deleted = 0
                    """,
                    (_now(), tenant_id, user_id),
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

        return await asyncio.to_thread(_delete_all)

    async def mark_recalled(
        self,
        record_ids: list[str],
        *,
        tenant_id: str,
        user_id: str,
        session_id: str = "",
    ) -> None:
        if not record_ids:
            return

        def _mark() -> None:
            now = _now()
            with self._connect() as conn:
                for rid in record_ids:
                    row = conn.execute(
                        "SELECT metadata FROM memories WHERE id=? AND tenant_id=? AND user_id=?",
                        (rid, tenant_id, user_id),
                    ).fetchone()
                    metadata: dict[str, Any] = {}
                    if row and row["metadata"]:
                        try:
                            metadata = json.loads(row["metadata"])
                        except json.JSONDecodeError:
                            metadata = {}
                    sessions = list(metadata.get("seen_sessions") or [])
                    if session_id and session_id not in sessions:
                        sessions.append(session_id)
                        metadata["seen_sessions"] = sessions[-20:]
                    conn.execute(
                        """
                        UPDATE memories
                        SET recall_count = recall_count + 1,
                            last_recalled_at = ?,
                            metadata = ?
                        WHERE id = ? AND tenant_id = ? AND user_id = ?
                        """,
                        (now, json.dumps(metadata, ensure_ascii=False), rid, tenant_id, user_id),
                    )
                conn.commit()

        await asyncio.to_thread(_mark)

    async def upsert_source_ledger(self, entries: list[SourceLedgerEntry]) -> int:
        if not entries:
            return 0

        def _upsert() -> int:
            saved = 0
            now = _now()
            with self._connect() as conn:
                for entry in entries:
                    if not entry.id or not entry.locator:
                        continue
                    row = conn.execute(
                        "SELECT hit_count, first_seen_at FROM source_ledger WHERE id=?",
                        (entry.id,),
                    ).fetchone()
                    if row:
                        conn.execute(
                            """
                            UPDATE source_ledger SET
                                hit_count = hit_count + 1,
                                last_used_at = ?,
                                quality = CASE WHEN ? != 'unknown' THEN ? ELSE quality END,
                                session_id = ?,
                                metadata = ?
                            WHERE id = ?
                            """,
                            (
                                now,
                                entry.quality,
                                entry.quality,
                                entry.session_id,
                                json.dumps(entry.metadata, ensure_ascii=False),
                                entry.id,
                            ),
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO source_ledger (
                                id, tenant_id, user_id, project_id, source_kind, locator,
                                quality, hit_count, last_used_at, first_seen_at,
                                session_id, metadata
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                            """,
                            (
                                entry.id,
                                entry.tenant_id,
                                entry.user_id,
                                entry.project_id,
                                entry.source_kind,
                                entry.locator,
                                entry.quality,
                                now,
                                now,
                                entry.session_id,
                                json.dumps(entry.metadata, ensure_ascii=False),
                            ),
                        )
                    saved += 1
                conn.commit()
            return saved

        return await asyncio.to_thread(_upsert)

    def list_source_ledger(
        self,
        *,
        tenant_id: str,
        user_id: str,
        project_id: str,
        limit: int = 8,
    ) -> list[SourceLedgerEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM source_ledger
                WHERE tenant_id = ? AND user_id = ? AND project_id = ?
                ORDER BY last_used_at DESC
                LIMIT ?
                """,
                (tenant_id, user_id, project_id, limit),
            ).fetchall()
        entries: list[SourceLedgerEntry] = []
        for row in rows:
            metadata: dict[str, Any] = {}
            if row["metadata"]:
                try:
                    metadata = json.loads(row["metadata"])
                except json.JSONDecodeError:
                    metadata = {}
            entries.append(
                SourceLedgerEntry(
                    id=row["id"],
                    tenant_id=row["tenant_id"],
                    user_id=row["user_id"],
                    project_id=row["project_id"],
                    source_kind=row["source_kind"],
                    locator=row["locator"],
                    quality=row["quality"],
                    hit_count=int(row["hit_count"] or 1),
                    last_used_at=row["last_used_at"] or "",
                    first_seen_at=row["first_seen_at"] or "",
                    session_id=row["session_id"] or "",
                    metadata=metadata,
                )
            )
        return entries

    async def consolidate(
        self,
        *,
        tenant_id: str,
        user_id: str,
        project_id: str = "",
    ) -> dict[str, int]:
        def _run() -> dict[str, int]:
            report = ConsolidationReport()
            half_life = getattr(self.policy, "consolidation_half_life_days", 30)
            floor = getattr(self.policy, "consolidation_min_confidence", 0.25)
            min_sessions = getattr(self.policy, "consolidation_promote_min_sessions", 2)
            purge_days = getattr(self.policy, "purge_after_days", 180)
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM memories
                    WHERE tenant_id = ? AND user_id = ?
                    """,
                    (tenant_id, user_id),
                ).fetchall()
                for row in rows:
                    record = self._row_to_record(row)
                    report.examined += 1
                    if should_purge(record, purge_after_days=purge_days):
                        conn.execute(
                            "DELETE FROM memories WHERE id=? AND tenant_id=? AND user_id=?",
                            (record.id, tenant_id, user_id),
                        )
                        report.purged += 1
                        continue
                    if record.is_deleted:
                        continue
                    new_conf = decay_confidence(record, half_life_days=half_life, floor=floor)
                    changed = False
                    if abs(new_conf - record.confidence) > 0.01:
                        record.confidence = new_conf
                        changed = True
                        report.decayed += 1
                    if should_promote(record, min_sessions=min_sessions):
                        record.trust_tier = TrustTier.TRUSTED
                        record.metadata = {**record.metadata, "promoted": True}
                        changed = True
                        report.promoted += 1
                    if changed:
                        record.updated_at = _now()
                        self._update_record(conn, record)
                conn.commit()
            self.audit.log(
                tenant_id=tenant_id,
                user_id=user_id,
                action="consolidate",
                detail=report.to_dict(),
            )
            return report.to_dict()

        return await asyncio.to_thread(_run)
