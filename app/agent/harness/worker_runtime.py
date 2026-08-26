"""
按步工人运行时（兼容入口）。

实现已迁到 app.research.workers：create_agent Leaf + WorkerRegistry。
"""

from __future__ import annotations

from typing import Any

from app.research.workers.prompts import SYNTHESIS_PROMPT_ADDENDUM, WORKER_PROMPT_ADDENDUM
from app.research.workers.registry import (
    DIRECT_STEP_TYPES,
    UnsupportedTaskType,
    build_worker_registry,
    resolve_execute_target,
    worker_tools_for_step,
)

DIRECT_WORKER_STEP_TYPES = DIRECT_STEP_TYPES

__all__ = [
    "DIRECT_STEP_TYPES",
    "DIRECT_WORKER_STEP_TYPES",
    "UnsupportedTaskType",
    "SYNTHESIS_PROMPT_ADDENDUM",
    "WORKER_PROMPT_ADDENDUM",
    "build_worker_graphs",
    "build_worker_registry",
    "resolve_execute_target",
    "worker_tools_for_step",
]


def build_worker_graphs(
    *,
    model: Any,
    checkpointer: Any = None,
    interrupt_on: dict[str, bool] | None = None,
) -> dict[str, Any]:
    registry = build_worker_registry(
        model=model,
        checkpointer=checkpointer,
        interrupt_on=interrupt_on,
    )
    return registry.as_step_map()
