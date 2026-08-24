"""
Harness Phase 1 单元测试（无需 LLM API）

验证：意图识别、计划生成、校验逻辑、恢复提示、模块导入。
"""

import sys
from pathlib import Path

# 保证从项目根目录可导入 app 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.harness.compressor import ContextCompressor
from app.agent.harness.planner import build_plan, understand_task
from app.agent.harness.recovery import RecoveryManager
from app.agent.harness.state import LoopState, Phase
from app.agent.harness.validator import ResultValidator


def test_imports():
    from app.agent.harness import AgentHarness, HarnessResult, Phase as P
    from app.agent.main_agent import harness, run_deep_agent

    assert AgentHarness is not None
    assert harness is not None
    assert callable(run_deep_agent)
    assert P.UNDERSTAND.value == "understand"
    print("[OK] imports")


def test_understand_and_plan():
    intent = understand_task("搜索2026年AI趋势，结合数据库库存，生成PDF报告")
    assert intent.needs_network
    assert intent.needs_database
    assert intent.deliverable == "pdf"

    plan = build_plan(intent)
    step_types = [s.step_type for s in plan.steps]
    assert "network_search" in step_types
    assert "database_query" in step_types
    assert "generate_markdown" in step_types
    assert "convert_pdf" in step_types
    print(f"[OK] plan: {plan.summary}")


def test_validator_step_and_finalize():
    validator = ResultValidator()
    session_dir = Path(__file__).parents[2] / "output" / "test_harness_val"
    session_dir.mkdir(parents=True, exist_ok=True)

    state = LoopState(session_id="test")
    state.intent = understand_task("搜索AI新闻")
    state.final_content = "这是关于AI趋势的详细研究报告，包含多个来源。"
    state.assistants_called = ["网络搜索助手"]

    outcome = validator.validate_finalize(state, session_dir)
    assert outcome.passed, f"expected pass, got {outcome.reason}"
    print("[OK] validator finalize pass case")

    from app.agent.harness.state import PlanStep, StepResult

    step = PlanStep(step_type="network_search", description="搜索", subagent="网络搜索助手")
    result = StepResult(step_type="network_search", content="x" * 200)
    state.assistants_called = ["网络搜索助手"]
    step_outcome = validator.validate_step(step, result, session_dir, state)
    assert step_outcome.passed
    print("[OK] validator step pass case")

    state2 = LoopState(session_id="test2")
    state2.intent = understand_task("生成Markdown报告", has_uploaded_files=False)
    state2.intent.deliverable = "md"
    state2.final_content = "报告已生成"
    outcome2 = validator.validate_finalize(state2, session_dir)
    assert not outcome2.passed
    assert outcome2.reason == "no_file_generated"
    print("[OK] validator fail case: no_file_generated")


def test_recovery_hint():
    recovery = RecoveryManager()
    state = LoopState(session_id="test")
    state.plan = build_plan(understand_task("查数据库库存"))
    hint = recovery.build_recovery_hint("sql_empty", state)
    assert "list_sql_tables" in hint
    print(f"[OK] recovery hint: {hint[:80]}...")


def test_compressor():
    compressor = ContextCompressor()
    short = "短文本"
    assert compressor.compress_sync(short) == short
    long = "x" * 3000
    compressed = compressor.compress_sync(long, "network_search")
    assert len(compressed) < len(long)
    assert "截断" in compressed or "压缩" in compressed
    print("[OK] compressor")


def test_phase_enum():
    assert Phase.VALIDATE.value == "validate"
    assert Phase.RECOVER.value == "recover"
    print("[OK] phase enum")


def test_context_builder_memory_and_mcp():
    from app.agent.harness.context_builder import ContextBuilder
    from app.mcp.client import bootstrap_mcp_registry

    bootstrap_mcp_registry()
    builder = ContextBuilder()
    memory_ctx = builder.build_memory_context(["机器人行业增速15%"])
    assert "历史研究记忆" in memory_ctx
    tool_ctx = builder.build_tool_context("network_search")
    assert "internet_search" in tool_ctx
    print("[OK] context builder memory + mcp")


def test_memory_store_local():
    import asyncio
    from app.agent.memory.store import MemoryStore

    store = MemoryStore()
    user_id = "test_memory_user"

    async def _run():
        await store.remember(["机器人行业2025年增速约15%"], user_id=user_id)
        recalled = await store.recall("机器人行业", user_id=user_id)
        return recalled

    recalled = asyncio.run(_run())
    assert any("机器人" in r.fact for r in recalled)
    print("[OK] memory store local recall")


def test_mcp_registry():
    from app.mcp.client import bootstrap_mcp_registry
    from app.mcp.registry import mcp_registry

    bootstrap_mcp_registry()
    descriptors = mcp_registry.list_descriptors("generate_markdown")
    assert any(d.name == "generate_markdown" for d in descriptors)
    db_desc = mcp_registry.list_descriptors("database_query")
    assert any(d.name == "execute_sql_query" for d in db_desc)
    kb_desc = mcp_registry.list_descriptors("knowledge_base")
    assert any(d.name == "create_ask_delete" for d in kb_desc)
    print("[OK] mcp registry")


def test_eval_metrics_extended():
    from tests.eval.metrics import TaskEvalResult, build_report, compare_with_baseline

    results = [
        TaskEvalResult(
            task_id="t01",
            query="q",
            mode="dry-run",
            success=True,
            step_success_rate=1.0,
            tool_calls_count=3,
            latency_ms=1000,
            avg_compression_ratio=0.25,
        )
    ]
    report = build_report(results)
    payload = report.to_dict()
    assert "step_success_rate" in payload
    assert "avg_tool_calls" in payload
    comparison = compare_with_baseline(payload, {"task_success_rate": 0.5})
    assert comparison["deltas"]["task_success_rate"] == 0.5
    print("[OK] eval metrics extended")


def test_tracing_optional():
    from app.api.tracing import build_run_config, is_langfuse_enabled

    config = build_run_config("test-session", metadata={"phase": "execute"})
    assert config["configurable"]["thread_id"] == "test-session"
    if is_langfuse_enabled():
        assert "callbacks" in config
    print(f"[OK] tracing enabled={is_langfuse_enabled()}")


if __name__ == "__main__":
    test_imports()
    test_understand_and_plan()
    test_validator_step_and_finalize()
    test_recovery_hint()
    test_compressor()
    test_phase_enum()
    test_context_builder_memory_and_mcp()
    test_memory_store_local()
    test_mcp_registry()
    test_eval_metrics_extended()
    test_tracing_optional()
    print("\n=== All harness unit tests passed ===")
