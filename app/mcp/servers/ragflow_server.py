"""
RAGFlow MCP Server（stdio 传输）

通过 MCP 协议暴露 get_assistant_list / create_ask_delete。
启动：uv run python -m app.mcp.servers.ragflow_server
"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

from app.tools import ragflow_core

mcp = FastMCP("ragflow-kb")


@mcp.tool()
def get_assistant_list() -> str:
    """查询 RAGFlow 可用聊天助手及其关联知识库。"""
    return ragflow_core.get_assistant_list()


@mcp.tool()
def create_ask_delete(chat_name: str, question: str) -> str:
    """向指定 RAGFlow 助手创建临时会话、提问并删除会话。"""
    return ragflow_core.create_ask_delete(chat_name, question)


if __name__ == "__main__":
    print("[RAGFlowMCP] starting stdio server", file=sys.stderr)
    mcp.run(transport="stdio")
