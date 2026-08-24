"""Phase 2 eval dry-run smoke test."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.eval.metrics import build_report
from tests.eval.run_eval import load_tasks, run_dry_eval


def test_dry_eval_runs():
    tasks = load_tasks(ROOT / "tests" / "eval" / "tasks.jsonl")
    assert len(tasks) == 11
    results = run_dry_eval(tasks)
    report = build_report(results)
    assert report.total == 11
    assert report.task_success_rate > 0
    assert report.plan_validation_pass_rate == 1.0
    payload = report.to_dict()
    assert "step_success_rate" in payload
    assert "avg_tool_calls" in payload
    print(
        f"[OK] dry eval TSR={report.task_success_rate:.1%} "
        f"SSR={report.step_success_rate:.1%}"
    )


if __name__ == "__main__":
    test_dry_eval_runs()
