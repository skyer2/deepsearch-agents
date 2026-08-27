"""
【Phase 16】启动时从 MCP Server list_tools 同步 Registry，避免 descriptor 漂移。
"""

from __future__ import annotations

from typing import Any

from app.mcp.registry import MCPToolDescriptor, mcp_registry
from app.mcp.session_pool import MCPSessionPool, use_session_pool
from app.mcp.tool_factory import build_mcp_tool
from app.mcp.mcp_runtime import (
    MYSQL_SERVER_MODULE,
    RAGFLOW_SERVER_MODULE,
    TAVILY_SERVER_MODULE,
)

# Harness 步级策略：MCP Server 不提供，由 Registry 维护
TOOL_STEP_POLICY: dict[str, dict[str, Any]] = {
    "internet_search": {
        "server": "tavily-mcp",
        "module": TAVILY_SERVER_MODULE,
        "step_types": ["network_search", "research"],
        "permissions": ["search", "read"],
    },
    "list_sql_tables": {
        "server": "mysql-mcp",
        "module": MYSQL_SERVER_MODULE,
        "step_types": ["database_query"],
        "permissions": ["read"],
    },
    "get_table_data": {
        "server": "mysql-mcp",
        "module": MYSQL_SERVER_MODULE,
        "step_types": ["database_query"],
        "permissions": ["read"],
    },
    "execute_sql_query": {
        "server": "mysql-mcp",
        "module": MYSQL_SERVER_MODULE,
        "step_types": ["database_query"],
        "permissions": ["read"],
    },
    "get_assistant_list": {
        "server": "ragflow-mcp",
        "module": RAGFLOW_SERVER_MODULE,
        "step_types": ["knowledge_base"],
        "permissions": ["read", "search"],
    },
    "create_ask_delete": {
        "server": "ragflow-mcp",
        "module": RAGFLOW_SERVER_MODULE,
        "step_types": ["knowledge_base"],
        "permissions": ["read", "search"],
    },
}

SERVER_MODULES_BY_ID = {
    "tavily-mcp": TAVILY_SERVER_MODULE,
    "mysql-mcp": MYSQL_SERVER_MODULE,
    "ragflow-mcp": RAGFLOW_SERVER_MODULE,
    "files-mcp": "app.mcp.servers.files_server",
}


def _list_tools_for_server(server_module: str) -> list[dict]:
    if use_session_pool():
        return MCPSessionPool.list_tools_sync(server_module)
    from app.mcp.mcp_runtime import MCPServerRuntime

    runtime = MCPServerRuntime(server_module)
    names = runtime.list_tools_sync() if hasattr(runtime, "list_tools_sync") else []
    return [{"name": n, "description": ""} for n in names]


def sync_mcp_registry_from_servers(enabled_servers: list[str]) -> int:
    """
    从 MCP Server 拉取 tools 列表，更新 Registry 描述与 LangChain 可调用体。
    enabled_servers: server id 列表，如 ['tavily-mcp','mysql-mcp']
    """
    synced = 0
    modules = {sid: SERVER_MODULES_BY_ID[sid] for sid in enabled_servers if sid in SERVER_MODULES_BY_ID}

    remote_by_name: dict[str, dict] = {}
    for _sid, module in modules.items():
        try:
            for item in _list_tools_for_server(module):
                remote_by_name[item["name"]] = item
        except Exception as exc:
            print(f"[RegistrySync] list_tools failed for {module}: {exc}")

    for tool_name, policy in TOOL_STEP_POLICY.items():
        server_id = policy["server"]
        if server_id not in modules:
            continue
        remote = remote_by_name.get(tool_name)
        if not remote:
            continue
        description = remote.get("description") or tool_name
        server_module = policy["module"]
        step_types = policy["step_types"]
        langchain_tool = build_mcp_tool(
            server_module=server_module,
            server_id=server_id,
            tool_name=tool_name,
            description=description,
            step_type=step_types[0] if step_types else "",
        )
        mcp_registry.register_or_update(
            MCPToolDescriptor(
                name=tool_name,
                description=description,
                server=server_id,
                permissions=list(policy.get("permissions") or []),
                step_types=step_types,
                transport="mcp-pool" if use_session_pool() else "mcp-stdio",
            ),
            langchain_tool,
        )
        synced += 1
    return synced
