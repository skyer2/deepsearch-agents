"""
【Phase 17】真实 token / 成本聚合分析

读取 logs/traces/*.jsonl 中的 llm_usage 与 run_summary，
输出：平均成本、phase 分布、最贵/最便宜 run、倍数与原因线索。

用法：
  uv run python tests/eval/analyze_usage.py
  uv run python tests/eval/analyze_usage.py --logs logs/traces --top 10
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def load_usage_records(log_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(log_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("event") == "llm_usage":
                records.append(item)
    return records


def load_run_summaries(log_dir: Path) -> dict[str, dict[str, Any]]:
    runs: dict[str, dict[str, Any]] = {}
    for path in sorted(log_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("event") == "run_summary":
                session_id = item.get("session_id") or path.stem
                runs[session_id] = item
    return runs


def aggregate_by_run(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_run: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "calls": 0,
            "by_phase": defaultdict(lambda: {"total_tokens": 0, "cost_usd": 0.0, "calls": 0}),
        }
    )
    for rec in records:
        session_id = rec.get("session_id") or "unknown"
        run = by_run[session_id]
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost_usd"):
            run[key] += float(rec.get(key) or 0)
        run["calls"] += 1
        phase = rec.get("phase") or "unknown"
        run["by_phase"][phase]["total_tokens"] += int(rec.get("total_tokens") or 0)
        run["by_phase"][phase]["cost_usd"] += float(rec.get("cost_usd") or 0)
        run["by_phase"][phase]["calls"] += 1
    return dict(by_run)


def print_report(by_run: dict[str, dict[str, Any]], top: int = 10) -> None:
    if not by_run:
        print("未发现 llm_usage 记录。请先跑 live eval 或真实任务。")
        return

    runs = list(by_run.items())
    costs = [r["cost_usd"] for _, r in runs]
    tokens = [r["total_tokens"] for _, r in runs]
    avg_cost = sum(costs) / len(costs)
    avg_tokens = sum(tokens) / len(tokens)

    phase_totals: dict[str, dict[str, float]] = defaultdict(lambda: {"cost": 0.0, "tokens": 0.0, "calls": 0.0})
    for _, run in runs:
        for phase, bucket in run["by_phase"].items():
            phase_totals[phase]["cost"] += bucket["cost_usd"]
            phase_totals[phase]["tokens"] += bucket["total_tokens"]
            phase_totals[phase]["calls"] += bucket["calls"]

    total_cost_all = sum(phase_totals[p]["cost"] for p in phase_totals) or 1e-9

    print("=== LLM Usage / Cost Report ===")
    print(f"Runs analyzed: {len(runs)}")
    print(f"Avg cost per run: ${avg_cost:.4f}")
    print(f"Avg tokens per run: {avg_tokens:,.0f}")
    print()

    print("--- Cost by Phase ---")
    for phase, bucket in sorted(phase_totals.items(), key=lambda x: -x[1]["cost"]):
        share = bucket["cost"] / total_cost_all
        print(
            f"  {phase:15s}  ${bucket['cost']:>8.4f}  "
            f"({share:>5.1%})  tokens={bucket['tokens']:>10,.0f}  calls={int(bucket['calls'])}"
        )
    print()

    sorted_runs = sorted(runs, key=lambda x: -x[1]["cost_usd"])
    most_expensive = sorted_runs[0]
    cheapest = sorted_runs[-1]
    ratio = most_expensive[1]["cost_usd"] / max(cheapest[1]["cost_usd"], 1e-9)

    print("--- Extremes ---")
    print(f"Most expensive: {most_expensive[0]}  ${most_expensive[1]['cost_usd']:.4f}  "
          f"tokens={most_expensive[1]['total_tokens']:,.0f}  calls={most_expensive[1]['calls']}")
    print(f"Cheapest:       {cheapest[0]}  ${cheapest[1]['cost_usd']:.4f}  "
          f"tokens={cheapest[1]['total_tokens']:,.0f}  calls={cheapest[1]['calls']}")
    print(f"Cost spread:    {ratio:.1f}x")
    print()

    print(f"--- Top {min(top, len(sorted_runs))} Runs by Cost ---")
    for session_id, run in sorted_runs[:top]:
        top_phase = max(run["by_phase"].items(), key=lambda x: x[1]["cost_usd"], default=("none", {"cost_usd": 0}))
        print(
            f"  {session_id[:24]:24s}  ${run['cost_usd']:>8.4f}  "
            f"tokens={run['total_tokens']:>9,.0f}  top_phase={top_phase[0]}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze LLM usage/cost from JSONL traces")
    parser.add_argument("--logs", default=str(ROOT / "logs" / "traces"), help="JSONL trace dir")
    parser.add_argument("--top", type=int, default=10, help="Show top N expensive runs")
    args = parser.parse_args()

    log_dir = Path(args.logs)
    if not log_dir.exists():
        print(f"Log dir not found: {log_dir}")
        return

    records = load_usage_records(log_dir)
    by_run = aggregate_by_run(records)
    print_report(by_run, top=args.top)


if __name__ == "__main__":
    main()
