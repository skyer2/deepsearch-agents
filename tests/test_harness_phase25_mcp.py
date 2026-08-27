"""Phase 25: MCP Capability Plane — 真身份、隔离 env、durable Tasks、DB guard。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config.loader import reload_harness_config
from app.mcp.auth import MCPPrincipal, TokenError, issue_access_token, validate_access_token
from app.mcp.circuit_breaker import CircuitBreaker, CircuitOpenError
from app.mcp.http_transport import call_http_tool
from app.mcp.mcp_gateway import MCPGateway, reset_mcp_gateway
from app.mcp.policy_context import (
    PolicyEngine,
    ToolCallContext,
    reset_tool_call_context,
    set_tool_call_context,
)
from app.mcp.resource_acl import authorize_session_uri
from app.mcp.result_normalizer import normalize_mcp_result
from app.mcp.retry_policy import SideEffectClass, should_retry, side_effect_for_tool
from app.mcp.schema_adapter import args_schema_from_input_schema
from app.mcp.server_env import build_server_env
from app.mcp.server_registry import (
    TrustedMCPServer,
    UntrustedMCPServerError,
    get_trusted_mcp_registry,
    reset_trusted_server_registry,
)
from app.mcp.sql_guard import cap_select_limit, validate_table_allowlist
from app.mcp.task_store import DurableTaskStore, MCPTaskManager


def test_real_token_not_self_env():
    os.environ["HARNESS_MCP_GATEWAY_TOKEN"] = "secret"
    gw = MCPGateway(require_auth=True, oauth_token="secret")
    ok, code = gw.authorize("agent", "internet_search")
    assert not ok, "matching the process env against itself must not authenticate"
    assert code == "missing_access_token"

    token = issue_access_token(MCPPrincipal(user_id="alice", tenant_id="t1", scopes=["read"]))
    principal = validate_access_token(token)
    assert principal.user_id == "alice"
    assert principal.audience == "https://mcp.local/gateway"

    ctx = set_tool_call_context(ToolCallContext(access_token=token, tenant_id="t1", user_id="alice"))
    try:
        ok2, _ = gw.authorize("agent", "internet_search")
        assert ok2
    finally:
        reset_tool_call_context(ctx)
        os.environ.pop("HARNESS_MCP_GATEWAY_TOKEN", None)
    print("[OK] real caller token")


def test_token_audience_and_expiry():
    token = issue_access_token(
        MCPPrincipal(user_id="bob", tenant_id="t1"),
        audience="https://mcp.company.com/db",
        ttl_sec=60,
    )
    try:
        validate_access_token(token)
        assert False, "wrong audience must fail"
    except TokenError as exc:
        assert "invalid_audience" in str(exc)
    print("[OK] audience check")


def test_server_env_allowlist():
    env = build_server_env(
        "tavily-mcp",
        environ={
            "TAVILY_API_KEY": "tvly-xxx",
            "MYSQL_PASSWORD": "super-secret",
            "RAGFLOW_TOKEN": "rf-secret",
            "HARNESS_MCP_GATEWAY_TOKEN": "gw-secret",
            "PATH": "/usr/bin",
        },
    )
    assert env["TAVILY_API_KEY"] == "tvly-xxx"
    assert "MYSQL_PASSWORD" not in env
    assert "RAGFLOW_TOKEN" not in env
    assert "HARNESS_MCP_GATEWAY_TOKEN" not in env
    mysql_env = build_server_env(
        "mysql-mcp",
        environ={"MYSQL_PASSWORD": "dbpass", "TAVILY_API_KEY": "tvly-xxx", "PATH": "/usr/bin"},
    )
    assert mysql_env["MYSQL_PASSWORD"] == "dbpass"
    assert "TAVILY_API_KEY" not in mysql_env
    print("[OK] server env isolation")


def test_durable_task_cross_store():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tasks.db"
        store_a = DurableTaskStore(path)
        mgr_a = MCPTaskManager(store_a)
        task_id = mgr_a.submit(
            server_module="files",
            tool_name="convert_md_to_pdf_async",
            runner=lambda: {"pdf": "ok"},
        )
        store_b = DurableTaskStore(path)
        mgr_b = MCPTaskManager(store_b)
        rec = mgr_b.wait(task_id, timeout_sec=5)
        assert rec.status.value == "done"
        assert rec.result == {"pdf": "ok"}
        handle = rec.to_handle()
        assert handle["task_id"] == task_id
        assert handle["kind"] == "mcp.task"
    print("[OK] durable tasks cross-process store")


def test_sql_limit_and_allowlist():
    capped = cap_select_limit("SELECT * FROM huge_table", 200)
    assert capped.lower().endswith("limit 200")
    capped2 = cap_select_limit("SELECT * FROM huge_table LIMIT 999999", 200)
    assert "LIMIT 200" in capped2
    ok, _ = validate_table_allowlist("SELECT * FROM orders", [])
    assert ok
    bad, code = validate_table_allowlist("SELECT * FROM secrets", ["orders"])
    assert not bad and code.startswith("table_not_allowed")
    print("[OK] sql production guard")


def test_circuit_breaker():
    br = CircuitBreaker(failure_threshold=2, reset_sec=60)
    br.record_failure("tavily-mcp")
    br.record_failure("tavily-mcp")
    try:
        br.before_call("tavily-mcp")
        assert False, "open breaker must deny"
    except CircuitOpenError:
        pass
    br.record_success("tavily-mcp")
    br.before_call("tavily-mcp")
    print("[OK] circuit breaker")


def test_retry_taxonomy():
    assert side_effect_for_tool("internet_search") == SideEffectClass.READ_ONLY
    assert side_effect_for_tool("create_ask_delete") == SideEffectClass.NON_IDEMPOTENT
    assert should_retry("internet_search", attempt=0, max_retries=1, exc=TimeoutError("x"))
    assert not should_retry("create_ask_delete", attempt=0, max_retries=3, exc=TimeoutError("x"))
    print("[OK] retry taxonomy")


def test_untrusted_server():
    registry = get_trusted_mcp_registry()
    try:
        registry.require("evil-mcp")
        assert False
    except UntrustedMCPServerError:
        pass
    registry.register(
        TrustedMCPServer(server_id="shadow-mcp", approved=False, module="evil.mod")
    )
    try:
        registry.require("shadow-mcp")
        assert False
    except UntrustedMCPServerError:
        pass
    reset_trusted_server_registry()
    print("[OK] trusted server registry")


def test_policy_task_allowlist():
    engine = PolicyEngine(require_auth=False)
    denied = engine.authorize(
        ToolCallContext(allowed_tools=["internet_search"]),
        tool_name="execute_sql_query",
    )
    assert not denied.allowed
    assert denied.error_code == "tool_not_in_task_allowlist"
    print("[OK] task-scoped allowlist")


def test_resource_acl():
    ctx = ToolCallContext(session_id="sess-a", tenant_id="t1", user_id="u1")
    authorize_session_uri("session://sess-a/report.md", ctx)
    try:
        authorize_session_uri("session://sess-b/private.md", ctx)
        assert False
    except PermissionError:
        pass
    print("[OK] resource ACL")


def test_result_normalizer_structured():
    result = {
        "structuredContent": {"hits": 2},
        "content": [
            {"type": "text", "text": "ignored because structured"},
            {"type": "resource_link", "uri": "artifact://run1/web/2"},
        ],
    }
    normalized = normalize_mcp_result(result)
    assert normalized.structured_content == {"hits": 2}
    assert "artifact://run1/web/2" in normalized.resource_links
    assert normalized.model_visible() == {"hits": 2}
    print("[OK] MCP result normalizer")


def test_schema_adapter():
    model = args_schema_from_input_schema(
        "search_crm_accounts",
        {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "name"}},
            "required": ["query"],
        },
    )
    assert model is not None
    inst = model(query="acme")
    assert inst.query == "acme"
    print("[OK] schema adapter")


def test_stateless_http_client():
    captured = {}

    class FakeResp:
        def read(self):
            return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}).encode()

    def opener(req, timeout=30):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization") or req.headers.get("Authorization")
        return FakeResp()

    out = call_http_tool(
        "https://mcp.example/db",
        "execute_sql_query",
        {"query": "SELECT 1"},
        access_token="mcp.abc.sig",
        opener=opener,
    )
    assert out == {"ok": True}
    assert captured["url"].endswith("/mcp")
    assert captured["auth"] == "Bearer mcp.abc.sig"
    print("[OK] stateless HTTP MCP")


def test_config_phase25():
    cfg = reload_harness_config()
    assert cfg.mcp_pool_size >= 2
    assert cfg.mcp_require_auth is False
    assert cfg.tools_sql_max_rows == 200
    assert cfg.mcp_oauth_audience
    print("[OK] config phase25")


if __name__ == "__main__":
    test_real_token_not_self_env()
    test_token_audience_and_expiry()
    test_server_env_allowlist()
    test_durable_task_cross_store()
    test_sql_limit_and_allowlist()
    test_circuit_breaker()
    test_retry_taxonomy()
    test_untrusted_server()
    test_policy_task_allowlist()
    test_resource_acl()
    test_result_normalizer_structured()
    test_schema_adapter()
    test_stateless_http_client()
    test_config_phase25()
    print("\n=== Phase 25 MCP capability plane tests passed ===")
