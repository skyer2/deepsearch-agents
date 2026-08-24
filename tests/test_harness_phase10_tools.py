"""Phase 10: SQL 护栏 + Tool Gateway + Tools API 测试（无需 API）。"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config.loader import reload_harness_config
from app.mcp.registry import MCPRegistry
from app.mcp.sql_guard import validate_select_only, validate_sql_identifier
from app.mcp.tool_gateway import ToolGateway, reset_tool_gateway
from app.tools import db_core
import app.mcp.registry as registry_mod
from app.mcp.client import bootstrap_mcp_registry


def test_sql_guard_select_only():
    ok, _ = validate_select_only("SELECT id, name FROM drugs LIMIT 10")
    assert ok
    ok2, code = validate_select_only("DELETE FROM drugs")
    assert not ok2 and code == "select_only_denied"
    ok3, code3 = validate_select_only("SELECT 1; DROP TABLE drugs")
    assert not ok3 and code3 == "multi_statement_denied"
    print("[OK] sql guard select only")


def test_sql_identifier():
    assert validate_sql_identifier("drugs_table")[0]
    assert not validate_sql_identifier("drugs;drop")[0]
    print("[OK] sql identifier")


def test_db_core_gateway_denial():
    reload_harness_config()
    reset_tool_gateway()
    out = db_core.execute_sql_query("DROP TABLE drugs")
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["source"] == "tool_gateway"
    print("[OK] db_core gateway denial")


def test_tool_gateway_step_policy():
    reload_harness_config()
    reset_tool_gateway()
    fresh = MCPRegistry()
    old = registry_mod.mcp_registry
    try:
        registry_mod.mcp_registry = fresh
        bootstrap_mcp_registry()
        gate = ToolGateway(fail_closed=True, enforce_step_policy=True)
        ok = gate.validate_tool_for_step("network_search", "internet_search")
        bad = gate.validate_tool_for_step("network_search", "execute_sql_query")
        assert ok.allowed
        assert not bad.allowed
        assert bad.error_code == "tool_not_allowed_for_step"
        print("[OK] tool gateway step policy")
    finally:
        registry_mod.mcp_registry = old


def test_registry_catalog():
    reload_harness_config()
    fresh = MCPRegistry()
    old = registry_mod.mcp_registry
    try:
        registry_mod.mcp_registry = fresh
        bootstrap_mcp_registry()
        catalog = fresh.to_catalog()
        names = {item["name"] for item in catalog}
        assert "internet_search" in names
        assert "execute_sql_query" in names
        assert len(catalog) >= 9
        print("[OK] registry catalog")
    finally:
        registry_mod.mcp_registry = old


def test_config_phase10():
    cfg = reload_harness_config()
    assert cfg.tools_fail_closed is True
    assert cfg.tools_sql_select_only is True
    assert cfg.mcp_call_timeout_sec == 30
    print("[OK] config phase10")


if __name__ == "__main__":
    test_sql_guard_select_only()
    test_sql_identifier()
    test_db_core_gateway_denial()
    test_tool_gateway_step_policy()
    test_registry_catalog()
    test_config_phase10()
    print("\n=== Phase 10 tools/MCP tests passed ===")
