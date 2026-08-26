"""Hybrid planning：来源策略 + DIRECT/TEMPLATE/DYNAMIC + PlanPatch（无需 LLM）。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.orchestration import check_unauthorized_tools
from app.agent.harness.planner import build_plan, understand_task, validate_plan_against_intent
from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.planning.compose import compose_execution_plan_sync
from app.research.planning.lead_planner import plan_from_lead_payload
from app.research.planning.plan_patch import apply_plan_patch, build_gap_patch
from app.research.planning.policy import (
    parse_source_policy,
    select_planning_mode,
    tools_for_sources,
)
from app.research.planning.progress import evaluate_progress
from app.research.runtime.scheduler import ready_research_steps
from app.research.workers.registry import resolve_execute_target


def test_source_policy_forbids_web():
    query = "只根据内部数据库分析 Tesla 库存，不要联网"
    intent = understand_task(query)
    assert intent.needs_network is False
    assert intent.needs_database is True
    assert "web" in intent.forbidden_sources
    policy = parse_source_policy(query)
    assert not policy.allows("web")
    plan, issues = compose_execution_plan_sync(intent)
    assert "internet_search" not in {
        tool for step in plan.steps for tool in (step.allowed_tools or [])
    }
    assert all(step.step_type != "network_search" for step in plan.steps)
    assert not any(i.startswith("forbidden") for i in issues)
    print("[OK] source policy forbids web")


def test_direct_and_template_still_source_oriented():
    direct = understand_task("搜索2026年AI电商趋势，生成Markdown报告")
    assert select_planning_mode(direct) == "direct"
    plan = build_plan(direct)
    assert [s.step_type for s in plan.steps] == ["network_search", "generate_markdown"]
    ok, issues = validate_plan_against_intent(direct, plan)
    assert ok and not issues

    template = understand_task("结合公开资料和数据库，整理机器人行业报告并生成PDF")
    assert select_planning_mode(template) == "template"
    tplan, tissues = compose_execution_plan_sync(template)
    types = [s.step_type for s in tplan.steps]
    assert "network_search" in types and "database_query" in types
    assert tplan.planning_mode == "template"
    print("[OK] direct/template keep source plans")


def test_dynamic_compare_builds_objective_dag():
    intent = understand_task("比较 Tesla / Figure / Unitree 2026 商业化进度")
    assert select_planning_mode(intent) == "dynamic"
    plan, issues = compose_execution_plan_sync(intent)
    assert plan.planning_mode == "dynamic"
    research = [s for s in plan.steps if s.step_type == "research"]
    assert len(research) >= 3
    assert all(s.objective or s.description for s in research)
    assert all("internet_search" in (s.allowed_tools or []) for s in research)
    compare = next(s for s in research if s.depends_on)
    assert set(compare.depends_on) <= {s.task_id for s in research}
    synth = next(s for s in plan.steps if s.step_type in {"summarize", "generate_markdown"})
    assert synth.depends_on
    ready = ready_research_steps(plan)
    assert len(ready) >= 2
    assert not issues or all("missing_" not in i for i in issues)
    print(f"[OK] dynamic DAG tasks={[s.task_id for s in research]} issues={issues}")


def test_lead_payload_cannot_smuggle_forbidden_web():
    intent = understand_task("比较 Tesla 和 Figure，不要联网，只根据内部数据库")
    policy = parse_source_policy(intent.raw_query)
    plan = plan_from_lead_payload(
        {
            "research_brief": "比较商业化",
            "tasks": [
                {
                    "task_id": "t_tesla",
                    "objective": "Tesla 商业化",
                    "depends_on": [],
                    "allowed_sources": ["web", "db"],
                },
                {
                    "task_id": "t_fig",
                    "objective": "Figure 商业化",
                    "depends_on": [],
                    "allowed_sources": ["web"],
                },
            ],
        },
        intent,
        policy,
    )
    assert plan is not None
    for step in plan.steps:
        if step.step_type == "research":
            assert "internet_search" not in (step.allowed_tools or [])
            assert "execute_sql_query" in (step.allowed_tools or [])
    print("[OK] lead payload strips forbidden web")


def test_plan_patch_respects_policy_and_evaluator():
    intent = understand_task("比较 Tesla / Figure，不要联网，只根据内部数据库")
    plan, _issues = compose_execution_plan_sync(intent)
    for step in plan.steps:
        if step.step_type == "research":
            step.metadata["status"] = "failed"
    patch = build_gap_patch(plan, intent)
    assert patch["add_tasks"]
    updated, issues = apply_plan_patch(plan, patch, intent, max_new_tasks=2)
    assert not issues
    assert updated.plan_version == plan.plan_version + 1
    new_research = [s for s in updated.steps if s.task_id.endswith("_gap")]
    assert new_research
    assert "internet_search" not in (new_research[0].allowed_tools or [])
    assert evaluate_progress(plan, aborted=True) == "abort"
    print("[OK] plan patch + progress")


def test_research_worker_allowlist_and_registry():
    step = PlanStep(
        step_type="research",
        description="Figure 订单",
        allowed_tools=tools_for_sources(["db"]),
    )
    ok, bad = check_unauthorized_tools(step, ["internet_search"], enforce=True)
    assert ok is False and "internet_search" in bad
    ok2, bad2 = check_unauthorized_tools(step, ["execute_sql_query"], enforce=True)
    assert ok2 is True and not bad2
    worker = object()
    agent, mode = resolve_execute_target(
        "research",
        workers={"research": worker},
        main_agent=object(),
        direct_invoke=True,
    )
    assert agent is worker and mode == "direct"
    print("[OK] research allowlist + registry")


if __name__ == "__main__":
    test_source_policy_forbids_web()
    test_direct_and_template_still_source_oriented()
    test_dynamic_compare_builds_objective_dag()
    test_lead_payload_cannot_smuggle_forbidden_web()
    test_plan_patch_respects_policy_and_evaluator()
    test_research_worker_allowlist_and_registry()
    print("\n=== Hybrid planning tests passed ===")
