"""
【Phase 16】从 MCP Server 动态构建 LangChain Tool（单一实现入口）。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from langchain_core.tools import StructuredTool

from app.api.monitor import monitor
from app.mcp.mcp_gateway import get_mcp_gateway


def _as_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False)


def build_mcp_tool(
    *,
    server_module: str,
    server_id: str,
    tool_name: str,
    description: str,
    step_type: str = "",
    invoke: Optional[Callable[..., Any]] = None,
    input_schema: Optional[dict[str, Any]] = None,
) -> StructuredTool:
    """构建经 MCP Gateway 调用的 LangChain Tool。"""

    gateway = get_mcp_gateway()

    if invoke is not None:
        func = invoke
    elif tool_name == "internet_search":

        def func(
            query: str,
            topic: str = "general",
            max_results: int = 5,
            include_raw_content: bool = False,
        ) -> Any:
            monitor.report_tool(
                tool_name="internet_search",
                args={
                    "query": query,
                    "transport": "mcp-gateway",
                    "server": server_id,
                },
            )
            return gateway.call_tool(
                server_module,
                tool_name,
                {
                    "query": query,
                    "topic": topic,
                    "max_results": max_results,
                    "include_raw_content": include_raw_content,
                },
                step_type=step_type,
            )

    elif tool_name in {"list_sql_tables"}:

        def func() -> str:
            monitor.report_tool(tool_name=tool_name, args={"transport": "mcp-gateway"})
            return _as_text(gateway.call_tool(server_module, tool_name, {}, step_type=step_type))

    elif tool_name in {"get_table_data"}:

        def func(table_name: str) -> str:
            monitor.report_tool(
                tool_name=tool_name,
                args={"table_name": table_name, "transport": "mcp-gateway"},
            )
            return _as_text(
                gateway.call_tool(
                    server_module,
                    tool_name,
                    {"table_name": table_name},
                    step_type=step_type,
                )
            )

    elif tool_name in {"execute_sql_query"}:

        def func(query: str) -> str:
            monitor.report_tool(
                tool_name=tool_name,
                args={"query": query, "transport": "mcp-gateway"},
            )
            return _as_text(
                gateway.call_tool(
                    server_module,
                    tool_name,
                    {"query": query},
                    step_type=step_type,
                )
            )

    elif tool_name in {"get_assistant_list"}:

        def func() -> str:
            monitor.report_tool(tool_name=tool_name, args={"transport": "mcp-gateway"})
            return _as_text(gateway.call_tool(server_module, tool_name, {}, step_type=step_type))

    elif tool_name in {"create_ask_delete"}:

        def func(chat_name: str, question: str) -> str:
            monitor.report_tool(
                tool_name=tool_name,
                args={"chat_name": chat_name, "transport": "mcp-gateway"},
            )
            return _as_text(
                gateway.call_tool(
                    server_module,
                    tool_name,
                    {"chat_name": chat_name, "question": question},
                    step_type=step_type,
                )
            )

    elif tool_name == "convert_md_to_pdf_async":

        def func(md_filename: str, pdf_filename: str | None = None) -> str:
            from app.config.loader import get_harness_config
            from app.mcp.mcp_tasks import get_mcp_task_manager

            monitor.report_tool(
                tool_name="convert_md_to_pdf_async",
                args={"md_filename": md_filename, "transport": "mcp-gateway"},
            )
            raw = gateway.call_tool(
                server_module,
                tool_name,
                {"md_filename": md_filename, "pdf_filename": pdf_filename},
                step_type=step_type,
            )
            text = _as_text(raw)
            cfg = get_harness_config()
            if not cfg.mcp_tasks_enabled:
                return text
            try:
                payload = json.loads(text) if isinstance(text, str) else raw
                if not isinstance(payload, dict):
                    payload = {}
                task_id = payload.get("task_id")
                if task_id:
                    rec = get_mcp_task_manager().wait(task_id, timeout_sec=180.0)
                    if rec.status.value == "done":
                        return _as_text(rec.result)
                    return json.dumps(
                        {"ok": False, "error": rec.error, "task_id": task_id},
                        ensure_ascii=False,
                    )
            except (json.JSONDecodeError, KeyError, TypeError, TimeoutError):
                pass
            return text

    else:
        from app.mcp.schema_adapter import build_structured_tool

        def _invoke(payload: dict[str, Any]) -> Any:
            monitor.report_tool(
                tool_name=tool_name,
                args={"transport": "mcp-gateway", **payload},
            )
            return gateway.call_tool(server_module, tool_name, payload, step_type=step_type)

        return build_structured_tool(
            name=tool_name,
            description=f"{description}（MCP {server_id}）",
            invoke=_invoke,
            input_schema=input_schema,
        )

    return StructuredTool.from_function(
        func=func,  # type: ignore[arg-type]
        name=tool_name,
        description=f"{description}（MCP {server_id}）",
    )
