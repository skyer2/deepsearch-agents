"""
薄 StateGraph：只负责 node wiring / conditional edge / Send / interrupt。

业务逻辑仍在 planner、scheduler、workers、现有 harness services。
"""

from __future__ import annotations

from typing import Any, Literal

from app.agent.harness.planner import build_plan, understand_task
from app.agent.harness.state import ExecutionPlan
from app.research.runtime.scheduler import (
    all_retrieval_done,
    annotate_plan_tasks,
    next_synthesis_step,
    ready_retrieval_steps,
)
from app.research.runtime.state import ResearchState, empty_research_state


def _plan_from_state(state: ResearchState) -> ExecutionPlan | None:
    if not state.get("plan"):
        return None
    return ExecutionPlan.from_dict(state["plan"])


def intent_node(state: ResearchState) -> dict[str, Any]:
    intent = understand_task(state["task_query"])
    return {
        "intent": intent.to_dict(),
        "needs_clarification": bool(intent.needs_clarification),
    }


def plan_node(state: ResearchState) -> dict[str, Any]:
    from app.agent.harness.planner import finalize_plan
    from app.agent.harness.state import TaskIntent

    raw = state.get("intent") or {}
    intent = TaskIntent.from_dict(raw) if raw else understand_task(state["task_query"])
    plan = annotate_plan_tasks(finalize_plan(build_plan(intent)))
    status = {
        step.resolved_task_id(i): str(step.metadata.get("status") or "pending")
        for i, step in enumerate(plan.steps)
    }
    return {
        "plan": plan.to_dict(),
        "plan_version": plan.plan_version,
        "task_status": status,
        "needs_plan_review": False,
    }


def route_after_intent(state: ResearchState) -> Literal["clarify", "plan"]:
    if state.get("needs_clarification"):
        return "clarify"
    return "plan"


def clarify_node(state: ResearchState) -> dict[str, Any]:
    """HITL 澄清占位：默认自动保守解析，真正等待由外层 HITL 接入。"""
    from app.agent.harness.planner import auto_resolve_clarification
    from app.agent.harness.state import TaskIntent

    intent = TaskIntent.from_dict(state.get("intent") or {})
    resolved = auto_resolve_clarification(intent)
    return {"intent": resolved.to_dict(), "needs_clarification": False}


def dispatch_node(state: ResearchState) -> dict[str, Any]:
    return {}


def route_dispatch(state: ResearchState) -> list[Any] | str:
    from langgraph.types import Send

    plan = _plan_from_state(state)
    if plan is None:
        return "finalize"
    status = dict(state.get("task_status") or {})
    ready = ready_retrieval_steps(plan, status)
    if ready:
        sends: list[Any] = []
        for index, step in ready:
            sends.append(
                Send(
                    "research_worker",
                    {
                        "run_id": state["run_id"],
                        "session_id": state["session_id"],
                        "plan_version": int(state.get("plan_version") or 1),
                        "task_id": step.resolved_task_id(index),
                        "step_index": index,
                        "step_type": step.step_type,
                        "description": step.description,
                        "subagent": step.subagent or "",
                        "task_query": state["task_query"],
                    },
                )
            )
        return sends
    if next_synthesis_step(plan, status) is not None and all_retrieval_done(plan, status):
        return "synthesize"
    return "finalize"


def research_worker_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    Leaf 执行由运行时注入的 invoke_worker 完成。
    默认占位：把任务标为 done，供图编译与单测使用。
    """
    invoke = state.get("_invoke_worker")
    if callable(invoke):
        return invoke(state)
    task_id = str(state.get("task_id") or "")
    return {
        "worker_results": [
            {
                "task_id": task_id,
                "step_type": state.get("step_type"),
                "ok": True,
                "payload": {"summary": "placeholder", "facts": [], "sources": []},
            }
        ],
        "task_status": {task_id: "done"},
        "evidence_refs": [task_id] if task_id else [],
    }


def synthesize_node(state: ResearchState) -> dict[str, Any]:
    plan = _plan_from_state(state)
    status = dict(state.get("task_status") or {})
    nxt = next_synthesis_step(plan, status) if plan else None
    if nxt is None:
        return {"status": "synthesized"}
    index, step = nxt
    tid = step.resolved_task_id(index)
    status[tid] = "done"
    return {"task_status": status, "status": "synthesized"}


def finalize_node(state: ResearchState) -> dict[str, Any]:
    return {
        "status": state.get("status") or "completed",
        "final_content": state.get("final_content") or "",
    }


def compile_research_graph(*, checkpointer: Any = None, invoke_worker: Any = None):
    """可执行的 Domain Harness 表示。invoke_worker 注入后才跑真实 Leaf。"""
    from langgraph.graph import END, START, StateGraph

    def _worker(payload: dict[str, Any]) -> dict[str, Any]:
        if invoke_worker is not None:
            payload = dict(payload)
            payload["_invoke_worker"] = invoke_worker
        return research_worker_node(payload)

    builder = StateGraph(ResearchState)
    builder.add_node("intent", intent_node)
    builder.add_node("clarify", clarify_node)
    builder.add_node("plan", plan_node)
    builder.add_node("dispatch", dispatch_node)
    builder.add_node("research_worker", _worker)
    builder.add_node("synthesize", synthesize_node)
    builder.add_node("finalize", finalize_node)

    builder.add_edge(START, "intent")
    builder.add_conditional_edges(
        "intent",
        route_after_intent,
        {"clarify": "clarify", "plan": "plan"},
    )
    builder.add_edge("clarify", "plan")
    builder.add_edge("plan", "dispatch")
    builder.add_conditional_edges(
        "dispatch",
        route_dispatch,
        ["research_worker", "synthesize", "finalize"],
    )
    builder.add_edge("research_worker", "dispatch")
    builder.add_edge("synthesize", "dispatch")
    builder.add_edge("finalize", END)

    kwargs: dict[str, Any] = {}
    if checkpointer is not None:
        kwargs["checkpointer"] = checkpointer
    return builder.compile(**kwargs)


def initial_graph_state(
    *,
    run_id: str,
    session_id: str,
    task_query: str,
    **kwargs: Any,
) -> ResearchState:
    return empty_research_state(
        run_id=run_id,
        session_id=session_id,
        task_query=task_query,
        **kwargs,
    )


from app.research.runtime.scheduler import dispatch_sends

__all__ = [
    "compile_research_graph",
    "dispatch_sends",
    "initial_graph_state",
    "intent_node",
    "plan_node",
    "route_dispatch",
]
