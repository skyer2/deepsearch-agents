"""
【Phase 10/16】Tools / MCP API
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.config.loader import get_harness_config
from app.mcp.client import bootstrap_mcp_registry, is_mcp_global_enabled
from app.mcp.mcp_gateway import get_mcp_gateway, reset_mcp_gateway
from app.mcp.mcp_tasks import get_mcp_task_manager
from app.mcp.registry import mcp_registry
from app.mcp.session_pool import use_session_pool
from app.mcp.tool_gateway import get_tool_gateway, reset_tool_gateway

router = APIRouter(prefix="/api/tools", tags=["tools"])


@router.get("/registry")
def list_tool_registry() -> dict[str, Any]:
    bootstrap_mcp_registry()
    config = get_harness_config()
    return {
        "total": len(mcp_registry.list_descriptors()),
        "tools": mcp_registry.to_catalog(),
        "mcp": {
            "enabled": config.mcp_enabled or is_mcp_global_enabled(),
            "tavily": config.mcp_tavily_enabled,
            "mysql": config.mcp_mysql_enabled,
            "ragflow": config.mcp_ragflow_enabled,
            "files": config.mcp_files_enabled,
            "transport": config.mcp_transport,
            "pool_enabled": config.mcp_pool_enabled,
            "sync_on_startup": config.mcp_sync_on_startup,
            "tasks_enabled": config.mcp_tasks_enabled,
        },
    }


@router.get("/policy")
def tool_gateway_policy() -> dict[str, Any]:
    bootstrap_mcp_registry()
    reset_tool_gateway()
    gateway = get_tool_gateway()
    return gateway.describe_policy()


@router.get("/mcp")
def mcp_transport_status() -> dict[str, Any]:
    bootstrap_mcp_registry()
    by_transport: dict[str, list[str]] = {}
    for item in mcp_registry.to_catalog():
        transport = item.get("transport", "unknown")
        by_transport.setdefault(transport, []).append(item["name"])
    return {
        "by_transport": by_transport,
        "pool_enabled": use_session_pool(),
        "note": "Phase16: mcp-pool + Gateway；关闭 MCP 时 langchain-tool 直连",
    }


@router.get("/mcp/gateway/audit")
def mcp_gateway_audit(limit: int = 50) -> dict[str, Any]:
    reset_mcp_gateway()
    gw = get_mcp_gateway()
    return {"entries": gw.list_audit(limit=limit)}


@router.get("/mcp/tasks/{task_id}")
def mcp_task_status(task_id: str) -> dict[str, Any]:
    rec = get_mcp_task_manager().poll(task_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="task not found")
    return {
        "task_id": rec.id,
        "tool_name": rec.tool_name,
        "server_module": rec.server_module,
        "status": rec.status.value,
        "result": rec.result,
        "error": rec.error,
        "created_at": rec.created_at,
        "updated_at": rec.updated_at,
    }
