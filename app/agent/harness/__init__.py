"""Agent Harness 运行时层 — 显式 Loop、校验、恢复、护栏。"""

from __future__ import annotations

from typing import Any

__all__ = ["AgentHarness", "HarnessResult", "Phase"]


def __getattr__(name: str) -> Any:
    """延迟导入，避免 `from app.agent.harness.context_builder import ...` 拉起 LLM 栈。"""
    if name == "AgentHarness":
        from app.agent.harness.loop import AgentHarness

        return AgentHarness
    if name in {"HarnessResult", "Phase"}:
        from app.agent.harness.state import HarnessResult, Phase

        return {"HarnessResult": HarnessResult, "Phase": Phase}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
