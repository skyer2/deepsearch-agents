"""Memory backend 抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from app.agent.memory.models import MemoryRecord, MemoryWriteRequest, RecallResult


class MemoryBackend(ABC):
    @abstractmethod
    async def recall(
        self,
        query: str,
        *,
        tenant_id: str,
        user_id: str,
        top_k: int,
    ) -> RecallResult:
        raise NotImplementedError

    @abstractmethod
    async def remember_writes(
        self,
        writes: list[MemoryWriteRequest],
        *,
        tenant_id: str,
        user_id: str,
    ) -> int:
        raise NotImplementedError

    @abstractmethod
    def list_records(
        self,
        *,
        tenant_id: str,
        user_id: str,
        include_deleted: bool = False,
    ) -> list[MemoryRecord]:
        raise NotImplementedError

    @abstractmethod
    async def delete_record(
        self,
        record_id: str,
        *,
        tenant_id: str,
        user_id: str,
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def delete_all(
        self,
        *,
        tenant_id: str,
        user_id: str,
    ) -> int:
        raise NotImplementedError
