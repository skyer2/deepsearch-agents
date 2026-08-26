"""PlanPatch：Evaluator 发现缺口后，由代码决定是否接受补丁。"""

from __future__ import annotations

from typing import Any

from app.agent.harness.planner import finalize_plan
from app.agent.harness.state import ExecutionPlan, PlanStep, TaskIntent
from app.research.planning.lead_planner import research_step_from_task
from app.research.planning.policy import SourcePolicy, parse_source_policy
from app.research.planning.validator import RESEARCH_TYPES, validate_hybrid_plan


def apply_plan_patch(
    plan: ExecutionPlan,
    patch: dict[str, Any],
    intent: TaskIntent,
    *,
    policy: SourcePolicy | None = None,
    max_new_tasks: int = 2,
    max_plan_steps: int = 12,
) -> tuple[ExecutionPlan, list[str]]:
    """校验并追加 research tasks。拒绝越权来源 / 环 / 超预算。"""
    policy = policy or parse_source_policy(intent.raw_query)
    additions = list(patch.get("add_tasks") or [])[: max(0, max_new_tasks)]
    if not additions:
        return plan, ["empty_patch"]
    existing = {step.task_id for step in plan.steps if step.task_id}
    new_steps = list(plan.steps)
    inserted = 0
    synth_index = next(
        (i for i, s in enumerate(new_steps) if s.step_type in {"generate_markdown", "summarize"}),
        len(new_steps),
    )
    for raw in additions:
        if not isinstance(raw, dict):
            continue
        objective = str(raw.get("objective") or "").strip()
        if not objective:
            continue
        tid = str(raw.get("task_id") or f"t_patch_{inserted + 1}")
        if tid in existing:
            tid = f"{tid}_v{plan.plan_version + 1}"
        sources = [
            s
            for s in (raw.get("allowed_sources") or [])
            if str(s) in policy.allowed_sources
        ] or [s for s in ("web", "kb", "db") if s in policy.allowed_sources]
        depends = [str(x) for x in (raw.get("depends_on") or []) if str(x) in existing]
        step = research_step_from_task(
            task_id=tid,
            objective=objective,
            depends_on=depends,
            sources=sources,
        )
        new_steps.insert(synth_index + inserted, step)
        existing.add(tid)
        inserted += 1
        synth_index += 1
    if inserted == 0:
        return plan, ["empty_patch"]
    for step in new_steps:
        if step.step_type in {"generate_markdown", "summarize"}:
            step.depends_on = [
                s.task_id for s in new_steps if s.task_id and s.step_type in RESEARCH_TYPES
            ]
    candidate = ExecutionPlan(
        steps=new_steps,
        summary=" → ".join(s.description for s in new_steps),
        plan_version=int(plan.plan_version or 1) + 1,
        planning_mode=plan.planning_mode or "dynamic",
        research_brief=plan.research_brief,
    )
    candidate = finalize_plan(candidate)
    issues = validate_hybrid_plan(
        intent,
        candidate,
        policy=policy,
        max_plan_steps=max_plan_steps,
    )
    if issues:
        return plan, issues
    return candidate, []


def build_gap_patch(
    plan: ExecutionPlan,
    intent: TaskIntent,
    *,
    worker_results: list[Any] | None = None,
) -> dict[str, Any]:
    """无 LLM 时的缺口补丁：失败或空证据的研究任务补一刀，且遵守来源策略。"""
    policy = parse_source_policy(intent.raw_query)
    results = list(worker_results or [])
    empty_ids = {
        str(row.get("task_id"))
        for row in results
        if isinstance(row, dict) and (not row.get("ok") or not str(row.get("summary") or "").strip())
    }
    failed = [
        step
        for step in plan.steps
        if step.step_type in RESEARCH_TYPES
        and (
            str(step.metadata.get("status") or "") == "failed"
            or step.task_id in empty_ids
        )
    ]
    if not failed:
        failed = [
            step
            for step in plan.steps
            if step.step_type in RESEARCH_TYPES
            and str(step.metadata.get("status") or "") != "done"
        ]
    target = failed[0] if failed else None
    if target is None:
        return {"add_tasks": [], "reason": "no_gap"}
    sources = [
        s
        for s in (target.metadata or {}).get("allowed_sources") or []
        if s in policy.allowed_sources
    ] or [s for s in ("web", "kb", "db") if s in policy.allowed_sources]
    if not sources:
        return {"add_tasks": [], "reason": "no_allowed_source"}
    return {
        "reason": f"gap:{target.task_id}",
        "add_tasks": [
            {
                "task_id": f"{target.task_id}_gap",
                "objective": f"补充证据：{target.objective or target.description}",
                "depends_on": [],
                "allowed_sources": sources,
            }
        ],
    }
