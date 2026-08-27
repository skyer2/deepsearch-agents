"""
MySQL 查询核心逻辑

供 LangChain @tool 与 MCP Server 共用，避免重复实现。

SQL syntax safety ≠ data access safety：SELECT-only 之外还要
read replica、table allowlist、server-side LIMIT、max bytes、statement timeout。
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from mysql.connector import Error, connect

from app.mcp.sql_guard import cap_select_limit, validate_table_allowlist

load_dotenv()


def get_db_config() -> dict:
    """从环境变量读取 MySQL 连接配置；优先只读副本。"""
    host = os.getenv("MYSQL_READ_HOST") or os.getenv("MYSQL_HOST", "localhost")
    config = {
        "host": host,
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


def _sql_limits() -> tuple[int, int, int, list[str]]:
    try:
        from app.config.loader import get_harness_config

        cfg = get_harness_config()
        rows = int(getattr(cfg, "tools_sql_max_rows", 200) or 200)
        nbytes = int(getattr(cfg, "tools_sql_max_bytes", 262144) or 262144)
        timeout_ms = int(getattr(cfg, "tools_sql_timeout_ms", 5000) or 5000)
        allowlist = [str(x) for x in (getattr(cfg, "tools_sql_table_allowlist", None) or [])]
        return rows, nbytes, timeout_ms, allowlist
    except Exception:
        return 200, 262144, 5000, []


def _apply_timeout(cursor, timeout_ms: int) -> None:
    if timeout_ms <= 0:
        return
    try:
        cursor.execute(f"SET SESSION MAX_EXECUTION_TIME = {int(timeout_ms)}")
    except Exception:
        pass


def _fetch_capped(cursor, *, max_rows: int, max_bytes: int) -> tuple[list[str], bool]:
    rows: list[str] = []
    total = 0
    truncated = False
    while len(rows) < max_rows:
        batch = cursor.fetchmany(min(50, max_rows - len(rows)))
        if not batch:
            break
        for row in batch:
            line = ",".join(map(str, row))
            total += len(line.encode("utf-8")) + 1
            if total > max_bytes:
                truncated = True
                break
            rows.append(line)
            if len(rows) >= max_rows:
                truncated = True
                break
        if truncated:
            break
    return rows, truncated


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
    """预览指定表的前 N 行数据（CSV 格式）。"""
    from app.mcp.tool_gateway import get_tool_gateway

    gate = get_tool_gateway()
    check = gate.validate_table_name(table_name)
    if not check.allowed:
        return check.to_denial_text()

    max_rows, max_bytes, timeout_ms, allowlist = _sql_limits()
    safe_table = table_name.strip().strip("`").strip('"').strip("'")
    ok, code = validate_table_allowlist(f"SELECT * FROM `{safe_table}`", allowlist)
    if not ok:
        from app.mcp.tool_gateway import ToolValidationResult

        return ToolValidationResult(
            allowed=False, error_code=code, message=f"SQL 调用被拒绝: {code}"
        ).to_denial_text()

    config = get_db_config()
    try:
        with connect(**config) as conn:
            with conn.cursor() as cursor:
                _apply_timeout(cursor, timeout_ms)
                sql = f"SELECT * FROM `{safe_table}` LIMIT {max_rows}"
                cursor.execute(sql)
                description = cursor.description
                if not description:
                    return f"数据表 {table_name} 暂无数据。"
                columns = [desc[0] for desc in description]
                rows, truncated = _fetch_capped(cursor, max_rows=max_rows, max_bytes=max_bytes)
                header_str = ",".join(columns)
                data_str = "\n".join(rows)
                suffix = "\n# truncated=true" if truncated else ""
                return f"{header_str}\n{data_str}{suffix}"
    except Error as e:
        return f"查询出现异常：{str(e)}"


def execute_sql_query(query: str) -> str:
    """执行自定义 SQL 查询并返回 CSV 格式结果。"""
    from app.mcp.tool_gateway import get_tool_gateway

    gate = get_tool_gateway()
    check = gate.validate_sql(query)
    if not check.allowed:
        return check.to_denial_text()

    max_rows, max_bytes, timeout_ms, allowlist = _sql_limits()
    ok, code = validate_table_allowlist(query, allowlist)
    if not ok:
        from app.mcp.tool_gateway import ToolValidationResult

        return ToolValidationResult(
            allowed=False, error_code=code, message=f"SQL 调用被拒绝: {code}"
        ).to_denial_text()

    capped = cap_select_limit(query, max_rows)
    config = get_db_config()
    try:
        with connect(**config) as conn:
            with conn.cursor() as cursor:
                _apply_timeout(cursor, timeout_ms)
                cursor.execute(capped)
                description = cursor.description
                if not description:
                    return f"执行自定义 SQL 语句没有查询结果，SQL 为：{query}"
                columns = [desc[0] for desc in description]
                rows, truncated = _fetch_capped(cursor, max_rows=max_rows, max_bytes=max_bytes)
                header_str = ",".join(columns)
                data_str = "\n".join(rows)
                suffix = "\n# truncated=true" if truncated else ""
                return f"{header_str}\n{data_str}{suffix}"
    except Error as e:
        return f"查询出现异常：{str(e)}"
