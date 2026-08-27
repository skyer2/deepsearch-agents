"""
Durable MCP Task Store — 协议风格 tasks/get / update / cancel。

stdio Server 是独立进程：必须用共享文件/DB，而不是双方各自的内存 dict。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional


class MCPTaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class MCPTaskRecord:
    id: str
    tool_name: str
    server_module: str
    status: MCPTaskStatus = MCPTaskStatus.PENDING
    result: Any = None
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    poll_interval_sec: float = 0.5
    tenant_id: str = ""
    user_id: str = ""
    run_id: str = ""

    def to_handle(self) -> dict[str, Any]:
        return {
            "task_id": self.id,
            "status": self.status.value,
            "poll_interval": self.poll_interval_sec,
            "kind": "mcp.task",
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.id,
            "tool_name": self.tool_name,
            "server_module": self.server_module,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "poll_interval": self.poll_interval_sec,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "run_id": self.run_id,
        }


def default_task_store_path() -> Path:
    raw = os.getenv("HARNESS_MCP_TASK_STORE", "").strip()
    if raw:
        return Path(raw)
    return Path("mcp_data") / "tasks.db"


class DurableTaskStore:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path or default_task_store_path())
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
                CREATE TABLE IF NOT EXISTS mcp_tasks (
                    id TEXT PRIMARY KEY,
                    tool_name TEXT NOT NULL,
                    server_module TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    poll_interval_sec REAL NOT NULL DEFAULT 0.5,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL DEFAULT '',
                    run_id TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()

    def create(
        self,
        *,
        tool_name: str,
        server_module: str,
        tenant_id: str = "",
        user_id: str = "",
        run_id: str = "",
        poll_interval_sec: float = 0.5,
        task_id: Optional[str] = None,
    ) -> MCPTaskRecord:
        record = MCPTaskRecord(
            id=task_id or uuid.uuid4().hex,
            tool_name=tool_name,
            server_module=server_module,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            poll_interval_sec=poll_interval_sec,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO mcp_tasks (
                    id, tool_name, server_module, status, result, error,
                    created_at, updated_at, poll_interval_sec, tenant_id, user_id, run_id
                ) VALUES (?, ?, ?, ?, NULL, '', ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.tool_name,
                    record.server_module,
                    record.status.value,
                    record.created_at,
                    record.updated_at,
                    record.poll_interval_sec,
                    record.tenant_id,
                    record.user_id,
                    record.run_id,
                ),
            )
            conn.commit()
        return record

    def get(self, task_id: str) -> Optional[MCPTaskRecord]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM mcp_tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return None
        return self._row_to_record(row)

    def update(
        self,
        task_id: str,
        *,
        status: Optional[MCPTaskStatus] = None,
        result: Any = None,
        error: Optional[str] = None,
    ) -> Optional[MCPTaskRecord]:
        rec = self.get(task_id)
        if rec is None:
            return None
        if rec.status == MCPTaskStatus.CANCELLED:
            return rec
        if status is not None:
            rec.status = status
        if result is not None:
            rec.result = result
        if error is not None:
            rec.error = error
        rec.updated_at = time.time()
        payload = json.dumps(rec.result, ensure_ascii=False) if rec.result is not None else None
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE mcp_tasks
                SET status=?, result=?, error=?, updated_at=?
                WHERE id=?
                """,
                (rec.status.value, payload, rec.error, rec.updated_at, task_id),
            )
            conn.commit()
        return rec

    def cancel(self, task_id: str) -> Optional[MCPTaskRecord]:
        return self.update(task_id, status=MCPTaskStatus.CANCELLED, error="cancelled")

    def _row_to_record(self, row: sqlite3.Row) -> MCPTaskRecord:
        result = None
        if row["result"]:
            try:
                result = json.loads(row["result"])
            except json.JSONDecodeError:
                result = row["result"]
        return MCPTaskRecord(
            id=row["id"],
            tool_name=row["tool_name"],
            server_module=row["server_module"],
            status=MCPTaskStatus(row["status"]),
            result=result,
            error=row["error"] or "",
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            poll_interval_sec=float(row["poll_interval_sec"] or 0.5),
            tenant_id=row["tenant_id"] or "",
            user_id=row["user_id"] or "",
            run_id=row["run_id"] or "",
        )


_store: DurableTaskStore | None = None


def get_durable_task_store() -> DurableTaskStore:
    global _store
    if _store is None:
        _store = DurableTaskStore()
    return _store


def reset_durable_task_store() -> None:
    global _store
    _store = None


class MCPTaskManager:
    """兼容旧 API：submit/poll/wait；底层走 durable store。"""

    def __init__(self, store: Optional[DurableTaskStore] = None):
        self.store = store or get_durable_task_store()

    def submit(
        self,
        *,
        server_module: str,
        tool_name: str,
        runner: Callable[[], Any],
        tenant_id: str = "",
        user_id: str = "",
        run_id: str = "",
    ) -> str:
        record = self.store.create(
            tool_name=tool_name,
            server_module=server_module,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
        )

        def _work() -> None:
            current = self.store.get(record.id)
            if current and current.status == MCPTaskStatus.CANCELLED:
                return
            self.store.update(record.id, status=MCPTaskStatus.RUNNING)
            try:
                result = runner()
                self.store.update(record.id, status=MCPTaskStatus.DONE, result=result)
            except Exception as exc:
                self.store.update(record.id, status=MCPTaskStatus.FAILED, error=str(exc))

        threading.Thread(target=_work, name=f"mcp-task-{record.id[:8]}", daemon=True).start()
        return record.id

    def poll(self, task_id: str) -> Optional[MCPTaskRecord]:
        return self.store.get(task_id)

    def get(self, task_id: str) -> Optional[MCPTaskRecord]:
        return self.store.get(task_id)

    def cancel(self, task_id: str) -> Optional[MCPTaskRecord]:
        return self.store.cancel(task_id)

    def wait(
        self,
        task_id: str,
        *,
        poll_interval_sec: float = 0.5,
        timeout_sec: float = 120.0,
    ) -> MCPTaskRecord:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            rec = self.poll(task_id)
            if rec is None:
                raise KeyError(f"task not found: {task_id}")
            if rec.status in {
                MCPTaskStatus.DONE,
                MCPTaskStatus.FAILED,
                MCPTaskStatus.CANCELLED,
            }:
                return rec
            time.sleep(min(poll_interval_sec, rec.poll_interval_sec or poll_interval_sec))
        raise TimeoutError(f"MCP task {task_id} timed out after {timeout_sec}s")


_task_manager: MCPTaskManager | None = None


def get_mcp_task_manager() -> MCPTaskManager:
    global _task_manager
    if _task_manager is None:
        _task_manager = MCPTaskManager()
    return _task_manager


def reset_mcp_task_manager() -> None:
    global _task_manager
    _task_manager = None
    reset_durable_task_store()
