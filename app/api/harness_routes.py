"""
【Phase 13】Harness 能力清单 API

GET /api/harness/capabilities  面试/运维：当前运行时能力与护栏
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.config.loader import get_harness_config

router = APIRouter(prefix="/api/harness", tags=["harness"])


@router.get("/capabilities")
def harness_capabilities() -> dict[str, Any]:
    """描述当前 Harness 作为运行时的能力面。"""
    config = get_harness_config()
    return {
        "version": config.version,
        "loop": [
            "understand",
            "plan",
            "build_context",
            "execute / parallel_execute",
            "compress",
            "validate",
            "recover / replan",
            "finalize",
            "abort",
        ],
        "control_plane": {
            "domain_harness": "app.research",
            "runtime": "langgraph",
            "leaf": "langchain.create_agent",
            "main_deep_agent": False,
        },
        "guardrails": {
            "max_tool_calls": config.max_tool_calls,
            "max_total_tokens": config.max_total_tokens,
            "max_run_sec": config.max_run_sec,
            "max_replan_count": config.max_replan_count,
            "max_plan_steps": config.max_plan_steps,
            "step_timeout_sec": config.step_timeout_sec,
            "max_retries": config.max_retries,
        },
        "orchestration": {
            "parallel_retrieval": config.parallel_retrieval_enabled,
            "subagent_binding": config.enforce_subagent_binding,
            "step_checkpoint": config.step_checkpoint_enabled,
            "structured_worker_output": config.require_structured_worker_output,
            "direct_worker_invoke": getattr(config, "direct_worker_invoke", True),
            "persist_loop_state": getattr(config, "persist_loop_state", True),
            "graph_runtime_enabled": getattr(config, "graph_runtime_enabled", False),
        },
        "planner": {
            "llm_enabled": config.planner_llm_enabled,
            "structured_slots": True,
            "clarification_hitl": config.planner_clarification_enabled,
            "plan_review": config.hitl_plan_review_enabled,
            "plan_validation": True,
        },
        "safety": {
            "tools_fail_closed": config.tools_fail_closed,
            "sql_select_only": config.tools_sql_select_only,
            "citations": config.citations_enabled,
            "hitl": config.hitl_enabled,
            "untrusted_context": config.context_wrap_untrusted_external,
        },
        "note": (
            "Domain Harness 定义计划、护栏、证据与评测；LangGraph 是 execution runtime。"
            "Leaf 为 create_agent，按 ResearchPlan 直调，不再经 Main DeepAgent 二次路由。"
            "Checkpointer 不管外部副作用，IdempotencyRegistry 仍然保留。"
        ),
    }
