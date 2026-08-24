"""Phase 17: LLM usage / cost tracking 测试（无需真实 API）。"""

import json
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.usage_tracker import (
    LLMCallRecord,
    UsageTracker,
    estimate_cost_usd,
    get_pricing,
    set_llm_phase,
    set_llm_session,
)


class _FakeMessage:
    def __init__(self, usage: dict, model: str = "qwen-max"):
        self.usage_metadata = usage
        self.response_metadata = {"model_name": model}


class _FakeGeneration:
    def __init__(self, usage: dict, model: str = "qwen-max"):
        self.message = _FakeMessage(usage, model)


class _FakeLLMResult:
    def __init__(self, usage: dict, model: str = "qwen-max"):
        self.llm_output = {}
        self.generations = [[_FakeGeneration(usage, model)]]


def test_estimate_cost():
    cost = estimate_cost_usd("qwen-max", 1_000_000, 1_000_000)
    assert cost > 0
    pricing = get_pricing("qwen-max")
    assert pricing["input"] > 0
    print("[OK] estimate cost")


def test_usage_tracker_record_and_summary():
    tracker = UsageTracker()
    session_id = "test_usage_session"
    set_llm_session(session_id)
    set_llm_phase("execute")

    rec = LLMCallRecord(
        session_id=session_id,
        phase="execute",
        model="qwen-max",
        prompt_tokens=1000,
        completion_tokens=200,
        total_tokens=1200,
        cost_usd=0.002,
    )
    tracker.record(rec)

    summary = tracker.session_summary(session_id)
    assert summary["total"]["total_tokens"] == 1200
    assert summary["by_phase"]["execute"]["calls"] == 1
    tracker.clear_session(session_id)
    print("[OK] usage tracker record/summary")


def test_callback_extracts_usage():
    from app.agent.harness.usage_tracker import UsageTrackingCallback

    tracker = UsageTracker()
    import app.agent.harness.usage_tracker as mod

    old = mod._tracker
    mod._tracker = tracker
    try:
        session_id = "test_callback_session"
        cb = UsageTrackingCallback(session_id=session_id, phase="plan")
        fake = _FakeLLMResult({"input_tokens": 500, "output_tokens": 100, "total_tokens": 600})
        cb.on_llm_end(fake, run_id="r1")
        summary = tracker.session_summary(session_id)
        assert summary["total"]["total_tokens"] == 600
        assert summary["by_phase"]["plan"]["prompt_tokens"] == 500
        print("[OK] callback extracts usage")
    finally:
        mod._tracker = old


def test_tracked_ainvoke_injects_phase_callback():
    from app.agent.harness.usage_tracker import tracked_ainvoke
    import app.agent.harness.usage_tracker as mod

    class FakeModel:
        async def ainvoke(self, prompt, config=None):
            for callback in (config or {}).get("callbacks", []):
                callback.on_llm_end(
                    _FakeLLMResult(
                        {
                            "input_tokens": 40,
                            "output_tokens": 10,
                            "total_tokens": 50,
                        }
                    )
                )
            return type("Response", (), {"content": "ok"})()

    tracker = UsageTracker()
    old = mod._tracker
    mod._tracker = tracker
    try:
        response = asyncio.run(
            tracked_ainvoke(
                FakeModel(),
                "prompt",
                session_id="direct-session",
                phase="compress",
            )
        )
        assert response.content == "ok"
        summary = tracker.session_summary("direct-session")
        assert summary["total"]["total_tokens"] == 50
        assert summary["by_phase"]["compress"]["calls"] == 1
        assert summary["total"]["missing_usage_calls"] == 0
    finally:
        mod._tracker = old


if __name__ == "__main__":
    test_estimate_cost()
    test_usage_tracker_record_and_summary()
    test_callback_extracts_usage()
    print("\n=== Phase 17 usage tracking tests passed ===")
