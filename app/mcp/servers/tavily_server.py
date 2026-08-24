"""
Tavily MCP Server（stdio 传输）

最小真 MCP 实现：通过 MCP 协议暴露 internet_search 工具。
启动：uv run python -m app.mcp.servers.tavily_server
"""

from __future__ import annotations

import json
import sys
from typing import Literal

from mcp.server.fastmcp import FastMCP

from app.tools.tavily_core import search_internet

mcp = FastMCP("tavily-search")


@mcp.tool()
def internet_search(
    query: str,
    topic: Literal["news", "finance", "general"] = "general",
    max_results: int = 5,
    include_raw_content: bool = False,
) -> str:
    """
    根据用户问题检索互联网公开信息。

    仅用于外部公开网页、新闻、政策等信息，不用于业务数据库或私有知识库。
    """
    result = search_internet(
        query=query,
        topic=topic,
        max_results=max_results,
        include_raw_content=include_raw_content,
    )
    return json.dumps(result, ensure_ascii=False)


if __name__ == "__main__":
    # stdio 模式下日志必须走 stderr，避免污染 JSON-RPC 通道
    print("[TavilyMCP] starting stdio server", file=sys.stderr)
    mcp.run(transport="stdio")
