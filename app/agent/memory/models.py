"""
【Phase 15】生产级记忆模型 — 类型化 fact、版本、租户、召回分数。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class MemoryType(str, Enum):
    """长期记忆类型（分库语义）。"""

    SEMANTIC = "semantic"  # 可复用研究结论、领域事实
    EPISODIC = "episodic"  # 某次任务/步骤的具体经历
    PREFERENCE = "preference"  # 用户偏好（交付物、风格等）
    PROCEDURAL = "procedural"  # 流程/操作习惯


class WriteSource(str, Enum):
    """写入来源。"""

    FINALIZE = "finalize"
    STEP_INCREMENTAL = "step_incremental"
    USER_EXPLICIT = "user_explicit"
    SEED = "seed"
    MEM0 = "mem0"


@dataclass
class MemoryRecord:
    fact: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    tenant_id: str = "default"
    user_id: str = ""
    memory_type: MemoryType = MemoryType.SEMANTIC
    version: int = 1
    confidence: float = 0.8
    write_source: WriteSource = WriteSource.FINALIZE
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    source: str = "remember"  # 向后兼容旧字段
    task: str = ""
    topic: str = ""
    session_id: str = ""
    is_deleted: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    recall_score: Optional[float] = None
    embedding: Optional[list[float]] = field(default=None, repr=False)

    def is_expired(self, ttl_days: int) -> bool:
        if ttl_days <= 0:
            return False
        try:
            created = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - created).days
            return age_days > ttl_days
        except ValueError:
            return False

    def type_label(self) -> str:
        return self.memory_type.value if isinstance(self.memory_type, MemoryType) else str(self.memory_type)

    def to_dict(self) -> dict[str, Any]:
        mt = self.memory_type.value if isinstance(self.memory_type, MemoryType) else str(self.memory_type)
        ws = self.write_source.value if isinstance(self.write_source, WriteSource) else str(self.write_source)
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "fact": self.fact,
            "memory_type": mt,
            "version": self.version,
            "confidence": self.confidence,
            "write_source": ws,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source": self.source,
            "task": self.task,
            "topic": self.topic,
            "session_id": self.session_id,
            "is_deleted": self.is_deleted,
            "metadata": self.metadata,
            "recall_score": self.recall_score,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryRecord":
        mt_raw = data.get("memory_type", MemoryType.SEMANTIC.value)
        try:
            memory_type = MemoryType(str(mt_raw))
        except ValueError:
            memory_type = MemoryType.SEMANTIC
        ws_raw = data.get("write_source", WriteSource.FINALIZE.value)
        try:
            write_source = WriteSource(str(ws_raw))
        except ValueError:
            write_source = WriteSource.FINALIZE
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            tenant_id=str(data.get("tenant_id") or "default"),
            user_id=str(data.get("user_id") or ""),
            fact=str(data.get("fact", "")),
            memory_type=memory_type,
            version=int(data.get("version") or 1),
            confidence=float(data.get("confidence") or 0.8),
            write_source=write_source,
            created_at=str(data.get("created_at") or datetime.now(timezone.utc).isoformat()),
            updated_at=str(data.get("updated_at") or data.get("created_at") or datetime.now(timezone.utc).isoformat()),
            source=str(data.get("source", "remember")),
            task=str(data.get("task", "")),
            topic=str(data.get("topic", "")),
            session_id=str(data.get("session_id", "")),
            is_deleted=bool(data.get("is_deleted", False)),
            metadata=dict(data.get("metadata") or {}),
            recall_score=data.get("recall_score"),
        )


@dataclass
class MemoryWriteRequest:
    fact: str
    memory_type: MemoryType = MemoryType.SEMANTIC
    confidence: float = 0.8
    write_source: WriteSource = WriteSource.FINALIZE
    task: str = ""
    topic: str = ""
    session_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecallResult:
    records: list[MemoryRecord]
    recall_at_k: float
    keyword_hits: int
    embedding_used: bool
