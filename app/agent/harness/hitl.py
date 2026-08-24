"""
HITL 人工审批协调器

Harness execute 阶段命中 interrupt_on 后暂停，等待前端 POST /api/task/{id}/resume。
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional


class HitlCoordinator:
    def __init__(self) -> None:
        self._waiters: dict[str, asyncio.Future[list[dict[str, Any]]]] = {}
        self._payloads: dict[str, dict[str, Any]] = {}

    async def wait_for_decisions(
        self,
        session_id: str,
        payload: dict[str, Any],
        timeout_sec: int = 600,
    ) -> list[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        if session_id in self._waiters and not self._waiters[session_id].done():
            raise RuntimeError(f"HITL already pending for session {session_id}")

        fut: asyncio.Future[list[dict[str, Any]]] = loop.create_future()
        self._waiters[session_id] = fut
        self._payloads[session_id] = payload

        try:
            return await asyncio.wait_for(fut, timeout=timeout_sec)
        except asyncio.TimeoutError as exc:
            if not fut.done():
                fut.cancel()
            raise TimeoutError(f"HITL approval timeout for session {session_id}") from exc
        finally:
            self._waiters.pop(session_id, None)
            self._payloads.pop(session_id, None)

    def submit_decisions(
        self,
        session_id: str,
        decisions: list[dict[str, Any]],
    ) -> bool:
        fut = self._waiters.get(session_id)
        if fut is None or fut.done():
            return False
        fut.set_result(decisions)
        return True

    def get_pending(self, session_id: str) -> Optional[dict[str, Any]]:
        return self._payloads.get(session_id)

    def has_pending(self, session_id: str) -> bool:
        fut = self._waiters.get(session_id)
        return fut is not None and not fut.done()


hitl_coordinator = HitlCoordinator()
