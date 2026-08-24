"""MCP Server 与 Registry 桥接测试。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_tavily_server_import():
    from app.mcp.servers import tavily_server

    assert tavily_server.mcp is not None
    print("[OK] tavily MCP server module import")


def test_mysql_server_import():
    from app.mcp.servers import mysql_server

    assert mysql_server.mcp is not None
    print("[OK] mysql MCP server module import")


def test_ragflow_server_import():
    from app.mcp.servers import ragflow_server

    assert ragflow_server.mcp is not None
    print("[OK] ragflow MCP server module import")


def test_registry_default_transport():
    from app.config.loader import reload_harness_config
    from app.mcp.client import bootstrap_mcp_registry
    from app.mcp.registry import MCPRegistry
    import app.mcp.registry as registry_mod

    reload_harness_config()
    fresh = MCPRegistry()
    old_registry = registry_mod.mcp_registry
    try:
        registry_mod.mcp_registry = fresh
        bootstrap_mcp_registry()
        desc = {d.name: d for d in fresh.list_descriptors()}
        assert desc["internet_search"].transport == "langchain-tool"
        assert desc["internet_search"].server == "tavily-mcp"
        assert desc["list_sql_tables"].server == "mysql-mcp"
        assert desc["get_assistant_list"].server == "ragflow-mcp"
        db_ctx = fresh.build_tool_context("database_query")
        assert "list_sql_tables" in db_ctx
        assert "execute_sql_query" in db_ctx
        kb_ctx = fresh.build_tool_context("knowledge_base")
        assert "get_assistant_list" in kb_ctx
        print("[OK] registry default langchain-tool transport + db/ragflow descriptors")
    finally:
        registry_mod.mcp_registry = old_registry


def test_subagent_builders_use_registry_tools():
    from app.config.loader import reload_harness_config
    from app.mcp.client import bootstrap_mcp_registry
    from app.agent.subagents.database_query_agent import build_database_query_agent
    from app.agent.subagents.knowledge_base_agent import build_knowledge_base_agent
    from app.agent.subagents.network_search_agent import build_network_search_agent
    import app.mcp.registry as registry_mod
    from app.mcp.registry import MCPRegistry

    reload_harness_config()
    fresh = MCPRegistry()
    old_registry = registry_mod.mcp_registry
    try:
        registry_mod.mcp_registry = fresh
        bootstrap_mcp_registry()

        db_agent = build_database_query_agent()
        assert len(db_agent["tools"]) == 3
        assert db_agent["tools"][0].name == "list_sql_tables"

        kb_agent = build_knowledge_base_agent()
        assert len(kb_agent["tools"]) == 2
        assert kb_agent["tools"][0].name == "get_assistant_list"

        net_agent = build_network_search_agent()
        assert net_agent["tools"][0].name == "internet_search"
        print("[OK] subagent builders resolved tools")
    finally:
        registry_mod.mcp_registry = old_registry


def test_mcp_runtime_list_tools_optional():
    import os

    if not os.getenv("TAVILY_API_KEY"):
        print("[SKIP] mcp runtime live test (no TAVILY_API_KEY)")
        return

    import asyncio

    from app.mcp.mcp_runtime import MCPServerRuntime, TAVILY_SERVER_MODULE

    runtime = MCPServerRuntime(TAVILY_SERVER_MODULE)
    tools = asyncio.run(runtime.list_tools())
    assert "internet_search" in tools
    print(f"[OK] mcp runtime list_tools={tools}")


if __name__ == "__main__":
    test_tavily_server_import()
    test_mysql_server_import()
    test_ragflow_server_import()
    test_registry_default_transport()
    test_subagent_builders_use_registry_tools()
    test_mcp_runtime_list_tools_optional()
    print("\n=== MCP tests passed ===")
