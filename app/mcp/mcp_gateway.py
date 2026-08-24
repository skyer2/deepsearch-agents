"""
【Phase 16】MCP Gateway — 统一鉴权、限流、审计；Agent 不直连 Server。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Optional

from app.mcp.session_pool import MCPSessionPool, use_session_pool
from app.mcp.tool_gateway import ToolGateway, get_tool_gateway


@dataclass
class MCPAuditEntry:
    timestamp: float
    agent_id: str
    server_module: str
    tool_name: str
    allowed: bool
    latency_ms: int = 0
    error: str = ""
    transport: str = "mcp-stdio"


class MCPGateway:
    """MCP 调用网关：OAuth 令牌校验 + 速率限制 + 审计 + 连接池转发。"""

    def __init__(
        self,
        *,
        rate_limit_per_minute: int = 120,
        oauth_token: str = "",
        tool_gateway: Optional[ToolGateway] = None,
    ):
        self.rate_limit_per_minute = max(1, rate_limit_per_minute)
        self.oauth_token = oauth_token.strip()
        self.tool_gateway = tool_gateway or get_tool_gateway()
        self._audit: deque[MCPAuditEntry] = deque(maxlen=500)
        self._call_times: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def authorize(self, agent_id: str, tool_name: str) -> tuple[bool, str]:
        if self.oauth_token:
            provided = os.getenv("HARNESS_MCP_GATEWAY_TOKEN", "").strip()
            if provided != self.oauth_token:
                return False, "invalid_mcp_gateway_token"
        if not agent_id:
            agent_id = "anonymous"
        return True, ""

    def _check_rate_limit(self, tool_name: str) -> tuple[bool, str]:
        now = time.time()
        window_start = now - 60.0
        with self._lock:
            bucket = self._call_times[tool_name]
            while bucket and bucket[0] < window_start:
                bucket.popleft()
            if len(bucket) >= self.rate_limit_per_minute:
                return False, "rate_limit_exceeded"
            bucket.append(now)
        return True, ""

    def _audit_log(self, entry: MCPAuditEntry) -> None:
        with self._lock:
            self._audit.append(entry)

    def list_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._audit)[-limit:]
        return [
            {
                "timestamp": e.timestamp,
                "agent_id": e.agent_id,
                "server_module": e.server_module,
                "tool_name": e.tool_name,
                "allowed": e.allowed,
                "latency_ms": e.latency_ms,
                "error": e.error,
                "transport": e.transport,
            }
            for e in items
        ]

    def call_tool(
        self,
        server_module: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        agent_id: str = "harness",
        step_type: str = "",
        timeout_sec: float = 30.0,
        max_retries: int = 1,
    ) -> Any:
        started = time.perf_counter()
        ok, reason = self.authorize(agent_id, tool_name)
        if not ok:
            self._audit_log(
                MCPAuditEntry(
                    time.time(), agent_id, server_module, tool_name, False, error=reason
                )
            )
            raise PermissionError(reason)

        rl_ok, rl_reason = self._check_rate_limit(tool_name)
        if not rl_ok:
            self._audit_log(
                MCPAuditEntry(
                    time.time(), agent_id, server_module, tool_name, False, error=rl_reason
                )
            )
            raise RuntimeError(rl_reason)

        if step_type:
            policy = self.tool_gateway.validate_tool_for_step(step_type, tool_name)
            if not policy.allowed:
                self._audit_log(
                    MCPAuditEntry(
                        time.time(),
                        agent_id,
                        server_module,
                        tool_name,
                        False,
                        error=policy.error_code,
                    )
                )
                return json.loads(policy.to_denial_text())

        last_exc: Exception | None = None
        for attempt in range(max(1, max_retries + 1)):
            try:
                if use_session_pool():
                    result = MCPSessionPool.call_tool_sync(
                        server_module,
                        tool_name,
                        arguments,
                        timeout_sec=timeout_sec,
                    )
                else:
                    from app.mcp.mcp_runtime import MCPServerRuntime

                    runtime = MCPServerRuntime(server_module)
                    result = runtime.call_tool_sync(tool_name, arguments)

                latency = int((time.perf_counter() - started) * 1000)
                self._audit_log(
                    MCPAuditEntry(
                        time.time(),
                        agent_id,
                        server_module,
                        tool_name,
                        True,
                        latency_ms=latency,
                        transport="mcp-pool" if use_session_pool() else "mcp-stdio",
                    )
                )
                return result
            except Exception as exc:
                last_exc = exc
                if attempt >= max_retries:
                    break
        latency = int((time.perf_counter() - started) * 1000)
        self._audit_log(
            MCPAuditEntry(
                time.time(),
                agent_id,
                server_module,
                tool_name,
                False,
                latency_ms=latency,
                error=str(last_exc),
            )
        )
        if last_exc:
            raise last_exc
        return ""

    def read_resource(self, server_module: str, uri: str, *, agent_id: str = "harness") -> str:
        ok, reason = self.authorize(agent_id, f"resource:{uri}")
        if not ok:
            raise PermissionError(reason)
        if use_session_pool():
            return MCPSessionPool.read_resource_sync(server_module, uri)
        raise RuntimeError("MCP resources require mcp_pool_enabled=true")


_gateway: MCPGateway | None = None


def get_mcp_gateway() -> MCPGateway:
    global _gateway
    if _gateway is None:
        from app.config.loader import get_harness_config

        cfg = get_harness_config()
        token = os.getenv("HARNESS_MCP_GATEWAY_TOKEN", cfg.mcp_gateway_oauth_token)
        _gateway = MCPGateway(
            rate_limit_per_minute=cfg.mcp_gateway_rate_limit_per_minute,
            oauth_token=token,
        )
    return _gateway


def reset_mcp_gateway() -> None:
    global _gateway
    _gateway = None
