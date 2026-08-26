"""研究进度评估：enough / gap / abort。语义缺口可问 LLM，是否允许补任务由 Harness 决定。"""

from __future__ import annotations

from typing import Any, Literal

from app.agent.harness.state import ExecutionPlan, LoopState
from app.research.planning.validator import RESEARCH_TYPES, SYNTHESIS_TYPES

Progress = Literal["enough", "gap", "abort", "run"]


def evaluate_progress(
    plan: ExecutionPlan | None,
    *,
    task_status: dict[str, str] | None = None,
    state: LoopState | None = None,
    aborted: bool = False,
) -> Progress:
    if aborted or (state is not None and state.abort_reason):
        return "abort"
    if plan is None or not plan.steps:
        return "abort"
    status = dict(task_status or {})
    if state is not None:
        for index, step in enumerate(plan.steps):
            tid = step.resolved_task_id(index)
            status.setdefault(tid, str(step.metadata.get("status") or "pending"))

    pending_research = [
        step
        for index, step in enumerate(plan.steps)
        if step.step_type in RESEARCH_TYPES
        and status.get(step.resolved_task_id(index), "pending") in {"pending", "running"}
    ]
    if pending_research:
        return "run"

    failed_research = [
        step
        for index, step in enumerate(plan.steps)
        if step.step_type in RESEARCH_TYPES
        and status.get(step.resolved_task_id(index), "pending") == "failed"
    ]
    if failed_research:
        return "gap"

    empty_coverage = False
    if state is not None and (plan.planning_mode or "") == "dynamic":
        research_results = [
            item
            for item in state.step_results
            if getattr(item, "step_type", "") in RESEARCH_TYPES
        ]
        if research_results:
            empty_coverage = any(
                not str(getattr(item, "content", "") or "").strip()
                for item in research_results
            )
    if empty_coverage:
        return "gap"

    pending_synth = [
        step
        for index, step in enumerate(plan.steps)
        if step.step_type in SYNTHESIS_TYPES
        and status.get(step.resolved_task_id(index), "pending") in {"pending", "running"}
    ]
    if pending_synth:
        return "enough"
    return "enough"
