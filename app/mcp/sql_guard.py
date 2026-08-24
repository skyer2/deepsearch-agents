"""
【Phase 10】SQL 安全护栏 — LangChain 与 MCP Server 共用

企业生产：只允许 SELECT（含 WITH CTE），禁止 DDL/DML/多语句注入。
"""

from __future__ import annotations

import re
from typing import Tuple

# 去掉块注释与行注释后再校验
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"--[^\n]*")

_FORBIDDEN_KEYWORDS = frozenset(
    {
        "insert",
        "update",
        "delete",
        "drop",
        "truncate",
        "alter",
        "create",
        "replace",
        "grant",
        "revoke",
        "call",
        "exec",
        "execute",
        "load",
        "outfile",
        "dumpfile",
        "into",
        "lock",
        "unlock",
    }
)

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _normalize_sql(query: str) -> str:
    text = query.strip()
    text = _BLOCK_COMMENT.sub(" ", text)
    text = _LINE_COMMENT.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def validate_sql_identifier(name: str) -> Tuple[bool, str]:
    """表名/列名只允许简单标识符，防注入。"""
    if not name or not isinstance(name, str):
        return False, "invalid_identifier"
    cleaned = name.strip().strip("`").strip('"').strip("'")
    if not _IDENTIFIER_PATTERN.match(cleaned):
        return False, "invalid_identifier"
    return True, ""


def validate_select_only(query: str, *, enabled: bool = True) -> Tuple[bool, str]:
    """
    校验 SQL 是否只读。
    返回 (ok, error_code)。
    """
    if not enabled:
        return True, ""
    if not query or not str(query).strip():
        return False, "empty_sql"

    normalized = _normalize_sql(str(query))
    lower = normalized.lower()

    if ";" in normalized.rstrip(";"):
        return False, "multi_statement_denied"

    if not (lower.startswith("select") or lower.startswith("with")):
        return False, "select_only_denied"

    tokens = re.findall(r"[a-zA-Z_]+", lower)
    for token in tokens:
        if token in _FORBIDDEN_KEYWORDS:
            return False, f"forbidden_keyword:{token}"

    return True, ""
