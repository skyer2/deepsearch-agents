"""
【Phase 10】Tool Gateway — 调用前 fail-closed 校验 + 标准化错误

企业生产：SQL 白名单、步级工具策略（Registry）、统一 denial 格式。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.mcp.registry import MCPToolDescriptor, mcp_registry
from app.mcp.sql_guard import validate_select_only, validate_sql_identifier


@dataclass
class ToolValidationResult:
    allowed: bool
    error_code: str = ""
    message: str = ""

    def to_denial_text(self) -> str:
        """Agent 可解析的标准化拒绝 payload。"""
        return json.dumps(
            {
                "ok": False,
                "error_code": self.error_code or "tool_denied",
                "message": self.message or "工具调用被拒绝",
                "source": "tool_gateway",
            },
            ensure_ascii=False,
        )


class ToolGateway:
    """工具调用网关：调用前校验，默认 fail-closed。"""

    def __init__(
        self,
        *,
        fail_closed: bool = True,
        sql_select_only: bool = True,
        enforce_step_policy: bool = True,
    ):
        self.fail_closed = fail_closed
        self.sql_select_only = sql_select_only
        self.enforce_step_policy = enforce_step_policy

    def validate_sql(self, query: str) -> ToolValidationResult:
        if not self.fail_closed:
            return ToolValidationResult(allowed=True)
        ok, code = validate_select_only(query, enabled=self.sql_select_only)
        if ok:
            return ToolValidationResult(allowed=True)
        return ToolValidationResult(
            allowed=False,
            error_code=code,
            message=f"SQL 调用被拒绝: {code}",
        )

    def validate_table_name(self, table_name: str) -> ToolValidationResult:
        if not self.fail_closed:
            return ToolValidationResult(allowed=True)
        ok, code = validate_sql_identifier(table_name)
        if ok:
            return ToolValidationResult(allowed=True)
        return ToolValidationResult(
            allowed=False,
            error_code=code,
            message="表名不符合安全策略，仅允许字母数字下划线",
        )

    def validate_tool_for_step(
        self,
        step_type: str,
        tool_name: str,
    ) -> ToolValidationResult:
        """步级工具白名单：工具必须在 Registry 中且 step_types 包含当前步。"""
        if not self.fail_closed or not self.enforce_step_policy:
            return ToolValidationResult(allowed=True)
        if not step_type:
            return ToolValidationResult(allowed=True)

        descriptors = mcp_registry.list_descriptors(step_type)
        allowed_names = {d.name for d in descriptors}
        if tool_name in allowed_names:
            return ToolValidationResult(allowed=True)
        return ToolValidationResult(
            allowed=False,
            error_code="tool_not_allowed_for_step",
            message=f"工具 {tool_name} 不允许在步骤 {step_type} 使用",
        )

    def describe_policy(self) -> dict[str, Any]:
        """供 /api/tools/policy 与面试演示。"""
        return {
            "fail_closed": self.fail_closed,
            "sql_select_only": self.sql_select_only,
            "enforce_step_policy": self.enforce_step_policy,
            "registered_tools": len(mcp_registry.list_descriptors()),
            "step_policies": {
                step: [d.name for d in mcp_registry.list_descriptors(step)]
                for step in sorted(
                    {st for d in mcp_registry.list_descriptors() for st in d.step_types}
                )
            },
        }


_gateway: ToolGateway | None = None


def get_tool_gateway() -> ToolGateway:
    global _gateway
    if _gateway is None:
        from app.config.loader import get_harness_config

        cfg = get_harness_config()
        _gateway = ToolGateway(
            fail_closed=cfg.tools_fail_closed,
            sql_select_only=cfg.tools_sql_select_only,
            enforce_step_policy=cfg.tools_enforce_step_policy,
        )
    return _gateway


def reset_tool_gateway() -> None:
    global _gateway
    _gateway = None
