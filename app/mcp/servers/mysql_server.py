"""
MySQL MCP Server（stdio 传输）

通过 MCP 协议暴露 list_sql_tables / get_table_data / execute_sql_query。
启动：uv run python -m app.mcp.servers.mysql_server
"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

from app.tools import db_core

mcp = FastMCP("mysql-query")


@mcp.tool()
def list_sql_tables() -> str:
    """查询当前 MySQL 数据库中所有可用表名。"""
    return db_core.list_sql_tables()


@mcp.tool()
def get_table_data(table_name: str) -> str:
    """预览指定表的前 100 行数据（CSV 格式）。"""
    return db_core.get_table_data(table_name)


@mcp.tool()
def execute_sql_query(query: str) -> str:
    """执行自定义 SQL 查询并返回 CSV 格式结果。"""
    return db_core.execute_sql_query(query)


if __name__ == "__main__":
    print("[MySQLMCP] starting stdio server", file=sys.stderr)
    mcp.run(transport="stdio")
