"""
MCP Gateway — 身份、策略、限流、熔断、重试分类、审计；Agent 不直连 Server。

caller 必须提交 access token；Gateway 不再用进程自己的 env 校验自己。
MCP token 不得 passthrough 给下游 MySQL/Tavily。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Optional

from app.mcp.audit_store import ToolCallAudit, get_mcp_audit_store
from app.mcp.auth import TokenError, validate_access_token
from app.mcp.circuit_breaker import CircuitOpenError, get_mcp_circuit_breaker
from app.mcp.http_transport import call_http_tool
from app.mcp.policy_context import (
    PolicyEngine,
    ToolCallContext,
    current_tool_call_context,
    get_tool_call_context,
)
from app.mcp.resource_acl import authorize_session_uri
from app.mcp.retry_policy import should_retry, sleep_seconds
from app.mcp.server_env import server_id_for_module
from app.mcp.server_registry import UntrustedMCPServerError, get_trusted_mcp_registry
from app.mcp.session_pool import MCPSessionPool, use_session_pool
from app.mcp.tool_gateway import ToolGateway, get_tool_gateway


class MCPGatewayError(RuntimeError):
    pass


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
    """MCP 调用网关：真实 caller token + 任务策略 + 熔断 + 分类重试 + 耐久审计。"""

    def __init__(
        self,
        *,
        rate_limit_per_minute: int = 120,
        oauth_token: str = "",
        tool_gateway: Optional[ToolGateway] = None,
        require_auth: bool = False,
        transport: str = "stdio",
        max_retries: int = 1,
        expected_audience: str = "https://mcp.local/gateway",
        expected_issuer: str = "deepsearch-harness",
    ):
        self.rate_limit_per_minute = max(1, rate_limit_per_minute)
        # 仅作 HMAC secret 回退，不再当作 caller identity
        self.oauth_token = (oauth_token or "").strip()
        if self.oauth_token and not os.getenv("HARNESS_MCP_TOKEN_SECRET"):
            os.environ["HARNESS_MCP_TOKEN_SECRET"] = self.oauth_token
        self.tool_gateway = tool_gateway or get_tool_gateway()
        self.require_auth = require_auth
        self.transport = (transport or "stdio").strip().lower()
        self.max_retries = max(0, int(max_retries))
        self.expected_audience = expected_audience
        self.expected_issuer = expected_issuer
        self.policy = PolicyEngine(
            require_auth=require_auth,
            expected_audience=expected_audience,
            expected_issuer=expected_issuer,
        )
        self._audit: deque[MCPAuditEntry] = deque(maxlen=64)
        self._call_times: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def authorize(self, agent_id: str, tool_name: str) -> tuple[bool, str]:
        """兼容旧测试签名。真正身份来自 caller token / ToolCallContext，不是进程 env。"""
        ctx = get_tool_call_context()
        token = (ctx.access_token if ctx else "") or ""
        if self.require_auth or token:
            try:
                principal = validate_access_token(
                    token,
                    expected_audience=self.expected_audience,
                    expected_issuer=self.expected_issuer,
                )
            except TokenError as exc:
                return False, str(exc) or "invalid_access_token"
            if principal.ephemeral:
                return False, "ephemeral_principal"
            if ctx and ctx.tenant_id and principal.tenant_id != ctx.tenant_id:
                return False, "tenant_mismatch"
        if not agent_id:
            agent_id = "anonymous"
        return True, ""

    def _check_rate_limit(self, tool_name: str, *, tenant_id: str = "") -> tuple[bool, str]:
        now = time.time()
        window_start = now - 60.0
        key = f"{tenant_id}:{tool_name}" if tenant_id else tool_name
        with self._lock:
            bucket = self._call_times[key]
            while bucket and bucket[0] < window_start:
                bucket.popleft()
            if len(bucket) >= self.rate_limit_per_minute:
                return False, "rate_limit_exceeded"
            bucket.append(now)
        return True, ""

    def _audit_log(self, entry: MCPAuditEntry, *, ctx: Optional[ToolCallContext] = None) -> None:
        with self._lock:
            self._audit.append(entry)
        ctx = ctx or get_tool_call_context()
        get_mcp_audit_store().record(
            ToolCallAudit(
                timestamp=entry.timestamp,
                trace_id=(ctx.trace_id if ctx else ""),
                run_id=(ctx.run_id if ctx else ""),
                task_id=(ctx.task_id if ctx else ""),
                tenant_id=(ctx.tenant_id if ctx else ""),
                user_id=(ctx.user_id if ctx else entry.agent_id),
                server_id=server_id_for_module(entry.server_module),
                tool_name=entry.tool_name,
                status="ok" if entry.allowed and not entry.error else "error",
                latency_ms=entry.latency_ms,
                error=entry.error,
                allowed=entry.allowed,
                transport=entry.transport,
            )
        )

    def list_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        durable = get_mcp_audit_store().list_entries(limit=limit)
        if durable:
            return [
                {
                    "timestamp": item["timestamp"],
                    "agent_id": item.get("user_id") or "",
                    "server_module": item.get("server_id") or "",
                    "tool_name": item["tool_name"],
                    "allowed": item["allowed"],
                    "latency_ms": item["latency_ms"],
                    "error": item["error"],
                    "transport": item["transport"],
                }
                for item in durable
            ]
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
        max_retries: Optional[int] = None,
        ctx: Optional[ToolCallContext] = None,
        idempotency_key: str = "",
    ) -> Any:
        started = time.perf_counter()
        ctx = ctx or current_tool_call_context(
            tool_name=tool_name,
            server_id=server_id_for_module(server_module),
            step_type=step_type,
        )
        if not ctx.tool_name:
            ctx.tool_name = tool_name
        ctx.server_id = ctx.server_id or server_id_for_module(server_module)
        ctx.step_type = ctx.step_type or step_type
        if idempotency_key:
            ctx.idempotency_key = idempotency_key

        ok, reason = self.authorize(agent_id, tool_name)
        if not ok:
            self._audit_log(
                MCPAuditEntry(time.time(), agent_id, server_module, tool_name, False, error=reason),
                ctx=ctx,
            )
            raise PermissionError(reason)

        decision = self.policy.authorize(
            ctx,
            tool_name=tool_name,
            step_type=step_type,
            arguments=arguments,
        )
        if not decision.allowed:
            self._audit_log(
                MCPAuditEntry(
                    time.time(),
                    agent_id,
                    server_module,
                    tool_name,
                    False,
                    error=decision.error_code,
                ),
                ctx=ctx,
            )
            return {
                "ok": False,
                "error_code": decision.error_code,
                "message": decision.message,
                "source": "mcp_gateway",
            }

        rl_ok, rl_reason = self._check_rate_limit(tool_name, tenant_id=ctx.tenant_id)
        if not rl_ok:
            self._audit_log(
                MCPAuditEntry(time.time(), agent_id, server_module, tool_name, False, error=rl_reason),
                ctx=ctx,
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
                    ),
                    ctx=ctx,
                )
                return json.loads(policy.to_denial_text())

        try:
            get_trusted_mcp_registry().require_module(server_module)
        except UntrustedMCPServerError as exc:
            self._audit_log(
                MCPAuditEntry(time.time(), agent_id, server_module, tool_name, False, error=str(exc)),
                ctx=ctx,
            )
            raise PermissionError(str(exc)) from exc

        retries = self.max_retries if max_retries is None else max(0, int(max_retries))
        last_exc: Exception | None = None
        breaker = get_mcp_circuit_breaker()
        args = dict(arguments or {})
        if ctx.idempotency_key and "idempotency_key" not in args:
            args["idempotency_key"] = ctx.idempotency_key

        for attempt in range(retries + 1):
            try:
                breaker.before_call(ctx.server_id)
                result = self._invoke(
                    server_module,
                    tool_name,
                    args,
                    timeout_sec=timeout_sec,
                    access_token=ctx.access_token,
                )
                breaker.record_success(ctx.server_id)
                latency = int((time.perf_counter() - started) * 1000)
                self._audit_log(
                    MCPAuditEntry(
                        time.time(),
                        agent_id,
                        server_module,
                        tool_name,
                        True,
                        latency_ms=latency,
                        transport=self._transport_label(),
                    ),
                    ctx=ctx,
                )
                return result
            except CircuitOpenError as exc:
                last_exc = exc
                break
            except Exception as exc:
                last_exc = exc
                breaker.record_failure(ctx.server_id)
                if not should_retry(tool_name, attempt=attempt, max_retries=retries, exc=exc):
                    break
                time.sleep(sleep_seconds(attempt))

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
            ),
            ctx=ctx,
        )
        if last_exc:
            raise last_exc
        return ""

    def _transport_label(self) -> str:
        if self.transport in {"streamable-http", "http", "stateless-http"}:
            return "mcp-http"
        return "mcp-pool" if use_session_pool() else "mcp-stdio"

    def _invoke(
        self,
        server_module: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_sec: float,
        access_token: str = "",
    ) -> Any:
        if self.transport in {"streamable-http", "http", "stateless-http"}:
            rec = get_trusted_mcp_registry().get(server_id_for_module(server_module))
            if rec and rec.endpoint:
                return call_http_tool(
                    rec.endpoint,
                    tool_name,
                    arguments,
                    access_token=access_token,
                    timeout_sec=timeout_sec,
                )
        if use_session_pool():
            return MCPSessionPool.call_tool_sync(
                server_module,
                tool_name,
                arguments,
                timeout_sec=timeout_sec,
            )
        from app.mcp.mcp_runtime import MCPServerRuntime

        runtime = MCPServerRuntime(server_module)
        return runtime.call_tool_sync(tool_name, arguments)

    def read_resource(self, server_module: str, uri: str, *, agent_id: str = "harness") -> str:
        ok, reason = self.authorize(agent_id, f"resource:{uri}")
        if not ok:
            raise PermissionError(reason)
        ctx = current_tool_call_context()
        if uri.startswith("session://"):
            authorize_session_uri(uri, ctx)
        if use_session_pool():
            return MCPSessionPool.read_resource_sync(server_module, uri)
        raise RuntimeError("MCP resources require mcp_pool_enabled=true")


_gateway: MCPGateway | None = None


def get_mcp_gateway() -> MCPGateway:
    global _gateway
    if _gateway is None:
        from app.config.loader import get_harness_config

        cfg = get_harness_config()
        secret = os.getenv("HARNESS_MCP_TOKEN_SECRET", "").strip() or cfg.mcp_gateway_oauth_token
        _gateway = MCPGateway(
            rate_limit_per_minute=cfg.mcp_gateway_rate_limit_per_minute,
            oauth_token=secret,
            require_auth=bool(getattr(cfg, "mcp_require_auth", False)),
            transport=str(getattr(cfg, "mcp_transport", "stdio") or "stdio"),
            max_retries=int(getattr(cfg, "mcp_max_retries", 1) or 1),
            expected_audience=str(
                getattr(cfg, "mcp_oauth_audience", "https://mcp.local/gateway")
                or "https://mcp.local/gateway"
            ),
            expected_issuer=str(
                getattr(cfg, "mcp_oauth_issuer", "deepsearch-harness") or "deepsearch-harness"
            ),
        )
    return _gateway


def reset_mcp_gateway() -> None:
    global _gateway
    _gateway = None
