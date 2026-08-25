"""Phase 19: 窗口卫生、分层预算、压缩保留、证据回读、工作笔记（无需 LLM）。"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.citations import CitationManager
from app.agent.harness.compressor import ContextCompressor
from app.agent.harness.context_budget import ContextBuildSettings, fit_layers_to_token_budget
from app.agent.harness.context_builder import ContextBuilder
from app.agent.harness.retention import apply_retention_patch, extract_numbers, extract_urls
from app.agent.harness.state import ExecutionPlan, LoopState, PlanStep, StepResult, TaskIntent
from app.agent.harness.window_hygiene import (
    TOOL_RESULT_PLACEHOLDER,
    apply_checkpoint_tool_hygiene,
    clear_bulky_tool_results,
    step_graph_thread_id,
)
from app.agent.harness.working_notes import render_working_notes, write_working_notes_file
from app.config.loader import reload_harness_config


def test_fresh_thread_ids_differ_per_step():
    a = step_graph_thread_id("sess-a", 0)
    b = step_graph_thread_id("sess-a", 1)
    assert a != b
    assert a.startswith("sess-a:step:")
    print("[OK] fresh thread ids")


def test_clear_bulky_tool_results():
    messages = [
        SimpleNamespace(type="human", content="q"),
        SimpleNamespace(type="tool", content="x" * 800),
        SimpleNamespace(type="tool", content="keep-me-short"),
        SimpleNamespace(type="tool", content="y" * 900),
    ]
    updated, cleared = clear_bulky_tool_results(messages, keep_last=1, max_chars=500)
    assert cleared == 1
    assert updated[1].content == TOOL_RESULT_PLACEHOLDER
    assert updated[3].content.startswith("y")
    print("[OK] tool result clearing")


def test_hygiene_writes_placeholder_back_to_graph():
    class DummyAgent:
        def __init__(self):
            self.messages = [
                SimpleNamespace(type="human", content="q", id="h1"),
                SimpleNamespace(type="tool", content="x" * 800, id="t1"),
                SimpleNamespace(type="tool", content="y" * 900, id="t2"),
            ]
            self.updated = None

        async def aget_state(self, _config):
            return SimpleNamespace(values={"messages": self.messages})

        async def aupdate_state(self, _config, values):
            self.updated = values

    dummy = DummyAgent()
    cleared = asyncio.run(
        apply_checkpoint_tool_hygiene(dummy, {"configurable": {"thread_id": "t"}})
    )
    assert cleared == 1
    assert dummy.updated is not None
    assert dummy.updated["messages"][0].content == TOOL_RESULT_PLACEHOLDER
    assert dummy.updated["messages"][0].id == "t1"
    print("[OK] checkpoint tool hygiene")


def test_retention_patch_keeps_url_and_number():
    original = (
        "IDC 报告称 2026 年全球市场规模约 15.5 亿美元，详见 https://example.com/idc-report "
        + ("噪音段落。" * 80)
    )
    compressed = "市场规模有所增长。" + ("摘要填充。" * 40)
    patched, meta = apply_retention_patch(original, compressed)
    assert meta["retention_patched"] is True
    assert "https://example.com/idc-report" in patched
    assert "15.5" in patched
    assert extract_urls(original)
    assert extract_numbers(original)
    print("[OK] retention patch")


def test_compressor_retention_without_llm():
    text = (
        ("填充内容。" * 500)
        + "来源 https://example.com/a 显示增速 28.3%。"
    )
    compressor = ContextCompressor(model=None, enabled=False, threshold_chars=100)
    compressed, meta = asyncio.run(compressor.compress(text, step_type="network_search"))
    assert meta["method"].startswith("truncate")
    assert "https://example.com/a" in compressed
    assert "28.3" in compressed
    print("[OK] compressor retention")


def test_layer_priority_keeps_current_step():
    layers = {
        "task_query": "Q" * 8000,
        "tools": "TOOLS " * 400,
        "step": "KEEP_STEP_INSTRUCTION only finish this step",
        "recovery": "please add citations",
    }
    message, metrics = fit_layers_to_token_budget(layers, 200)
    assert "KEEP_STEP_INSTRUCTION" in message
    assert metrics.used_layer_priority is True
    print("[OK] layer priority eviction")


def test_working_notes_and_file(tmp_path: Path):
    results = [
        StepResult(
            step_type="network_search",
            content="ok",
            metadata={
                "worker_payload": {
                    "facts": ["2026 市场规模约 15 亿美元"],
                    "sources": ["https://example.com/idc"],
                }
            },
        )
    ]
    notes = render_working_notes(task_query="调研机器人", step_results=results)
    assert "15 亿" in notes
    assert "https://example.com/idc" in notes
    path = write_working_notes_file(tmp_path, notes)
    assert path.exists()
    print("[OK] working notes")


def test_refresh_writes_evidence_json(tmp_path: Path):
    mgr = CitationManager()
    mgr.bind_worker_facts(
        0,
        "network_search",
        ["Market size is 15.5 billion USD in 2026"],
        ["https://example.com/idc"],
    )
    notes = render_working_notes(task_query="调研机器人", step_results=[])
    write_working_notes_file(tmp_path, notes)
    path = mgr.save_evidence_json(tmp_path)
    assert path is not None and path.exists()
    assert (tmp_path / "working_notes.md").exists()
    assert "15.5" in path.read_text(encoding="utf-8")
    assert "【可回读证据" in mgr.build_lookup_block()
    print("[OK] incremental evidence.json")


def test_fact_source_binding_and_lookup():
    mgr = CitationManager()
    bound = mgr.bind_worker_facts(
        0,
        "network_search",
        ["Market size is 15.5 billion USD in 2026"],
        ["https://example.com/idc"],
    )
    assert bound
    lookup = mgr.build_lookup_block()
    assert "15.5" in lookup
    report = mgr.build_cited_report(
        "Analysts say Market size is 15.5 billion USD in 2026, and competition increased."
    )
    assert "[1]" in report
    metrics = mgr.compute_metrics(report)
    assert metrics["registered_sources"] >= 1
    assert metrics["citation_coverage_rate"] > 0
    assert metrics["numeric_citation_coverage"] > 0

    trailing = mgr.compute_metrics(
        "Analysts say Market size is 15.5 billion USD in 2026, and competition increased. [1]\n\n"
        "## 参考文献\n[1] (网络) https://example.com/idc"
    )
    assert trailing["numeric_citation_coverage"] > 0
    print("[OK] fact-source binding")


def test_synthesis_injects_evidence_lookup():
    settings = ContextBuildSettings(wrap_untrusted_external=False)
    builder = ContextBuilder(settings)
    state = LoopState(
        session_id="s1",
        intent=TaskIntent(raw_query="q", summary="s", needs_network=True, deliverable="md"),
        plan=ExecutionPlan(
            steps=[PlanStep(step_type="generate_markdown", description="写报告")],
            summary="plan",
        ),
    )
    state.evidence_lookup_block = "    EVIDENCE_LOOKUP_BLOCK [1] src-1 https://e.com"
    state.working_notes = "    WORKING_NOTES_BLOCK task q"
    msg = builder.build_step_message(
        "write report",
        state,
        state.plan.steps[0],
        0,
        "output/session_s1",
    )
    assert "EVIDENCE_LOOKUP_BLOCK" in msg
    assert "WORKING_NOTES_BLOCK" in msg
    print("[OK] synthesis evidence lookup")


def test_retrieval_skips_evidence_lookup():
    settings = ContextBuildSettings(wrap_untrusted_external=False)
    builder = ContextBuilder(settings)
    state = LoopState(
        session_id="s1",
        plan=ExecutionPlan(
            steps=[PlanStep(step_type="network_search", description="搜索")],
            summary="plan",
        ),
    )
    state.evidence_lookup_block = "EVIDENCE_LOOKUP_BLOCK should not appear on retrieval"
    msg = builder.build_step_message(
        "search",
        state,
        state.plan.steps[0],
        0,
        "output/session_s1",
    )
    assert "EVIDENCE_LOOKUP_BLOCK" not in msg
    print("[OK] retrieval skips evidence lookup")


def test_config_phase19():
    cfg = reload_harness_config()
    assert cfg.context_fresh_thread_per_step is True
    assert cfg.context_layer_priority_eviction is True
    assert cfg.compression_retention_check is True
    assert cfg.context_clear_bulky_tool_results is True
    print("[OK] config phase19")


if __name__ == "__main__":
    test_fresh_thread_ids_differ_per_step()
    test_clear_bulky_tool_results()
    test_hygiene_writes_placeholder_back_to_graph()
    test_retention_patch_keeps_url_and_number()
    test_compressor_retention_without_llm()
    test_layer_priority_keeps_current_step()
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        test_working_notes_and_file(Path(tmp))
        test_refresh_writes_evidence_json(Path(tmp))
    test_fact_source_binding_and_lookup()
    test_synthesis_injects_evidence_lookup()
    test_retrieval_skips_evidence_lookup()
    test_config_phase19()
    print("\n=== Phase 19 context tests passed ===")
