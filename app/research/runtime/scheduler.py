"""
DAG Scheduler：宏观控制代码化。

READY = PENDING 且 depends_on 均 DONE。
并行单元当前仍按数据源步（Phase 4 再升级为 research task）。
"""

from __future__ import annotations

from typing import Any, Iterable

from app.agent.harness.orchestration import RETRIEVAL_STEP_TYPES, SYNTHESIS_STEP_TYPES
from app.agent.harness.state import ExecutionPlan, PlanStep

TERMINAL_STATUS = frozenset({"done", "failed", "skipped"})


def annotate_plan_tasks(plan: ExecutionPlan) -> ExecutionPlan:
    """为每步补 task_id / depends_on。检索步默认无依赖，合成步依赖全部检索。"""
    retrieval_ids: list[str] = []
    markdown_id = ""
    for index, step in enumerate(plan.steps):
        if not step.task_id:
            step.task_id = f"t{index}:{step.step_type}"
        if step.depends_on:
            if step.step_type in RETRIEVAL_STEP_TYPES:
                retrieval_ids.append(step.task_id)
            if step.step_type == "generate_markdown":
                markdown_id = step.task_id
            continue
        if step.step_type in RETRIEVAL_STEP_TYPES:
            step.depends_on = []
            retrieval_ids.append(step.task_id)
        elif step.step_type in {"generate_markdown", "summarize"}:
            step.depends_on = list(retrieval_ids)
            markdown_id = step.task_id
        elif step.step_type == "convert_pdf":
            step.depends_on = [markdown_id] if markdown_id else list(retrieval_ids)
        else:
            step.depends_on = list(retrieval_ids)
    if plan.plan_version < 1:
        plan.plan_version = 1
    return plan


def task_status_map(plan: ExecutionPlan) -> dict[str, str]:
    status: dict[str, str] = {}
    for index, step in enumerate(plan.steps):
        tid = step.resolved_task_id(index)
        status[tid] = str(step.metadata.get("status") or "pending")
    return status


def _deps_satisfied(step: PlanStep, status: dict[str, str]) -> bool:
    for dep in step.depends_on or []:
        if status.get(dep) != "done":
            return False
    return True


def ready_steps(
    plan: ExecutionPlan,
    status: dict[str, str] | None = None,
    *,
    include_types: Iterable[str] | None = None,
) -> list[tuple[int, PlanStep]]:
    status = status or task_status_map(plan)
    allowed = set(include_types) if include_types is not None else None
    ready: list[tuple[int, PlanStep]] = []
    for index, step in enumerate(plan.steps):
        tid = step.resolved_task_id(index)
        current = status.get(tid, "pending")
        if current not in {"pending", "running"}:
            continue
        if current == "running":
            continue
        if allowed is not None and step.step_type not in allowed:
            continue
        if _deps_satisfied(step, status):
            ready.append((index, step))
    return ready


def ready_retrieval_steps(
    plan: ExecutionPlan,
    status: dict[str, str] | None = None,
) -> list[tuple[int, PlanStep]]:
    return ready_steps(plan, status, include_types=RETRIEVAL_STEP_TYPES)


def all_retrieval_done(plan: ExecutionPlan, status: dict[str, str] | None = None) -> bool:
    status = status or task_status_map(plan)
    retrieval = [
        step.resolved_task_id(i)
        for i, step in enumerate(plan.steps)
        if step.step_type in RETRIEVAL_STEP_TYPES
    ]
    if not retrieval:
        return True
    return all(status.get(tid) == "done" for tid in retrieval)


def next_synthesis_step(
    plan: ExecutionPlan,
    status: dict[str, str] | None = None,
) -> tuple[int, PlanStep] | None:
    ready = ready_steps(plan, status, include_types=SYNTHESIS_STEP_TYPES)
    return ready[0] if ready else None


def dispatch_sends(plan: ExecutionPlan, status: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """纯数据描述的 fan-out 清单；graph.py 再转成 Send。"""
    payloads: list[dict[str, Any]] = []
    for index, step in ready_retrieval_steps(plan, status):
        payloads.append(
            {
                "task_id": step.resolved_task_id(index),
                "step_index": index,
                "step_type": step.step_type,
                "description": step.description,
                "subagent": step.subagent or "",
            }
        )
    return payloads
