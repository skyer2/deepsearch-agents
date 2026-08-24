"""
【Phase 16】MCP 异步 Tasks — 长任务提交 + 轮询，避免阻塞 step_timeout。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class MCPTaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


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


class MCPTaskManager:
    """内存 Task 存储 + 后台 worker（生产可换 Redis）。"""

    def __init__(self) -> None:
        self._tasks: dict[str, MCPTaskRecord] = {}
        self._lock = threading.Lock()

    def submit(
        self,
        *,
        server_module: str,
        tool_name: str,
        runner: Callable[[], Any],
    ) -> str:
        task_id = uuid.uuid4().hex
        record = MCPTaskRecord(
            id=task_id,
            tool_name=tool_name,
            server_module=server_module,
        )
        with self._lock:
            self._tasks[task_id] = record

        def _work() -> None:
            self._update(task_id, status=MCPTaskStatus.RUNNING)
            try:
                result = runner()
                self._update(task_id, status=MCPTaskStatus.DONE, result=result)
            except Exception as exc:
                self._update(task_id, status=MCPTaskStatus.FAILED, error=str(exc))

        threading.Thread(target=_work, name=f"mcp-task-{task_id[:8]}", daemon=True).start()
        return task_id

    def _update(
        self,
        task_id: str,
        *,
        status: MCPTaskStatus,
        result: Any = None,
        error: str = "",
    ) -> None:
        with self._lock:
            rec = self._tasks.get(task_id)
            if not rec:
                return
            rec.status = status
            rec.result = result
            rec.error = error
            rec.updated_at = time.time()

    def poll(self, task_id: str) -> Optional[MCPTaskRecord]:
        with self._lock:
            rec = self._tasks.get(task_id)
            return rec

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
            if rec.status in {MCPTaskStatus.DONE, MCPTaskStatus.FAILED}:
                return rec
            time.sleep(poll_interval_sec)
        raise TimeoutError(f"MCP task {task_id} timed out after {timeout_sec}s")


_task_manager: MCPTaskManager | None = None


def get_mcp_task_manager() -> MCPTaskManager:
    global _task_manager
    if _task_manager is None:
        _task_manager = MCPTaskManager()
    return _task_manager
