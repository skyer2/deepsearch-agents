"""
【Phase 15】记忆安全 — PII 过滤 + 审计日志。
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")),
    ("phone_cn", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("id_card_cn", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
]


def contains_pii(text: str) -> bool:
    for _, pattern in _PII_PATTERNS:
        if pattern.search(text):
            return True
    return False


def redact_pii(text: str) -> str:
    redacted = text
    for label, pattern in _PII_PATTERNS:
        redacted = pattern.sub(f"[REDACTED_{label.upper()}]", redacted)
    return redacted


class MemoryAuditLog:
    """SQLite 审计表（与 memory backend 共用库文件）。"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    record_id TEXT,
                    action TEXT NOT NULL,
                    detail TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def log(
        self,
        *,
        tenant_id: str,
        user_id: str,
        action: str,
        record_id: Optional[str] = None,
        detail: Optional[dict[str, Any]] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_audit (tenant_id, user_id, record_id, action, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    tenant_id,
                    user_id,
                    record_id,
                    action,
                    json.dumps(detail or {}, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()

    def list_entries(
        self,
        *,
        tenant_id: str,
        user_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if user_id:
                rows = conn.execute(
                    """
                    SELECT * FROM memory_audit
                    WHERE tenant_id = ? AND user_id = ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (tenant_id, user_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM memory_audit
                    WHERE tenant_id = ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (tenant_id, limit),
                ).fetchall()
        return [dict(row) for row in rows]
