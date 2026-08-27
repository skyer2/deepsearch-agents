"""Durable MCP tool-call audit（SQLite），替代进程内 deque(500)。"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Optional


def default_audit_path() -> Path:
    raw = os.getenv("HARNESS_MCP_AUDIT_STORE", "").strip()
    if raw:
        return Path(raw)
    return Path("mcp_data") / "audit.db"


@dataclass
class ToolCallAudit:
    timestamp: float = 0.0
    trace_id: str = ""
    run_id: str = ""
    task_id: str = ""
    tenant_id: str = ""
    user_id: str = ""
    server_id: str = ""
    tool_name: str = ""
    status: str = ""
    latency_ms: float = 0.0
    error: str = ""
    attempt: int = 0
    allowed: bool = True
    risk: str = ""
    args_hash: str = ""
    approval: str = ""
    artifact_ref: str = ""
    transport: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_entry(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "server_id": self.server_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "latency_ms": int(self.latency_ms),
            "error": self.error,
            "allowed": self.allowed,
            "risk": self.risk,
            "args_hash": self.args_hash,
            "approval": self.approval,
            "artifact_ref": self.artifact_ref,
            "transport": self.transport,
            "extra": {**self.extra, "attempt": self.attempt},
        }


class MCPAuditStore:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path or default_audit_path())
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    trace_id TEXT NOT NULL DEFAULT '',
                    run_id TEXT NOT NULL DEFAULT '',
                    task_id TEXT NOT NULL DEFAULT '',
                    tenant_id TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL DEFAULT '',
                    server_id TEXT NOT NULL DEFAULT '',
                    tool_name TEXT NOT NULL DEFAULT '',
                    risk TEXT NOT NULL DEFAULT '',
                    args_hash TEXT NOT NULL DEFAULT '',
                    approval TEXT NOT NULL DEFAULT '',
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    allowed INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    artifact_ref TEXT NOT NULL DEFAULT '',
                    transport TEXT NOT NULL DEFAULT '',
                    extra TEXT
                )
                """
            )
            conn.commit()

    def record(self, audit: ToolCallAudit | dict[str, Any]) -> None:
        entry = audit.to_entry() if isinstance(audit, ToolCallAudit) else dict(audit)
        self.write(entry)

    def write(self, entry: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO mcp_audit (
                    timestamp, trace_id, run_id, task_id, tenant_id, user_id,
                    server_id, tool_name, risk, args_hash, approval, latency_ms,
                    allowed, status, error, artifact_ref, transport, extra
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    float(entry.get("timestamp") or time.time()),
                    str(entry.get("trace_id") or ""),
                    str(entry.get("run_id") or ""),
                    str(entry.get("task_id") or ""),
                    str(entry.get("tenant_id") or ""),
                    str(entry.get("user_id") or ""),
                    str(entry.get("server_id") or entry.get("server_module") or ""),
                    str(entry.get("tool_name") or ""),
                    str(entry.get("risk") or ""),
                    str(entry.get("args_hash") or ""),
                    str(entry.get("approval") or ""),
                    int(entry.get("latency_ms") or 0),
                    1 if entry.get("allowed", True) else 0,
                    str(entry.get("status") or ""),
                    str(entry.get("error") or ""),
                    str(entry.get("artifact_ref") or ""),
                    str(entry.get("transport") or ""),
                    json.dumps(entry.get("extra") or {}, ensure_ascii=False),
                ),
            )
            conn.commit()

    def list_entries(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM mcp_audit ORDER BY id DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        items = []
        for row in rows:
            items.append(
                {
                    "timestamp": row["timestamp"],
                    "trace_id": row["trace_id"],
                    "run_id": row["run_id"],
                    "task_id": row["task_id"],
                    "tenant_id": row["tenant_id"],
                    "user_id": row["user_id"],
                    "server_id": row["server_id"],
                    "tool_name": row["tool_name"],
                    "allowed": bool(row["allowed"]),
                    "latency_ms": row["latency_ms"],
                    "error": row["error"],
                    "transport": row["transport"],
                    "artifact_ref": row["artifact_ref"],
                    "status": row["status"],
                    "risk": row["risk"],
                }
            )
        return items


_audit: MCPAuditStore | None = None


def get_mcp_audit_store() -> MCPAuditStore:
    global _audit
    if _audit is None:
        _audit = MCPAuditStore()
    return _audit
