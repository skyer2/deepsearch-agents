"""
ToolCallContext + PolicyEngine — task/principal 级授权，而不仅是 step_type。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Optional

from app.mcp.auth import MCPPrincipal, TokenError, validate_access_token
import app.mcp.registry as registry_mod


@dataclass
class ToolCallContext:
    tenant_id: str = "default"
    user_id: str = ""
    project_id: str = "default"
    session_id: str = ""
    run_id: str = ""
    task_id: str = ""
    step_type: str = ""
    tool_name: str = ""
    server_id: str = ""
    granted_scopes: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    data_classification: str = ""
    approval_state: str = ""
    access_token: str = ""
    ephemeral: bool = False
    idempotency_key: str = ""
    trace_id: str = ""

    def principal(self) -> MCPPrincipal:
        return MCPPrincipal(
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            scopes=list(self.granted_scopes),
            ephemeral=self.ephemeral,
        )


_ctx_var: ContextVar[Optional[ToolCallContext]] = ContextVar("tool_call_context", default=None)


def set_tool_call_context(ctx: ToolCallContext) -> Token:
    return _ctx_var.set(ctx)


def reset_tool_call_context(token: Token) -> None:
    _ctx_var.reset(token)


def get_tool_call_context() -> Optional[ToolCallContext]:
    return _ctx_var.get()


def current_tool_call_context(**overrides: Any) -> ToolCallContext:
    base = get_tool_call_context() or ToolCallContext()
    if not overrides:
        return base
    data = base.__dict__.copy()
    data.update({k: v for k, v in overrides.items() if v is not None})
    return ToolCallContext(**data)


@dataclass
class PolicyDecision:
    allowed: bool
    error_code: str = ""
    message: str = ""


class PolicyEngine:
    """
    principal scopes ∩ task allowed_tools ∩ tool permissions ∩ step policy。
    MCP Server 自己的 annotation 不作为安全事实。
    """

    def __init__(
        self,
        *,
        require_auth: bool = False,
        expected_audience: str = "https://mcp.local/gateway",
        expected_issuer: str = "deepsearch-harness",
    ) -> None:
        self.require_auth = require_auth
        self.expected_audience = expected_audience
        self.expected_issuer = expected_issuer

    def authorize(
        self,
        ctx: ToolCallContext,
        *,
        tool_name: str,
        step_type: str = "",
        require_auth: bool = False,
        expected_audience: str = "https://mcp.local/gateway",
        expected_issuer: str = "deepsearch-harness",
        arguments: Optional[dict[str, Any]] = None,
    ) -> PolicyDecision:
        from app.mcp.tool_gateway import get_tool_gateway

        require_auth = require_auth or self.require_auth
        expected_audience = expected_audience or self.expected_audience
        expected_issuer = expected_issuer or self.expected_issuer
        if require_auth:
            try:
                principal = validate_access_token(
                    ctx.access_token,
                    expected_audience=expected_audience,
                    expected_issuer=expected_issuer,
                )
            except TokenError as exc:
                return PolicyDecision(False, str(exc) or "invalid_access_token", "access token 校验失败")
            if principal.tenant_id != (ctx.tenant_id or principal.tenant_id):
                return PolicyDecision(False, "tenant_mismatch", "token tenant 与请求不一致")
            if principal.ephemeral:
                return PolicyDecision(False, "ephemeral_principal", "匿名身份不能调用 MCP")
            ctx.granted_scopes = ctx.granted_scopes or list(principal.scopes)
            ctx.user_id = ctx.user_id or principal.user_id
            ctx.tenant_id = ctx.tenant_id or principal.tenant_id

        if ctx.allowed_tools and tool_name not in ctx.allowed_tools:
            return PolicyDecision(False, "tool_not_in_task_allowlist", f"任务不允许工具 {tool_name}")

        effective_step = step_type or ctx.step_type
        if effective_step:
            step_check = get_tool_gateway().validate_tool_for_step(effective_step, tool_name)
            if not step_check.allowed:
                return PolicyDecision(False, step_check.error_code, step_check.message)

        desc = registry_mod.mcp_registry.get_descriptor(tool_name)
        required = list(desc.permissions) if desc else []
        if required and ctx.granted_scopes:
            if not set(required).intersection(ctx.granted_scopes):
                return PolicyDecision(
                    False,
                    "insufficient_scope",
                    f"需要 scopes {required}，当前 {ctx.granted_scopes}",
                )

        arguments = arguments or {}
        uri = str(arguments.get("uri") or arguments.get("resource") or "")
        if uri.startswith("session://"):
            rest = uri.removeprefix("session://")
            session_id = rest.split("/", 1)[0]
            if ctx.session_id and session_id and session_id != ctx.session_id:
                return PolicyDecision(False, "resource_acl_denied", "无权访问其他 session 的 Resource")
        return PolicyDecision(True)
