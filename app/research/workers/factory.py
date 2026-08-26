"""
Leaf Agent 工厂。

普通 Research Worker / 合成工人：langchain.agents.create_agent。
HITL：interrupt() 在副作用之前（PURE → HITL → SIDE EFFECT）。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from langchain.agents import create_agent

from app.research.workers.prompts import SYNTHESIS_PROMPT_ADDENDUM, WORKER_PROMPT_ADDENDUM

HITL_TOOL_DEFAULTS = {
    "generate_markdown": True,
    "convert_md_to_pdf": True,
    "read_file_content": False,
}


def _is_reject(decisions: list[Any]) -> bool:
    for item in decisions:
        if isinstance(item, dict) and item.get("type") == "reject":
            return True
    return False


def _apply_edits(kwargs: dict[str, Any], decisions: list[Any]) -> dict[str, Any]:
    updated = dict(kwargs)
    for item in decisions:
        if not isinstance(item, dict) or item.get("type") != "edit":
            continue
        args = item.get("args") or item.get("edited_action") or {}
        if isinstance(args, dict):
            updated.update(args)
    return updated


def _normalize_resume(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return [{"type": "approve"}]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        if isinstance(raw.get("decisions"), list):
            return [item for item in raw["decisions"] if isinstance(item, dict)]
        if raw.get("type"):
            return [raw]
    return [{"type": "approve"}]


def attach_approval_gate(tool: Any, *, enabled: bool) -> Any:
    """工具真正执行前 interrupt；resume 后 interrupt() 返回决定再跑副作用。"""
    if not enabled or tool is None:
        return tool

    from langchain_core.tools import StructuredTool
    from langgraph.types import interrupt

    tool_name = getattr(tool, "name", "tool")
    description = getattr(tool, "description", "") or tool_name
    args_schema = getattr(tool, "args_schema", None)

    def _run(**kwargs: Any) -> Any:
        payload = {
            "action_requests": [{"name": tool_name, "args": dict(kwargs)}],
            "review_configs": [
                {
                    "action_name": tool_name,
                    "allowed_decisions": ["approve", "reject", "edit"],
                }
            ],
        }
        decisions = _normalize_resume(interrupt(payload))
        if _is_reject(decisions):
            return json.dumps(
                {
                    "ok": False,
                    "error_code": "hitl_rejected",
                    "message": f"用户拒绝了工具 {tool_name}",
                },
                ensure_ascii=False,
            )
        kwargs = _apply_edits(kwargs, decisions)
        if hasattr(tool, "invoke"):
            return tool.invoke(kwargs)
        func = getattr(tool, "func", None)
        if callable(func):
            return func(**kwargs)
        raise RuntimeError(f"tool {tool_name} is not invokable")

    return StructuredTool.from_function(
        func=_run,
        name=tool_name,
        description=description,
        args_schema=args_schema,
    )


def create_leaf_agent(
    *,
    model: Any,
    tools: list[Any],
    system_prompt: str,
    checkpointer: Any = None,
    interrupt_on: Mapping[str, bool] | None = None,
    addendum: str = "",
) -> Any:
    gated: list[Any] = []
    flags = dict(interrupt_on or {})
    for tool in tools:
        name = getattr(tool, "name", "")
        gated.append(attach_approval_gate(tool, enabled=bool(flags.get(name, False))))
    prompt = system_prompt
    if addendum:
        prompt = f"{system_prompt.rstrip()}\n{addendum}"
    kwargs: dict[str, Any] = {
        "model": model,
        "tools": gated,
        "system_prompt": prompt,
    }
    if checkpointer is not None:
        kwargs["checkpointer"] = checkpointer
    try:
        return create_agent(**kwargs)
    except TypeError:
        model = kwargs.pop("model")
        return create_agent(model, **kwargs)


def create_research_worker(
    *,
    model: Any,
    tools: list[Any],
    system_prompt: str,
    checkpointer: Any = None,
) -> Any:
    return create_leaf_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        checkpointer=checkpointer,
        interrupt_on={},
        addendum=WORKER_PROMPT_ADDENDUM,
    )


def create_synthesis_worker(
    *,
    model: Any,
    tools: list[Any],
    system_prompt: str,
    checkpointer: Any = None,
    interrupt_on: Mapping[str, bool] | None = None,
) -> Any:
    return create_leaf_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        checkpointer=checkpointer,
        interrupt_on=interrupt_on if interrupt_on is not None else HITL_TOOL_DEFAULTS,
        addendum=SYNTHESIS_PROMPT_ADDENDUM,
    )
