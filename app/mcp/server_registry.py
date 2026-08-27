"""
Trusted MCP Server Registry — 禁止任意 URL 自动连接。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class TrustedMCPServer:
    server_id: str
    module: str = ""
    endpoint: str = ""
    transport: str = "stdio"  # stdio | streamable-http
    approved: bool = True
    owner: str = "harness"
    protocol_version: str = "2026-07-28"
    allowed_tools: list[str] = field(default_factory=list)
    auth_scheme: str = "oauth"
    risk: str = "medium"
    data_classification: str = "internal"
    env_allowlist: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_id": self.server_id,
            "module": self.module,
            "endpoint": self.endpoint,
            "transport": self.transport,
            "approved": self.approved,
            "owner": self.owner,
            "protocol_version": self.protocol_version,
            "allowed_tools": list(self.allowed_tools),
            "auth_scheme": self.auth_scheme,
            "risk": self.risk,
            "data_classification": self.data_classification,
        }


DEFAULT_TRUSTED_SERVERS: dict[str, TrustedMCPServer] = {
    "tavily-mcp": TrustedMCPServer(
        server_id="tavily-mcp",
        module="app.mcp.servers.tavily_server",
        allowed_tools=["internet_search"],
        risk="medium",
        env_allowlist=["TAVILY_API_KEY"],
    ),
    "mysql-mcp": TrustedMCPServer(
        server_id="mysql-mcp",
        module="app.mcp.servers.mysql_server",
        allowed_tools=["list_sql_tables", "get_table_data", "execute_sql_query"],
        risk="high",
        data_classification="confidential",
        env_allowlist=["MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE"],
    ),
    "ragflow-mcp": TrustedMCPServer(
        server_id="ragflow-mcp",
        module="app.mcp.servers.ragflow_server",
        allowed_tools=["get_assistant_list", "create_ask_delete"],
        risk="medium",
    ),
    "files-mcp": TrustedMCPServer(
        server_id="files-mcp",
        module="app.mcp.servers.files_server",
        allowed_tools=["read_file_content", "generate_markdown", "convert_md_to_pdf_async"],
        risk="medium",
    ),
}


class UntrustedMCPServerError(PermissionError):
    pass


class TrustedServerRegistry:
    def __init__(self, servers: Optional[dict[str, TrustedMCPServer]] = None):
        self._servers = dict(servers or DEFAULT_TRUSTED_SERVERS)

    def get(self, server_id: str) -> Optional[TrustedMCPServer]:
        return self._servers.get(server_id)

    def require(self, server_id: str) -> TrustedMCPServer:
        server = self.get(server_id)
        if server is None or not server.approved:
            raise UntrustedMCPServerError(f"untrusted_mcp_server:{server_id}")
        return server

    def require_approved(self, server_id: str) -> TrustedMCPServer:
        return self.require(server_id)

    def require_module(self, server_module: str) -> TrustedMCPServer:
        for server in self._servers.values():
            if server.module == server_module and server.approved:
                return server
        raise UntrustedMCPServerError(f"untrusted_mcp_module:{server_module}")

    def register(self, server: TrustedMCPServer) -> None:
        self._servers[server.server_id] = server

    def list_approved(self) -> list[TrustedMCPServer]:
        return [s for s in self._servers.values() if s.approved]


_registry: TrustedServerRegistry | None = None


def get_trusted_server_registry() -> TrustedServerRegistry:
    global _registry
    if _registry is None:
        _registry = TrustedServerRegistry()
        try:
            from app.config.loader import get_harness_config

            cfg = get_harness_config()
            extras = getattr(cfg, "mcp_trusted_servers", None) or {}
            for server_id, payload in dict(extras).items():
                if not isinstance(payload, dict):
                    continue
                current = _registry.get(str(server_id)) or TrustedMCPServer(server_id=str(server_id))
                for key, value in payload.items():
                    if hasattr(current, key):
                        setattr(current, key, value)
                _registry.register(current)
        except Exception:
            pass
    return _registry


def reset_trusted_server_registry() -> None:
    global _registry
    _registry = None


def get_trusted_mcp_registry() -> TrustedServerRegistry:
    return get_trusted_server_registry()
