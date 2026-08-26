"""
WorkerRegistry：按 step_type 直调 Leaf，不再经 Main DeepAgent 二次路由。

kind:
  create_agent     — 默认
  create_deep_agent — 仅 document_research / coding 等需要 filesystem 的工人
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

STEP_KINDS: dict[str, str] = {
    "network_search": "create_agent",
    "database_query": "create_agent",
    "knowledge_base": "create_agent",
    "file_read": "create_agent",
    "research": "create_agent",
    "generate_markdown": "create_agent",
    "convert_pdf": "create_agent",
    "summarize": "create_agent",
}

class UnsupportedTaskType(KeyError):
    """Planner 产出了 WorkerRegistry 未注册的 step_type。"""


DIRECT_STEP_TYPES = frozenset(STEP_KINDS)


@dataclass
class WorkerSpec:
    step_type: str
    kind: str
    graph: Any


class WorkerRegistry:
    def __init__(self, workers: dict[str, Any] | None = None):
        self._workers: dict[str, Any] = dict(workers or {})

    def register(self, step_type: str, graph: Any) -> None:
        self._workers[step_type] = graph

    def get(self, step_type: str) -> Any | None:
        return self._workers.get(step_type)

    def as_step_map(self) -> dict[str, Any]:
        return dict(self._workers)

    def has(self, step_type: str) -> bool:
        return step_type in self._workers and self._workers[step_type] is not None


def worker_tools_for_step(step_type: str) -> list[str]:
    return {
        "network_search": ["internet_search"],
        "database_query": ["list_sql_tables", "get_table_data", "execute_sql_query"],
        "knowledge_base": ["get_assistant_list", "create_ask_delete"],
        "file_read": ["read_file_content"],
        "research": [
            "internet_search",
            "list_sql_tables",
            "get_table_data",
            "execute_sql_query",
            "get_assistant_list",
            "create_ask_delete",
            "read_file_content",
        ],
        "generate_markdown": ["generate_markdown", "read_file_content"],
        "convert_pdf": ["convert_md_to_pdf", "generate_markdown", "read_file_content"],
        "summarize": ["generate_markdown", "read_file_content"],
    }.get(step_type, [])


def resolve_execute_target(
    step_type: str,
    *,
    workers: dict[str, Any] | None,
    main_agent: Any = None,
    direct_invoke: bool = True,
) -> tuple[Any, str]:
    """计划指定谁干就调谁。未注册 step 默认 fail-closed，禁止落到合成工人。"""
    if not direct_invoke:
        if main_agent is not None:
            return main_agent, "main"
        raise UnsupportedTaskType(step_type)
    if workers and workers.get(step_type) is not None:
        return workers[step_type], "direct"
    raise UnsupportedTaskType(step_type)


def _file_tool_map() -> dict[str, Any]:
    from app.mcp.client import get_file_tools

    mapping: dict[str, Any] = {}
    for tool in get_file_tools():
        if tool is not None:
            mapping[getattr(tool, "name", "")] = tool
    return mapping


def build_worker_registry(
    *,
    model: Any,
    checkpointer: Any = None,
    interrupt_on: Mapping[str, bool] | None = None,
    kinds: Mapping[str, str] | None = None,
) -> WorkerRegistry:
    from app.agent.subagents.database_query_agent import build_database_query_agent
    from app.agent.subagents.knowledge_base_agent import build_knowledge_base_agent
    from app.agent.subagents.network_search_agent import build_network_search_agent
    from app.mcp.client import get_db_tools, get_internet_search_tool, get_ragflow_tools
    from app.research.workers.factory import create_research_worker, create_synthesis_worker
    from app.research.workers.prompts import RESEARCH_TASK_SYSTEM_PROMPT, SYNTHESIS_SYSTEM_PROMPT

    kind_map = dict(STEP_KINDS)
    if kinds:
        kind_map.update(kinds)

    net = build_network_search_agent()
    db = build_database_query_agent()
    kb = build_knowledge_base_agent()
    files = _file_tool_map()
    synthesis_prompt = SYNTHESIS_SYSTEM_PROMPT
    hitl = dict(interrupt_on or {})

    registry = WorkerRegistry()

    def _maybe_deep(step_type: str, tools: list[Any], prompt: str, *, hitl_flags: Mapping[str, bool] | None = None) -> Any:
        kind = kind_map.get(step_type, "create_agent")
        if kind == "create_deep_agent":
            from deepagents import create_deep_agent

            return create_deep_agent(
                model=model,
                system_prompt=prompt,
                tools=tools,
                subagents=[],
                checkpointer=checkpointer,
                interrupt_on=dict(hitl_flags or {}),
            )
        if hitl_flags:
            return create_synthesis_worker(
                model=model,
                tools=tools,
                system_prompt=prompt,
                checkpointer=checkpointer,
                interrupt_on=hitl_flags,
            )
        return create_research_worker(
            model=model,
            tools=tools,
            system_prompt=prompt,
            checkpointer=checkpointer,
        )

    net_tool = get_internet_search_tool()
    net_tools = [net_tool] if net_tool is not None else [t for t in (net.get("tools") or []) if t]
    db_tools = [t for t in (get_db_tools() or []) if t] or [t for t in (db.get("tools") or []) if t]
    kb_tools = [t for t in (get_ragflow_tools() or []) if t] or [t for t in (kb.get("tools") or []) if t]

    registry.register(
        "network_search",
        _maybe_deep("network_search", net_tools, str(net.get("system_prompt") or "")),
    )
    registry.register(
        "database_query",
        _maybe_deep("database_query", db_tools, str(db.get("system_prompt") or "")),
    )
    registry.register(
        "knowledge_base",
        _maybe_deep("knowledge_base", kb_tools, str(kb.get("system_prompt") or "")),
    )
    research_tools: list[Any] = []
    for tool in [*net_tools, *db_tools, *kb_tools]:
        if tool is not None and tool not in research_tools:
            research_tools.append(tool)
    registry.register(
        "research",
        _maybe_deep("research", research_tools, RESEARCH_TASK_SYSTEM_PROMPT),
    )

    read_tool = files.get("read_file_content")
    md_tool = files.get("generate_markdown")
    pdf_tool = files.get("convert_md_to_pdf")
    read_tools = [read_tool] if read_tool else []
    md_tools = [t for t in (md_tool, read_tool) if t]
    pdf_tools = [t for t in (pdf_tool, md_tool, read_tool) if t]

    registry.register(
        "file_read",
        _maybe_deep("file_read", read_tools, synthesis_prompt, hitl_flags={"read_file_content": hitl.get("read_file_content", False)}),
    )
    registry.register(
        "generate_markdown",
        _maybe_deep("generate_markdown", md_tools, synthesis_prompt, hitl_flags=hitl),
    )
    registry.register(
        "summarize",
        _maybe_deep("summarize", md_tools, synthesis_prompt, hitl_flags=hitl),
    )
    registry.register(
        "convert_pdf",
        _maybe_deep("convert_pdf", pdf_tools, synthesis_prompt, hitl_flags=hitl),
    )
    return registry
