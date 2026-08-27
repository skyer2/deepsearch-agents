"""
每个 MCP Server 只继承自己需要的环境变量，禁止 os.environ.copy()。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

_RUNTIME_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PYTHONPATH",
    "PYTHONHOME",
    "VIRTUAL_ENV",
    "PYTHONNOUSERSITE",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SYSTEMROOT",
    "COMSPEC",
    "USERPROFILE",
    "HARNESS_MCP_TASK_STORE",
    "HARNESS_MCP_AUDIT_STORE",
)

SERVER_ENV_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "tavily-mcp": ("TAVILY_API_KEY",),
    "mysql-mcp": (
        "MYSQL_HOST",
        "MYSQL_PORT",
        "MYSQL_USER",
        "MYSQL_PASSWORD",
        "MYSQL_DATABASE",
        "MYSQL_CHARSET",
        "MYSQL_COLLATION",
        "MYSQL_SQL_MODE",
        "MYSQL_READ_HOST",
    ),
    "ragflow-mcp": (
        "RAGFLOW_API_KEY",
        "RAGFLOW_BASE_URL",
        "RAGFLOW_ADDRESS",
        "RAGFLOW_TOKEN",
    ),
    "files-mcp": ("HARNESS_MCP_TASK_STORE",),
}

_MODULE_TO_SERVER = {
    "app.mcp.servers.tavily_server": "tavily-mcp",
    "app.mcp.servers.mysql_server": "mysql-mcp",
    "app.mcp.servers.ragflow_server": "ragflow-mcp",
    "app.mcp.servers.files_server": "files-mcp",
}


def server_id_for_module(server_module: str) -> str:
    return _MODULE_TO_SERVER.get(server_module, server_module)


def build_server_env(
    server_id: str,
    *,
    extra_allowlist: Optional[Iterable[str]] = None,
    environ: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """最小凭证集：runtime keys + 该 server 的 allowlist。"""
    source = environ if environ is not None else os.environ
    allowed = set(_RUNTIME_KEYS)
    allowed.update(SERVER_ENV_ALLOWLIST.get(server_id, ()))
    if extra_allowlist:
        allowed.update(extra_allowlist)
    env: dict[str, str] = {}
    for key in allowed:
        value = source.get(key)
        if value is not None and value != "":
            env[key] = value
    workspace = str(Path(__file__).resolve().parents[2])
    existing_pp = env.get("PYTHONPATH", "")
    parts = [workspace]
    if existing_pp:
        parts.append(existing_pp)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    if "HARNESS_MCP_TASK_STORE" not in env:
        from app.mcp.task_store import default_task_store_path

        env["HARNESS_MCP_TASK_STORE"] = str(default_task_store_path().resolve())
    if "HARNESS_MCP_AUDIT_STORE" not in env:
        from app.mcp.audit_store import default_audit_path

        env["HARNESS_MCP_AUDIT_STORE"] = str(default_audit_path().resolve())
    return env
