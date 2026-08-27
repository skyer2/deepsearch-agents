"""
Harness 健康检查

GET /health 返回依赖状态，供部署探针与面试演示。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Literal

import requests
from dotenv import find_dotenv, load_dotenv

from app.api.tracing import is_langfuse_enabled
from app.config.loader import get_harness_config

load_dotenv(find_dotenv())

DependencyStatus = Literal["ok", "degraded", "down", "disabled"]


def _status_from_bool(ok: bool, configured: bool = True) -> DependencyStatus:
    if not configured:
        return "disabled"
    return "ok" if ok else "down"


async def _check_mysql(timeout: float = 2.0) -> DependencyStatus:
    if not os.getenv("MYSQL_USER") or not os.getenv("MYSQL_DATABASE"):
        return "disabled"

    def _probe() -> bool:
        try:
            from mysql.connector import connect

            conn = connect(
                host=os.getenv("MYSQL_HOST", "localhost"),
                port=int(os.getenv("MYSQL_PORT", "3306")),
                user=os.getenv("MYSQL_USER"),
                password=os.getenv("MYSQL_PASSWORD"),
                database=os.getenv("MYSQL_DATABASE"),
                connection_timeout=int(timeout),
            )
            conn.close()
            return True
        except Exception:
            return False

    return _status_from_bool(await asyncio.to_thread(_probe))


async def _check_llm(timeout: float = 3.0) -> DependencyStatus:
    if not os.getenv("OPENAI_API_KEY"):
        return "down"

    def _probe() -> bool:
        try:
            from openai import OpenAI

            client = OpenAI(
                api_key=os.getenv("OPENAI_API_KEY"),
                base_url=os.getenv("OPENAI_BASE_URL"),
                timeout=timeout,
            )
            client.models.list()
            return True
        except Exception:
            return False

    return _status_from_bool(await asyncio.to_thread(_probe))


def _check_tavily() -> DependencyStatus:
    return _status_from_bool(bool(os.getenv("TAVILY_API_KEY")))


async def _check_ragflow(timeout: float = 2.0) -> DependencyStatus:
    base_url = os.getenv("RAGFLOW_API_URL", "").rstrip("/")
    if not base_url:
        return "disabled"

    def _probe() -> DependencyStatus:
        try:
            resp = requests.get(f"{base_url}/v1/system/status", timeout=timeout)
            if resp.status_code < 500:
                return "ok"
            return "degraded"
        except requests.Timeout:
            return "degraded"
        except Exception:
            return "down"

    return await asyncio.to_thread(_probe)


def _check_langfuse() -> DependencyStatus:
    config = get_harness_config()
    if not config.langfuse_enabled:
        return "disabled"
    return _status_from_bool(is_langfuse_enabled())


def _check_mem0() -> DependencyStatus:
    config = get_harness_config()
    mem0_enabled = (
        os.getenv("MEM0_ENABLED", "false").lower() == "true"
        or config.memory_provider == "mem0"
    )
    if not mem0_enabled:
        return "disabled"
    try:
        from mem0 import Memory  # noqa: F401

        return "ok"
    except Exception:
        return "degraded"


async def collect_health() -> dict[str, Any]:
    config = get_harness_config()
    llm, mysql, ragflow = await asyncio.gather(
        _check_llm(),
        _check_mysql(),
        _check_ragflow(),
    )
    dependencies = {
        "llm": llm,
        "mysql": mysql,
        "tavily": _check_tavily(),
        "ragflow": ragflow,
        "langfuse": _check_langfuse(),
        "mem0": _check_mem0(),
    }

    critical = [dependencies["llm"]]
    if any(status == "down" for status in critical):
        overall = "degraded"
    elif any(status in {"degraded", "down"} for status in dependencies.values()):
        overall = "degraded"
    else:
        overall = "ok"

    return {
        "status": overall,
        "dependencies": dependencies,
        "version": config.version,
        "harness_config": {
            "max_retries": config.max_retries,
            "jsonl_log_enabled": config.jsonl_log_enabled,
            "validation_strict_mode": config.validation_strict_mode,
            "orchestration": {
                "parallel_retrieval_enabled": config.parallel_retrieval_enabled,
                "max_parallel_workers": config.max_parallel_workers,
                "step_timeout_sec": config.step_timeout_sec,
                "enforce_subagent_binding": config.enforce_subagent_binding,
                "step_checkpoint_enabled": config.step_checkpoint_enabled,
                "structured_output_retry": config.structured_output_retry,
                "synthesis_use_evidence_digest": config.synthesis_use_evidence_digest,
                "direct_worker_invoke": getattr(config, "direct_worker_invoke", True),
                "persist_loop_state": getattr(config, "persist_loop_state", True),
                "graph_runtime_enabled": getattr(config, "graph_runtime_enabled", False),
            },
            "planner": {
                "llm_enabled": config.planner_llm_enabled,
                "llm_confirm_enabled": config.planner_llm_confirm_enabled,
                "clarification_enabled": config.planner_clarification_enabled,
                "clarification_auto_resolve": config.planner_clarification_auto_resolve,
                "plan_review_min_confidence": config.planner_plan_review_min_confidence,
            },
            "observability": {
                "jsonl_log_enabled": config.jsonl_log_enabled,
                "langfuse_enabled": config.langfuse_enabled,
                "metrics_enabled": config.metrics_enabled,
                "metrics_window_hours": config.metrics_window_hours,
                "prometheus_enabled": config.prometheus_enabled,
            },
            "eval": {
                "heuristic_judge_enabled": config.eval_heuristic_judge_enabled,
                "llm_judge_enabled": config.eval_llm_judge_enabled,
                "intent_deliverable_min_accuracy": config.eval_intent_deliverable_min_accuracy,
                "plan_validation_min_rate": config.eval_plan_validation_min_rate,
            },
            "tools": {
                "fail_closed": config.tools_fail_closed,
                "sql_select_only": config.tools_sql_select_only,
                "enforce_step_policy": config.tools_enforce_step_policy,
            },
            "context": {
                "max_step_message_tokens": config.context_max_step_message_tokens,
                "prior_results_max_steps": config.context_prior_results_max_steps,
                "wrap_untrusted_external": config.context_wrap_untrusted_external,
                "compression_threshold_chars": config.compression_threshold_chars,
                "fresh_thread_per_step": config.context_fresh_thread_per_step,
                "layer_priority_eviction": config.context_layer_priority_eviction,
                "working_notes_enabled": config.context_working_notes_enabled,
                "evidence_lookup_enabled": config.context_evidence_lookup_enabled,
                "clear_bulky_tool_results": config.context_clear_bulky_tool_results,
                "retention_check": config.compression_retention_check,
                "jit_retrieval_enabled": getattr(config, "context_jit_retrieval_enabled", True),
                "research_brief_as_anchor": getattr(config, "context_research_brief_as_anchor", True),
                "reversible_compression": getattr(config, "context_reversible_compression", True),
                "tool_output_contract": getattr(config, "context_tool_output_contract", True),
                "token_budget_model": getattr(config, "token_budget_model", "glm-5.2"),
                "token_context_window": getattr(config, "token_context_window", 128000),
                "token_stage_budgets": dict(getattr(config, "token_stage_budgets", None) or {}),
            },
            "memory": {
                "enabled": config.memory_enabled,
                "provider": config.memory_provider,
                "recall_top_k": config.memory_recall_top_k,
                "ttl_days": config.memory_ttl_days,
                "wrap_untrusted": config.memory_wrap_untrusted,
                "embedding_enabled": config.memory_embedding_enabled,
                "step_incremental_enabled": config.memory_step_incremental_enabled,
                "project_scope_enabled": config.memory_project_scope_enabled,
                "require_explicit_identity": config.memory_require_explicit_identity,
                "synthesis_min_trust": config.memory_synthesis_min_trust,
                "source_ledger_enabled": config.memory_source_ledger_enabled,
                "consolidation_enabled": config.memory_consolidation_enabled,
            },
            "guardrails": {
                "max_tool_calls": config.max_tool_calls,
                "max_total_tokens": config.max_total_tokens,
                "max_run_sec": config.max_run_sec,
                "max_replan_count": config.max_replan_count,
                "max_plan_steps": config.max_plan_steps,
            },
            "mcp": {
                "enabled": config.mcp_enabled,
                "call_timeout_sec": config.mcp_call_timeout_sec,
                "max_retries": config.mcp_max_retries,
            },
        },
    }
