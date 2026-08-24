"""
【Phase 14】Intent / Plan 评测指标

dry-run 门禁：交付物准确率、Plan 校验通过率、Trajectory 相似度。
"""

from __future__ import annotations

from app.agent.harness.planner import build_plan, validate_plan_against_intent
from app.agent.harness.planner_llm import build_plan_for_intent
from app.agent.harness.state import TaskIntent
from app.agent.harness.planner import understand_task


def evaluate_deliverable(expected: str | None, actual: str) -> bool:
    if not expected:
        return True
    return expected == actual


def evaluate_intent_slots(task: dict, intent: TaskIntent) -> bool:
    """槽位级评测（可选 expected_slots）。"""
    expected = task.get("expected_slots") or {}
    if not expected:
        return True
    slots = intent.slots
    if "item_count" in expected and slots.item_count != expected["item_count"]:
        return False
    if "require_citations" in expected and slots.require_citations != expected["require_citations"]:
        return False
    if "output_preference" in expected and slots.output_preference != expected["output_preference"]:
        return False
    return True


def evaluate_plan_validation(intent: TaskIntent, planned_steps: list[str]) -> tuple[bool, list[str]]:
    plan, issues = build_plan_for_intent(intent)
    step_types = [s.step_type for s in plan.steps]
    if planned_steps != step_types:
        return False, issues + ["step_sequence_mismatch"]
    ok, val_issues = validate_plan_against_intent(intent, plan)
    return ok, issues + val_issues


def evaluate_intent_and_plan(task: dict) -> dict:
    """单条 golden task 的 intent/plan 评测摘要。"""
    intent = understand_task(task["query"], task.get("requires_upload", False))
    plan = build_plan(intent)
    planned_steps = [s.step_type for s in plan.steps]
    deliverable_ok = evaluate_deliverable(task.get("expected_deliverable"), intent.deliverable)
    slots_ok = evaluate_intent_slots(task, intent)
    plan_ok, plan_issues = evaluate_plan_validation(intent, planned_steps)
    return {
        "intent": intent,
        "plan": plan,
        "planned_steps": planned_steps,
        "deliverable_ok": deliverable_ok,
        "slots_ok": slots_ok,
        "plan_validation_ok": plan_ok,
        "plan_issues": plan_issues,
        "intent_confidence": intent.intent_confidence,
    }
