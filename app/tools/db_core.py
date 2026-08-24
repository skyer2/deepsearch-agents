"""
MySQL 查询核心逻辑

供 LangChain @tool 与 MCP Server 共用，避免重复实现。
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from mysql.connector import Error, connect

load_dotenv()


def get_db_config() -> dict:
    """从环境变量读取 MySQL 连接配置。"""
    config = {
        "host": os.getenv("MYSQL_HOST", "localhost"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER"),
        "password": os.getenv("MYSQL_PASSWORD"),
        "database": os.getenv("MYSQL_DATABASE"),
        "charset": os.getenv("MYSQL_CHARSET", "utf8mb4"),
        "collation": os.getenv("MYSQL_COLLATION", "utf8mb4_unicode_ci"),
        "autocommit": True,
        "sql_mode": os.getenv("MYSQL_SQL_MODE", "TRADITIONAL"),
    }
    config = {k: v for k, v in config.items() if v is not None}

    required_keys = ["user", "password", "database"]
    missing_keys = [k for k in required_keys if k not in config]
    if missing_keys:
        raise ValueError(f"缺失数据库核心配置：{', '.join(missing_keys)}")

    return config


def list_sql_tables() -> str:
    """查询当前数据库中所有可用表。"""
    config = get_db_config()
    try:
        with connect(**config) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SHOW TABLES")
                tables = cursor.fetchall()
                if not tables:
                    return "没有可用的表"
                table_names = [table[0] for table in tables]
                return f"可用的表有：{', '.join(table_names)}"
    except Error as e:
        return f"查询出现异常：{str(e)}"


def get_table_data(table_name: str) -> str:
    """预览指定表的前 100 行数据（CSV 格式）。"""
    from app.mcp.tool_gateway import get_tool_gateway

    gate = get_tool_gateway()
    check = gate.validate_table_name(table_name)
    if not check.allowed:
        return check.to_denial_text()

    safe_table = table_name.strip().strip("`").strip('"').strip("'")
    config = get_db_config()
    try:
        with connect(**config) as conn:
            with conn.cursor() as cursor:
                sql = f"SELECT * FROM `{safe_table}` LIMIT 100"
                cursor.execute(sql)
                description = cursor.description
                if not description:
                    return f"数据表 {table_name} 暂无数据。"
                columns = [desc[0] for desc in description]
                rows = cursor.fetchall()
                results = [",".join(map(str, row)) for row in rows]
                header_str = ",".join(columns)
                data_str = "\n".join(results)
                return f"{header_str}\n{data_str}"
    except Error as e:
        return f"查询出现异常：{str(e)}"


def execute_sql_query(query: str) -> str:
    """执行自定义 SQL 查询并返回 CSV 格式结果。"""
    from app.mcp.tool_gateway import get_tool_gateway

    gate = get_tool_gateway()
    check = gate.validate_sql(query)
    if not check.allowed:
        return check.to_denial_text()

    config = get_db_config()
    try:
        with connect(**config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(query)
                description = cursor.description
                if not description:
                    return f"执行自定义 SQL 语句没有查询结果，SQL 为：{query}"
                columns = [desc[0] for desc in description]
                rows = cursor.fetchall()
                results = [",".join(map(str, row)) for row in rows]
                header_str = ",".join(columns)
                data_str = "\n".join(results)
                return f"{header_str}\n{data_str}"
    except Error as e:
        return f"查询出现异常：{str(e)}"
