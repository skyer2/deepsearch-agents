"""
MCP Client 桥接层

【Phase 16】HARNESS_MCP_ENABLED=true 时 Tavily/MySQL/RAGFlow/Files 统一走 MCP Gateway；
默认仍 langchain-tool 直连（Eval / 无子进程环境）。
"""

from __future__ import annotations

import os
from typing import Any

from app.config.loader import get_harness_config
from app.mcp.registry import MCPToolDescriptor, mcp_registry
from app.tools.db_tools import execute_sql_query, get_table_data, list_sql_tables
from app.tools.markdown_tools import generate_markdown
from app.tools.pdf_tools import convert_md_to_pdf
from app.tools.ragflow_tools import create_ask_delete, get_assistant_list
from app.tools.tavily_tool import internet_search
from app.tools.upload_file_read_tool import read_file_content

_LOCAL_DB_TOOLS = [list_sql_tables, get_table_data, execute_sql_query]
_LOCAL_RAGFLOW_TOOLS = [get_assistant_list, create_ask_delete]
_LOCAL_FILE_TOOLS = [read_file_content, generate_markdown, convert_md_to_pdf]

_MCP_SERVER_ENV = {
    "tavily-mcp": "HARNESS_MCP_TAVILY",
    "mysql-mcp": "HARNESS_MCP_MYSQL",
    "ragflow-mcp": "HARNESS_MCP_RAGFLOW",
    "files-mcp": "HARNESS_MCP_FILES",
}

_ALL_SERVERS = ("tavily-mcp", "mysql-mcp", "ragflow-mcp", "files-mcp")


def _env_enabled(name: str) -> bool:
    return os.getenv(name, "").lower() in {"1", "true", "yes", "on"}


def is_mcp_global_enabled() -> bool:
    cfg = get_harness_config()
    return _env_enabled("HARNESS_MCP_ENABLED") or cfg.mcp_enabled


def is_mcp_server_enabled(server_id: str, config_flag: bool) -> bool:
    if is_mcp_global_enabled():
        return True
    env_name = _MCP_SERVER_ENV.get(server_id, "")
    if env_name and _env_enabled(env_name):
        return True
    return config_flag


def _enabled_mcp_servers() -> list[str]:
    cfg = get_harness_config()
    return [
        sid
        for sid in _ALL_SERVERS
        if is_mcp_server_enabled(
            sid,
            {
                "tavily-mcp": cfg.mcp_tavily_enabled,
                "mysql-mcp": cfg.mcp_mysql_enabled,
                "ragflow-mcp": cfg.mcp_ragflow_enabled,
                "files-mcp": cfg.mcp_files_enabled,
            }[sid],
        )
    ]


def _register_local_tool(desc: MCPToolDescriptor, tool: Any) -> None:
    mcp_registry.register(desc, tool, transport="langchain-tool")


def _bootstrap_local_registry() -> None:
    db_tool_map = {t.name: t for t in _LOCAL_DB_TOOLS}
    ragflow_tool_map = {t.name: t for t in _LOCAL_RAGFLOW_TOOLS}
    file_tool_map = {t.name: t for t in _LOCAL_FILE_TOOLS}

    _register_local_tool(
        MCPToolDescriptor(
            name="internet_search",
            description="检索互联网公开资料",
            server="tavily-mcp",
            permissions=["search", "read"],
            step_types=["network_search"],
        ),
        internet_search,
    )
    for name, desc in [
        (
            "list_sql_tables",
            MCPToolDescriptor(
                name="list_sql_tables",
                description="列出 MySQL 可用表",
                server="mysql-mcp",
                permissions=["read"],
                step_types=["database_query"],
            ),
        ),
        (
            "get_table_data",
            MCPToolDescriptor(
                name="get_table_data",
                description="预览 MySQL 表数据",
                server="mysql-mcp",
                permissions=["read"],
                step_types=["database_query"],
            ),
        ),
        (
            "execute_sql_query",
            MCPToolDescriptor(
                name="execute_sql_query",
                description="执行 MySQL 自定义查询",
                server="mysql-mcp",
                permissions=["read"],
                step_types=["database_query"],
            ),
        ),
    ]:
        _register_local_tool(desc, db_tool_map[name])
    for name, desc in [
        (
            "get_assistant_list",
            MCPToolDescriptor(
                name="get_assistant_list",
                description="查询 RAGFlow 可用助手",
                server="ragflow-mcp",
                permissions=["read", "search"],
                step_types=["knowledge_base"],
            ),
        ),
        (
            "create_ask_delete",
            MCPToolDescriptor(
                name="create_ask_delete",
                description="向 RAGFlow 助手提问",
                server="ragflow-mcp",
                permissions=["read", "search"],
                step_types=["knowledge_base"],
            ),
        ),
    ]:
        _register_local_tool(desc, ragflow_tool_map[name])
    for name, desc in [
        (
            "read_file_content",
            MCPToolDescriptor(
                name="read_file_content",
                description="读取当前会话上传文件",
                server="file-mcp",
                permissions=["read"],
                step_types=["file_read", "research"],
            ),
        ),
        (
            "generate_markdown",
            MCPToolDescriptor(
                name="generate_markdown",
                description="生成 Markdown 报告",
                server="file-mcp",
                permissions=["write"],
                step_types=["generate_markdown", "summarize"],
            ),
        ),
        (
            "convert_md_to_pdf",
            MCPToolDescriptor(
                name="convert_md_to_pdf",
                description="将 Markdown 转换为 PDF",
                server="file-mcp",
                permissions=["write"],
                step_types=["convert_pdf"],
            ),
        ),
    ]:
        _register_local_tool(desc, file_tool_map[name])


_CONTEXT_STEP_TYPES = [
    "network_search",
    "database_query",
    "knowledge_base",
    "file_read",
    "research",
    "generate_markdown",
    "summarize",
    "convert_pdf",
]


def _register_context_tools() -> None:
    from app.tools.artifact_tools import read_artifact, read_evidence

    _register_local_tool(
        MCPToolDescriptor(
            name="read_artifact",
            description="按 artifact_id 回读已外置的原始工具结果",
            server="context-store",
            permissions=["read"],
            step_types=list(_CONTEXT_STEP_TYPES),
        ),
        read_artifact,
    )
    _register_local_tool(
        MCPToolDescriptor(
            name="read_evidence",
            description="按 evidence_id 回读证据 span",
            server="context-store",
            permissions=["read"],
            step_types=list(_CONTEXT_STEP_TYPES),
        ),
        read_evidence,
    )


def _register_static_mcp_tools(server_ids: list[str]) -> None:
    from app.mcp.registry_sync import TOOL_STEP_POLICY
    from app.mcp.tool_factory import build_mcp_tool

    for tool_name, policy in TOOL_STEP_POLICY.items():
        if policy["server"] not in server_ids:
            continue
        langchain_tool = build_mcp_tool(
            server_module=policy["module"],
            server_id=policy["server"],
            tool_name=tool_name,
            description=tool_name,
            step_type=policy["step_types"][0],
        )
        mcp_registry.register(
            MCPToolDescriptor(
                name=tool_name,
                description=tool_name,
                server=policy["server"],
                permissions=list(policy.get("permissions") or []),
                step_types=policy["step_types"],
            ),
            langchain_tool,
            transport="mcp-pool",
        )


def _register_mcp_files_tools() -> None:
    from app.mcp.registry_sync import SERVER_MODULES_BY_ID
    from app.mcp.tool_factory import build_mcp_tool

    module = SERVER_MODULES_BY_ID["files-mcp"]
    specs = [
        ("read_file_content", ["file_read"], ["read"], "读取会话文件"),
        ("generate_markdown", ["generate_markdown", "summarize"], ["write"], "生成 Markdown"),
        ("convert_md_to_pdf", ["convert_pdf"], ["write"], "异步 PDF 转换"),
    ]
    for name, step_types, perms, desc in specs:
        tool = build_mcp_tool(
            server_module=module,
            server_id="files-mcp",
            tool_name="convert_md_to_pdf_async" if name == "convert_md_to_pdf" else name,
            description=desc,
            step_type=step_types[0],
        )
        mcp_registry.register(
            MCPToolDescriptor(
                name=name,
                description=desc,
                server="files-mcp",
                permissions=perms,
                step_types=step_types,
            ),
            tool,
            transport="mcp-pool",
        )


def _bootstrap_hybrid_registry(enabled_mcp: list[str]) -> None:
    """按 Server 粒度混合：已启用走 MCP，未启用走 LangChain 直连。"""
    from app.mcp.registry_sync import sync_mcp_registry_from_servers

    remote_ids = [s for s in enabled_mcp if s != "files-mcp"]
    cfg = get_harness_config()

    if remote_ids:
        if cfg.mcp_sync_on_startup:
            try:
                synced = sync_mcp_registry_from_servers(remote_ids)
                print(f"[MCP] Registry synced: {synced} tools from {remote_ids}")
            except Exception as exc:
                print(f"[MCP] sync failed, static MCP tools: {exc}")
                _register_static_mcp_tools(remote_ids)
        else:
            _register_static_mcp_tools(remote_ids)

    if "tavily-mcp" not in enabled_mcp:
        _register_local_tool(
            MCPToolDescriptor(
                name="internet_search",
                description="检索互联网公开资料",
                server="tavily-mcp",
                permissions=["search", "read"],
                step_types=["network_search"],
            ),
            internet_search,
        )
    if "mysql-mcp" not in enabled_mcp:
        db_map = {t.name: t for t in _LOCAL_DB_TOOLS}
        for name in ("list_sql_tables", "get_table_data", "execute_sql_query"):
            _register_local_tool(
                MCPToolDescriptor(
                    name=name,
                    description=name,
                    server="mysql-mcp",
                    permissions=["read"],
                    step_types=["database_query", "research"],
                ),
                db_map[name],
            )
    if "ragflow-mcp" not in enabled_mcp:
        rf_map = {t.name: t for t in _LOCAL_RAGFLOW_TOOLS}
        for name in ("get_assistant_list", "create_ask_delete"):
            _register_local_tool(
                MCPToolDescriptor(
                    name=name,
                    description=name,
                    server="ragflow-mcp",
                    permissions=["read", "search"],
                    step_types=["knowledge_base"],
                ),
                rf_map[name],
            )

    if "files-mcp" in enabled_mcp:
        _register_mcp_files_tools()
    else:
        file_map = {t.name: t for t in _LOCAL_FILE_TOOLS}
        for name in ("read_file_content", "generate_markdown", "convert_md_to_pdf"):
            _register_local_tool(
                MCPToolDescriptor(
                    name=name,
                    description=name,
                    server="file-mcp",
                    permissions=["read"] if name == "read_file_content" else ["write"],
                    step_types={
                        "read_file_content": ["file_read"],
                        "generate_markdown": ["generate_markdown", "summarize"],
                        "convert_md_to_pdf": ["convert_pdf"],
                    }[name],
                ),
                file_map[name],
            )


def bootstrap_mcp_registry(*, force: bool = False) -> None:
    if mcp_registry.list_descriptors() and not force:
        return
    if force:
        mcp_registry.clear()

    enabled = _enabled_mcp_servers()
    if enabled:
        _bootstrap_hybrid_registry(enabled)
    else:
        _bootstrap_local_registry()
    _register_context_tools()


def get_internet_search_tool() -> Any:
    bootstrap_mcp_registry()
    return mcp_registry.get_tool("internet_search")


def get_db_tools() -> list[Any]:
    bootstrap_mcp_registry()
    return [
        mcp_registry.get_tool(n)
        for n in ("list_sql_tables", "get_table_data", "execute_sql_query")
    ]


def get_ragflow_tools() -> list[Any]:
    bootstrap_mcp_registry()
    return [
        mcp_registry.get_tool(n)
        for n in ("get_assistant_list", "create_ask_delete")
    ]


def get_file_tools() -> list[Any]:
    bootstrap_mcp_registry()
    names = ["read_file_content", "generate_markdown", "convert_md_to_pdf"]
    return [mcp_registry.get_tool(n) for n in names if mcp_registry.get_tool(n)]
