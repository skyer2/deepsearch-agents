"""
MCP 运行时客户端

通过 stdio 子进程连接真 MCP Server，并将工具调用桥接为 LangChain 可调用对象。
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, Literal, Optional

from langchain_core.tools import StructuredTool
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.api.monitor import monitor

MYSQL_SERVER_MODULE = "app.mcp.servers.mysql_server"
RAGFLOW_SERVER_MODULE = "app.mcp.servers.ragflow_server"
TAVILY_SERVER_MODULE = "app.mcp.servers.tavily_server"
FILES_SERVER_MODULE = "app.mcp.servers.files_server"


class MCPServerRuntime:
    """按需启动 MCP Server 子进程并调用工具。"""

    def __init__(
        self,
        server_module: str,
        python_executable: Optional[str] = None,
    ):
        self.server_module = server_module
        self._python = python_executable or sys.executable

    def _server_params(self) -> StdioServerParameters:
        from app.mcp.server_env import build_server_env, server_id_for_module

        return StdioServerParameters(
            command=self._python,
            args=["-m", self.server_module],
            env=build_server_env(server_id_for_module(self.server_module)),
        )

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_sec: float = 30.0,
        max_retries: int = 1,
    ) -> Any:
        last_exc: Exception | None = None
        for attempt in range(max(1, max_retries + 1)):
            try:
                return await asyncio.wait_for(
                    self._call_tool_once(tool_name, arguments),
                    timeout=timeout_sec,
                )
            except asyncio.TimeoutError as exc:
                last_exc = exc
                if attempt >= max_retries:
                    raise TimeoutError(
                        f"MCP tool {tool_name} timed out after {timeout_sec}s"
                    ) from exc
            except Exception as exc:
                last_exc = exc
                if attempt >= max_retries:
                    raise
        if last_exc:
            raise last_exc
        return ""

    async def _call_tool_once(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        async with stdio_client(self._server_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                from app.mcp.result_normalizer import normalize_mcp_result

                visible = normalize_mcp_result(result).model_visible()
                if isinstance(visible, str):
                    try:
                        return json.loads(visible)
                    except json.JSONDecodeError:
                        return visible
                return visible

    async def call_tool_legacy(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """向后兼容：无 timeout/retry。"""
        return await self._call_tool_once(tool_name, arguments)

    async def list_tools(self) -> list[str]:
        async with stdio_client(self._server_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                return [tool.name for tool in tools.tools]

    def list_tools_sync(self) -> list[str]:
        from app.mcp.session_pool import MCPSessionPool, use_session_pool

        if use_session_pool():
            items = MCPSessionPool.list_tools_sync(self.server_module)
            return [item["name"] for item in items]
        return asyncio.run(self.list_tools())

    def call_tool_sync(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        from app.config.loader import get_harness_config

        cfg = get_harness_config()
        return asyncio.run(
            self.call_tool(
                tool_name,
                arguments,
                timeout_sec=float(cfg.mcp_call_timeout_sec),
                max_retries=int(cfg.mcp_max_retries),
            )
        )


def _as_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False)


def build_internet_search_mcp_tool(
    runtime: Optional[MCPServerRuntime] = None,
) -> StructuredTool:
    client = runtime or MCPServerRuntime(TAVILY_SERVER_MODULE)

    def _invoke(
        query: str,
        topic: Literal["news", "finance", "general"] = "general",
        max_results: int = 5,
        include_raw_content: bool = False,
    ) -> dict[str, Any]:
        monitor.report_tool(
            tool_name="internet_search",
            args={
                "query": query,
                "topic": topic,
                "max_results": max_results,
                "include_raw_content": include_raw_content,
                "transport": "mcp-stdio",
            },
        )
        result = client.call_tool_sync(
            "internet_search",
            {
                "query": query,
                "topic": topic,
                "max_results": max_results,
                "include_raw_content": include_raw_content,
            },
        )
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                return {"result": result}
        return {"result": result}

    return StructuredTool.from_function(
        func=_invoke,
        name="internet_search",
        description=(
            "根据用户问题检索互联网公开信息（MCP stdio）。"
            "仅用于外部公开网页、新闻、政策等信息。"
        ),
    )


def build_mysql_mcp_tools(
    runtime: Optional[MCPServerRuntime] = None,
) -> list[StructuredTool]:
    client = runtime or MCPServerRuntime(MYSQL_SERVER_MODULE)

    def list_sql_tables() -> str:
        monitor.report_tool(
            tool_name="数据库表名查询工具：list_sql_tables",
            args={"transport": "mcp-stdio"},
        )
        return _as_text(client.call_tool_sync("list_sql_tables", {}))

    def get_table_data(table_name: str) -> str:
        monitor.report_tool(
            tool_name="数据库表数据查询工具：get_table_data",
            args={"table_name": table_name, "transport": "mcp-stdio"},
        )
        return _as_text(client.call_tool_sync("get_table_data", {"table_name": table_name}))

    def execute_sql_query(query: str) -> str:
        monitor.report_tool(
            tool_name="数据库表数据查询工具：execute_sql_query",
            args={"query": query, "transport": "mcp-stdio"},
        )
        return _as_text(client.call_tool_sync("execute_sql_query", {"query": query}))

    return [
        StructuredTool.from_function(
            func=list_sql_tables,
            name="list_sql_tables",
            description="查询当前 MySQL 数据库中所有可用表名（MCP stdio）。",
        ),
        StructuredTool.from_function(
            func=get_table_data,
            name="get_table_data",
            description="预览指定表的前 100 行数据，CSV 格式（MCP stdio）。",
        ),
        StructuredTool.from_function(
            func=execute_sql_query,
            name="execute_sql_query",
            description="执行自定义 SQL 查询并返回 CSV 格式结果（MCP stdio）。",
        ),
    ]


def build_ragflow_mcp_tools(
    runtime: Optional[MCPServerRuntime] = None,
) -> list[StructuredTool]:
    client = runtime or MCPServerRuntime(RAGFLOW_SERVER_MODULE)

    def get_assistant_list() -> str:
        monitor.report_tool(
            tool_name="ragflow聊天助手列表查询工具：get_assistant_list",
            args={"transport": "mcp-stdio"},
        )
        return _as_text(client.call_tool_sync("get_assistant_list", {}))

    def create_ask_delete(chat_name: str, question: str) -> str:
        monitor.report_tool(
            tool_name="ragflow提问助手工具：create_ask_delete",
            args={
                "chat_name": chat_name,
                "question": question,
                "transport": "mcp-stdio",
            },
        )
        return _as_text(
            client.call_tool_sync(
                "create_ask_delete",
                {"chat_name": chat_name, "question": question},
            )
        )

    return [
        StructuredTool.from_function(
            func=get_assistant_list,
            name="get_assistant_list",
            description="查询 RAGFlow 可用聊天助手及其关联知识库（MCP stdio）。",
        ),
        StructuredTool.from_function(
            func=create_ask_delete,
            name="create_ask_delete",
            description="向指定 RAGFlow 助手创建临时会话、提问并删除会话（MCP stdio）。",
        ),
    ]


# 向后兼容旧名称
TavilyMCPRuntime = MCPServerRuntime
