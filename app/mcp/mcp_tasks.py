"""MCP Tasks facade.

The previous in-memory dict lived in the Agent process *and* each stdio
subprocess, so ``task_id`` returned by files-mcp could not be polled by the
client. Production tasks are durable SQLite records shared via
``HARNESS_MCP_TASK_STORE``.
"""

from __future__ import annotations

from app.mcp.task_store import (
    MCPTaskManager,
    MCPTaskRecord,
    MCPTaskRecord as MCPTask,
    MCPTaskStatus,
    get_mcp_task_manager,
    reset_durable_task_store,
    reset_mcp_task_manager,
)

__all__ = [
    "MCPTask",
    "MCPTaskRecord",
    "MCPTaskManager",
    "MCPTaskStatus",
    "get_mcp_task_manager",
    "reset_durable_task_store",
    "reset_mcp_task_manager",
]
