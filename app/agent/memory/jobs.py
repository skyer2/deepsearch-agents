"""
Durable Memory Job Queue — 巩固任务不能只靠 in-process asyncio.create_task。

finalize 先把 candidate event 写入同一 Memory Store 的 jobs 表，再尝试 drain。
进程崩溃后，下次 MemoryStore 初始化或显式 drain 仍能续跑。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MemoryJob:
    job_type: str
    payload: dict[str, Any]
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "pending"  # pending | running | done | failed
    attempts: int = 0
    available_at: str = field(default_factory=_now)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job_type": self.job_type,
            "payload": dict(self.payload),
            "status": self.status,
            "attempts": self.attempts,
            "available_at": self.available_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_error": self.last_error,
        }

    @classmethod
    def from_row(cls, row: Any) -> "MemoryJob":
        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        return cls(
            id=str(row["id"]),
            job_type=str(row["job_type"]),
            payload=dict(payload or {}),
            status=str(row["status"] or "pending"),
            attempts=int(row["attempts"] or 0),
            available_at=str(row["available_at"] or ""),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
            last_error=str(row["last_error"] or ""),
        )


def consolidation_job(*, tenant_id: str, user_id: str, project_id: str = "") -> MemoryJob:
    return MemoryJob(
        job_type="consolidate",
        payload={
            "tenant_id": tenant_id,
            "user_id": user_id,
            "project_id": project_id or "",
        },
    )
