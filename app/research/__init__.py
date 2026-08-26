"""
Deep Research Domain Harness。

本包是架构定义者：Intent / Plan / Policy / Evidence / Eval。
LangGraph 是它选择的 execution runtime，不是上一层 Harness。
"""

from __future__ import annotations

__all__ = [
    "CONTROL_INVARIANTS",
    "SEMANTIC_DECISIONS",
]

# Control invariants deterministic；semantic decisions agentic。
CONTROL_INVARIANTS = (
    "max_parallel_workers",
    "token_tool_budget",
    "timeout_retry",
    "dependency",
    "tool_permission",
    "checkpoint_resume",
    "hitl_policy",
    "idempotency",
)

SEMANTIC_DECISIONS = (
    "research_workstreams",
    "query_refinement",
    "local_tool_use",
    "evidence_gap_judgment",
)
