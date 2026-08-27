"""Phase 16: MCP 生产化 — Gateway / Pool / Registry Sync / Tasks。"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config.loader import reload_harness_config
from app.mcp.auth import MCPPrincipal, issue_access_token
from app.mcp.client import bootstrap_mcp_registry
from app.mcp.mcp_gateway import MCPGateway, reset_mcp_gateway
from app.mcp.mcp_tasks import get_mcp_task_manager
from app.mcp.policy_context import ToolCallContext, reset_tool_call_context, set_tool_call_context
from app.mcp.registry import MCPRegistry
import app.mcp.registry as registry_mod


def test_mcp_gateway_rate_limit():
    reset_mcp_gateway()
    gw = MCPGateway(rate_limit_per_minute=2, oauth_token="")
    gw.call_tool = lambda *a, **k: "ok"  # type: ignore[method-assign]
    gw._check_rate_limit("internet_search")
    gw._check_rate_limit("internet_search")
    ok, code = gw._check_rate_limit("internet_search")
    assert not ok and code == "rate_limit_exceeded"
    print("[OK] mcp gateway rate limit")


def test_mcp_gateway_oauth():
    reset_mcp_gateway()
    gw = MCPGateway(require_auth=True)
    ok, code = gw.authorize("agent", "internet_search")
    assert not ok
    assert code in {"missing_access_token", "malformed_access_token"}

    token = issue_access_token(
        MCPPrincipal(user_id="u1", tenant_id="acme", scopes=["read", "search"])
    )
    ctx_token = set_tool_call_context(
        ToolCallContext(
            access_token=token,
            user_id="u1",
            tenant_id="acme",
            granted_scopes=["read", "search"],
        )
    )
    try:
        ok2, _ = gw.authorize("agent", "internet_search")
        assert ok2
        print("[OK] mcp gateway oauth")
    finally:
        reset_tool_call_context(ctx_token)


def test_mcp_tasks_poll():
    mgr = get_mcp_task_manager()
    task_id = mgr.submit(
        server_module="test",
        tool_name="demo",
        runner=lambda: "done-result",
    )
    deadline = time.time() + 5
    rec = None
    while time.time() < deadline:
        rec = mgr.poll(task_id)
        if rec and rec.status.value in {"done", "failed"}:
            break
        time.sleep(0.05)
    assert rec is not None
    assert rec.status.value == "done"
    assert rec.result == "done-result"
    print("[OK] mcp tasks poll")


def test_local_registry_bootstrap():
    reload_harness_config()
    fresh = MCPRegistry()
    old = registry_mod.mcp_registry
    try:
        registry_mod.mcp_registry = fresh
        with patch.dict("os.environ", {}, clear=False):
            import os

            for key in list(os.environ.keys()):
                if key.startswith("HARNESS_MCP"):
                    os.environ.pop(key, None)
            bootstrap_mcp_registry(force=True)
        catalog = fresh.to_catalog()
        names = {item["name"] for item in catalog}
        assert "internet_search" in names
        assert "execute_sql_query" in names
        assert len(catalog) >= 9
        transports = {item["transport"] for item in catalog}
        assert "langchain-tool" in transports
        print("[OK] local registry bootstrap")
    finally:
        registry_mod.mcp_registry = old


def test_registry_sync_mock_list_tools():
    reload_harness_config()
    fresh = MCPRegistry()
    old = registry_mod.mcp_registry
    try:
        registry_mod.mcp_registry = fresh
        import app.mcp.registry_sync as sync_mod

        sync_mod.mcp_registry = fresh

        def fake_list(module: str):
            return [
                {"name": "internet_search", "description": "search web"},
                {"name": "list_sql_tables", "description": "list tables"},
            ]

        with patch("app.mcp.registry_sync._list_tools_for_server", fake_list):
            from app.mcp.registry_sync import sync_mcp_registry_from_servers

            synced = sync_mcp_registry_from_servers(["tavily-mcp", "mysql-mcp"])
            assert synced >= 1
            desc = fresh.get_descriptor("internet_search")
            assert desc is not None
            assert desc.server == "tavily-mcp"
            print("[OK] registry sync mock")
    finally:
        registry_mod.mcp_registry = old


def test_config_phase16():
    cfg = reload_harness_config()
    assert cfg.mcp_pool_enabled is True
    assert cfg.mcp_sync_on_startup is True
    assert cfg.mcp_gateway_rate_limit_per_minute >= 1
    print("[OK] config phase16")


if __name__ == "__main__":
    test_mcp_gateway_rate_limit()
    test_mcp_gateway_oauth()
    test_mcp_tasks_poll()
    test_local_registry_bootstrap()
    test_registry_sync_mock_list_tools()
    test_config_phase16()
    print("\n=== Phase 16 MCP production tests passed ===")
