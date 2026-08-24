"""
Harness 配置加载器

从 app/config/harness.yml 读取配置，环境变量可覆盖关键开关。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_CONFIG_PATH = Path(__file__).resolve().parent / "harness.yml"
_cached_config: "HarnessConfig | None" = None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass
class HarnessConfig:
    version: str = "1.0.0-harness"
    max_retries: int = 2

    compression_enabled: bool = True
    compression_max_tokens: int = 500
    compression_threshold_chars: int = 2000
    compression_model: str = "qwen-turbo"

    context_max_step_message_tokens: int = 12_000
    context_prior_results_max_steps: int = 5
    context_prior_snippet_max_chars: int = 400
    context_wrap_untrusted_external: bool = True
    context_layer_budget_log_enabled: bool = True

    memory_provider: str = "sqlite"
    memory_enabled: bool = True
    memory_recall_top_k: int = 5
    memory_ttl_days: int = 90
    memory_max_facts_per_remember: int = 5
    memory_min_fact_chars: int = 12
    memory_wrap_untrusted: bool = True
    memory_embedding_enabled: bool = True
    memory_recall_keyword_weight: float = 0.4
    memory_recall_embedding_weight: float = 0.6
    memory_merge_jaccard_threshold: float = 0.55
    memory_merge_embedding_threshold: float = 0.88
    memory_pii_redact_enabled: bool = True
    memory_step_incremental_enabled: bool = True
    memory_remember_on_partial: bool = False
    memory_project_scope_enabled: bool = True
    memory_require_explicit_identity: bool = False
    memory_min_recall_trust: str = "untrusted"
    memory_synthesis_min_trust: str = "derived"
    memory_require_provenance_for_step_write: bool = True
    memory_source_ledger_enabled: bool = True
    memory_source_ledger_max_inject: int = 8
    memory_step_recall_enabled: bool = True
    memory_step_recall_top_k: int = 3
    memory_consolidation_enabled: bool = True
    memory_consolidation_async: bool = True
    memory_consolidation_half_life_days: int = 30
    memory_consolidation_min_confidence: float = 0.25
    memory_consolidation_promote_min_sessions: int = 2
    memory_purge_after_days: int = 180

    validation_strict_mode: bool = False

    langfuse_enabled: bool = True
    jsonl_log_enabled: bool = True
    jsonl_log_dir: str = "logs/traces"
    metrics_enabled: bool = True
    metrics_window_hours: int = 168
    prometheus_enabled: bool = True

    max_total_tokens: int = 100_000
    max_tool_calls: int = 20
    max_run_sec: int = 600
    max_replan_count: int = 3
    max_plan_steps: int = 12

    mcp_enabled: bool = False
    mcp_tavily_enabled: bool = False
    mcp_mysql_enabled: bool = False
    mcp_ragflow_enabled: bool = False
    mcp_transport: str = "stdio"
    mcp_call_timeout_sec: int = 30
    mcp_max_retries: int = 1
    mcp_pool_enabled: bool = True
    mcp_sync_on_startup: bool = True
    mcp_files_enabled: bool = False
    mcp_tasks_enabled: bool = True
    mcp_gateway_rate_limit_per_minute: int = 120
    mcp_gateway_oauth_token: str = ""

    tools_fail_closed: bool = True
    tools_sql_select_only: bool = True
    tools_enforce_step_policy: bool = True

    hitl_enabled: bool = True
    hitl_timeout_sec: int = 600
    hitl_interrupt_on: dict[str, bool] = field(default_factory=dict)
    hitl_step_gate_types: list[str] = field(default_factory=list)
    hitl_plan_review_enabled: bool = True
    hitl_allow_edit: bool = True
    hitl_allow_replan: bool = True

    citations_enabled: bool = True
    citations_min_coverage_rate: float = 0.2

    eval_trajectory_min_similarity: float = 0.6
    eval_heuristic_judge_enabled: bool = True
    eval_heuristic_judge_min_score: float = 0.6
    eval_llm_judge_enabled: bool = False
    eval_structured_output_min_rate: float = 0.8
    eval_intent_deliverable_min_accuracy: float = 0.9
    eval_plan_validation_min_rate: float = 1.0
    usage_tracking_enabled: bool = True

    # 【Phase 7】多 Agent 编排
    parallel_retrieval_enabled: bool = True
    max_parallel_workers: int = 3
    step_timeout_sec: int = 120
    enforce_subagent_binding: bool = True
    step_checkpoint_enabled: bool = True
    resume_checkpoint: bool = True
    supervisor_temperature: float = 0.1
    structured_output_retry: bool = True
    require_structured_worker_output: bool = True
    synthesis_use_evidence_digest: bool = True

    planner_llm_confirm_enabled: bool = False
    planner_llm_confirm_min_confidence: float = 0.5
    # 【Phase 14】工业级 Planner
    planner_llm_enabled: bool = True
    planner_llm_min_confidence: float = 0.5
    planner_clarification_enabled: bool = True
    planner_clarification_auto_resolve: bool = True
    planner_plan_review_min_confidence: float = 0.75

    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def compression_max_chars(self) -> int:
        return max(200, self.compression_max_tokens * 4)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("harness", data)


def load_harness_config(path: Path | None = None) -> HarnessConfig:
    path = path or _CONFIG_PATH
    raw = _load_yaml(path)
    compression = raw.get("compression", {})
    context_cfg = raw.get("context", {})
    memory = raw.get("memory", {})
    validation = raw.get("validation", {})
    observability = raw.get("observability", {})
    budget = raw.get("budget", {})
    mcp = raw.get("mcp", {})
    hitl = raw.get("hitl", {})
    citations = raw.get("citations", {})
    eval_cfg = raw.get("eval", {})
    orch = raw.get("orchestration", {})
    planner_cfg = raw.get("planner", {})
    default_interrupt = {
        "generate_markdown": True,
        "convert_md_to_pdf": True,
        "read_file_content": False,
    }
    interrupt_raw = hitl.get("interrupt_on", default_interrupt)

    config = HarnessConfig(
        version=str(raw.get("version", "1.0.0-harness")),
        max_retries=int(raw.get("max_retries", 2)),
        compression_enabled=_env_bool(
            "HARNESS_LLM_COMPRESSION",
            bool(compression.get("enabled", True)),
        ),
        compression_max_tokens=int(compression.get("max_tokens", 500)),
        compression_threshold_chars=int(
            compression.get("threshold_chars", 2000)
        ),
        compression_model=str(
            os.getenv("LLM_COMPRESSION_MODEL", compression.get("model", "qwen-turbo"))
        ),
        context_max_step_message_tokens=int(
            os.getenv(
                "HARNESS_CONTEXT_MAX_STEP_TOKENS",
                context_cfg.get("max_step_message_tokens", 12_000),
            )
        ),
        context_prior_results_max_steps=int(
            context_cfg.get("prior_results_max_steps", 5)
        ),
        context_prior_snippet_max_chars=int(
            context_cfg.get("prior_snippet_max_chars", 400)
        ),
        context_wrap_untrusted_external=_env_bool(
            "HARNESS_CONTEXT_WRAP_UNTRUSTED",
            bool(context_cfg.get("wrap_untrusted_external", True)),
        ),
        context_layer_budget_log_enabled=_env_bool(
            "HARNESS_CONTEXT_LAYER_BUDGET_LOG",
            bool(context_cfg.get("layer_budget_log_enabled", True)),
        ),
        memory_provider=str(
            os.getenv(
                "HARNESS_MEMORY_PROVIDER",
                "mem0" if _env_bool("MEM0_ENABLED", False) else memory.get("provider", "local"),
            )
        ),
        memory_recall_top_k=int(memory.get("recall_top_k", 5)),
        memory_enabled=_env_bool(
            "HARNESS_MEMORY_ENABLED",
            bool(memory.get("enabled", True)),
        ),
        memory_ttl_days=int(memory.get("ttl_days", 90)),
        memory_max_facts_per_remember=int(memory.get("max_facts_per_remember", 5)),
        memory_min_fact_chars=int(memory.get("min_fact_chars", 12)),
        memory_wrap_untrusted=_env_bool(
            "HARNESS_MEMORY_WRAP_UNTRUSTED",
            bool(memory.get("wrap_untrusted", True)),
        ),
        memory_embedding_enabled=_env_bool(
            "HARNESS_MEMORY_EMBEDDING_ENABLED",
            bool(memory.get("embedding_enabled", True)),
        ),
        memory_recall_keyword_weight=float(
            memory.get("recall_keyword_weight", 0.4)
        ),
        memory_recall_embedding_weight=float(
            memory.get("recall_embedding_weight", 0.6)
        ),
        memory_merge_jaccard_threshold=float(
            memory.get("merge_jaccard_threshold", 0.55)
        ),
        memory_merge_embedding_threshold=float(
            memory.get("merge_embedding_threshold", 0.88)
        ),
        memory_pii_redact_enabled=_env_bool(
            "HARNESS_MEMORY_PII_REDACT",
            bool(memory.get("pii_redact_enabled", True)),
        ),
        memory_step_incremental_enabled=_env_bool(
            "HARNESS_MEMORY_STEP_INCREMENTAL",
            bool(memory.get("step_incremental_enabled", True)),
        ),
        memory_remember_on_partial=_env_bool(
            "HARNESS_MEMORY_REMEMBER_ON_PARTIAL",
            bool(memory.get("remember_on_partial", False)),
        ),
        memory_project_scope_enabled=_env_bool(
            "HARNESS_MEMORY_PROJECT_SCOPE",
            bool(memory.get("project_scope_enabled", True)),
        ),
        memory_require_explicit_identity=_env_bool(
            "HARNESS_MEMORY_REQUIRE_IDENTITY",
            bool(memory.get("require_explicit_identity", False)),
        ),
        memory_min_recall_trust=str(memory.get("min_recall_trust", "untrusted")),
        memory_synthesis_min_trust=str(memory.get("synthesis_min_trust", "derived")),
        memory_require_provenance_for_step_write=_env_bool(
            "HARNESS_MEMORY_REQUIRE_PROVENANCE",
            bool(memory.get("require_provenance_for_step_write", True)),
        ),
        memory_source_ledger_enabled=_env_bool(
            "HARNESS_MEMORY_SOURCE_LEDGER",
            bool(memory.get("source_ledger_enabled", True)),
        ),
        memory_source_ledger_max_inject=int(memory.get("source_ledger_max_inject", 8)),
        memory_step_recall_enabled=_env_bool(
            "HARNESS_MEMORY_STEP_RECALL",
            bool(memory.get("step_recall_enabled", True)),
        ),
        memory_step_recall_top_k=int(memory.get("step_recall_top_k", 3)),
        memory_consolidation_enabled=_env_bool(
            "HARNESS_MEMORY_CONSOLIDATION",
            bool(memory.get("consolidation_enabled", True)),
        ),
        memory_consolidation_async=_env_bool(
            "HARNESS_MEMORY_CONSOLIDATION_ASYNC",
            bool(memory.get("consolidation_async", True)),
        ),
        memory_consolidation_half_life_days=int(
            memory.get("consolidation_half_life_days", 30)
        ),
        memory_consolidation_min_confidence=float(
            memory.get("consolidation_min_confidence", 0.25)
        ),
        memory_consolidation_promote_min_sessions=int(
            memory.get("consolidation_promote_min_sessions", 2)
        ),
        memory_purge_after_days=int(memory.get("purge_after_days", 180)),
        validation_strict_mode=_env_bool(
            "HARNESS_VALIDATION_STRICT",
            bool(validation.get("strict_mode", False)),
        ),
        langfuse_enabled=_env_bool(
            "HARNESS_LANGFUSE_ENABLED",
            bool(observability.get("langfuse_enabled", True)),
        ),
        jsonl_log_enabled=_env_bool(
            "HARNESS_JSONL_LOG_ENABLED",
            bool(observability.get("jsonl_log_enabled", True)),
        ),
        jsonl_log_dir=str(observability.get("jsonl_log_dir", "logs/traces")),
        metrics_enabled=_env_bool(
            "HARNESS_METRICS_ENABLED",
            bool(observability.get("metrics_enabled", True)),
        ),
        metrics_window_hours=int(
            os.getenv(
                "HARNESS_METRICS_WINDOW_HOURS",
                observability.get("metrics_window_hours", 168),
            )
        ),
        prometheus_enabled=_env_bool(
            "HARNESS_PROMETHEUS_ENABLED",
            bool(observability.get("prometheus_enabled", True)),
        ),
        max_total_tokens=int(budget.get("max_total_tokens", 100_000)),
        max_tool_calls=int(budget.get("max_tool_calls", 20)),
        max_run_sec=int(
            os.getenv("HARNESS_MAX_RUN_SEC", budget.get("max_run_sec", 600))
        ),
        max_replan_count=int(budget.get("max_replan_count", 3)),
        max_plan_steps=int(budget.get("max_plan_steps", 12)),
        mcp_enabled=_env_bool(
            "HARNESS_MCP_ENABLED",
            bool(mcp.get("enabled", False)),
        ),
        mcp_tavily_enabled=_env_bool(
            "HARNESS_MCP_TAVILY",
            bool(mcp.get("tavily_enabled", mcp.get("enabled", False))),
        ),
        mcp_mysql_enabled=_env_bool(
            "HARNESS_MCP_MYSQL",
            bool(mcp.get("mysql_enabled", mcp.get("enabled", False))),
        ),
        mcp_ragflow_enabled=_env_bool(
            "HARNESS_MCP_RAGFLOW",
            bool(mcp.get("ragflow_enabled", mcp.get("enabled", False))),
        ),
        mcp_transport=str(mcp.get("transport", "stdio")),
        mcp_call_timeout_sec=int(
            os.getenv("HARNESS_MCP_CALL_TIMEOUT_SEC", mcp.get("call_timeout_sec", 30))
        ),
        mcp_max_retries=int(
            os.getenv("HARNESS_MCP_MAX_RETRIES", mcp.get("max_retries", 1))
        ),
        mcp_pool_enabled=_env_bool(
            "HARNESS_MCP_POOL_ENABLED",
            bool(mcp.get("pool_enabled", True)),
        ),
        mcp_sync_on_startup=_env_bool(
            "HARNESS_MCP_SYNC_ON_STARTUP",
            bool(mcp.get("sync_on_startup", True)),
        ),
        mcp_files_enabled=_env_bool(
            "HARNESS_MCP_FILES",
            bool(mcp.get("files_enabled", mcp.get("enabled", False))),
        ),
        mcp_tasks_enabled=_env_bool(
            "HARNESS_MCP_TASKS_ENABLED",
            bool(mcp.get("tasks_enabled", True)),
        ),
        mcp_gateway_rate_limit_per_minute=int(
            os.getenv(
                "HARNESS_MCP_GATEWAY_RATE_LIMIT",
                mcp.get("gateway_rate_limit_per_minute", 120),
            )
        ),
        mcp_gateway_oauth_token=str(
            os.getenv("HARNESS_MCP_GATEWAY_OAUTH_TOKEN", mcp.get("gateway_oauth_token", ""))
        ),
        tools_fail_closed=_env_bool(
            "HARNESS_TOOLS_FAIL_CLOSED",
            bool(raw.get("tools", {}).get("fail_closed", True)),
        ),
        tools_sql_select_only=_env_bool(
            "HARNESS_SQL_SELECT_ONLY",
            bool(raw.get("tools", {}).get("sql_select_only", True)),
        ),
        tools_enforce_step_policy=_env_bool(
            "HARNESS_TOOLS_ENFORCE_STEP_POLICY",
            bool(raw.get("tools", {}).get("enforce_step_policy", True)),
        ),
        hitl_enabled=_env_bool("HARNESS_HITL_ENABLED", bool(hitl.get("enabled", True))),
        hitl_timeout_sec=int(hitl.get("timeout_sec", 600)),
        hitl_interrupt_on={
            str(k): bool(v) for k, v in (interrupt_raw or default_interrupt).items()
        },
        hitl_step_gate_types=list(hitl.get("step_gate_types", ["database_query"])),
        hitl_plan_review_enabled=_env_bool(
            "HARNESS_HITL_PLAN_REVIEW",
            bool(hitl.get("plan_review_enabled", True)),
        ),
        hitl_allow_edit=_env_bool(
            "HARNESS_HITL_ALLOW_EDIT",
            bool(hitl.get("allow_edit", True)),
        ),
        hitl_allow_replan=_env_bool(
            "HARNESS_HITL_ALLOW_REPLAN",
            bool(hitl.get("allow_replan", True)),
        ),
        citations_enabled=_env_bool(
            "HARNESS_CITATIONS_ENABLED",
            bool(citations.get("enabled", True)),
        ),
        citations_min_coverage_rate=float(citations.get("min_coverage_rate", 0.2)),
        eval_trajectory_min_similarity=float(
            eval_cfg.get("trajectory_min_similarity", 0.6)
        ),
        eval_heuristic_judge_enabled=_env_bool(
            "HARNESS_EVAL_HEURISTIC_JUDGE",
            bool(eval_cfg.get("heuristic_judge_enabled", True)),
        ),
        eval_heuristic_judge_min_score=float(
            eval_cfg.get("heuristic_judge_min_score", 0.6)
        ),
        eval_llm_judge_enabled=_env_bool(
            "HARNESS_EVAL_LLM_JUDGE",
            bool(eval_cfg.get("llm_judge_enabled", False)),
        ),
        eval_structured_output_min_rate=float(
            eval_cfg.get("structured_output_min_rate", 0.8)
        ),
        eval_intent_deliverable_min_accuracy=float(
            eval_cfg.get("intent_deliverable_min_accuracy", 0.9)
        ),
        eval_plan_validation_min_rate=float(
            eval_cfg.get("plan_validation_min_rate", 1.0)
        ),
        usage_tracking_enabled=_env_bool(
            "HARNESS_USAGE_TRACKING_ENABLED",
            bool(raw.get("observability", {}).get("usage_tracking_enabled", True)),
        ),
        parallel_retrieval_enabled=_env_bool(
            "HARNESS_PARALLEL_RETRIEVAL",
            bool(orch.get("parallel_retrieval_enabled", True)),
        ),
        max_parallel_workers=int(
            os.getenv("HARNESS_MAX_PARALLEL_WORKERS", orch.get("max_parallel_workers", 3))
        ),
        step_timeout_sec=int(
            os.getenv("HARNESS_STEP_TIMEOUT_SEC", orch.get("step_timeout_sec", 120))
        ),
        enforce_subagent_binding=_env_bool(
            "HARNESS_ENFORCE_SUBAGENT_BINDING",
            bool(orch.get("enforce_subagent_binding", True)),
        ),
        step_checkpoint_enabled=_env_bool(
            "HARNESS_STEP_CHECKPOINT",
            bool(orch.get("step_checkpoint_enabled", True)),
        ),
        resume_checkpoint=_env_bool(
            "HARNESS_RESUME_CHECKPOINT",
            bool(orch.get("resume_checkpoint", True)),
        ),
        supervisor_temperature=float(
            os.getenv(
                "HARNESS_SUPERVISOR_TEMPERATURE",
                orch.get("supervisor_temperature", 0.1),
            )
        ),
        structured_output_retry=_env_bool(
            "HARNESS_STRUCTURED_OUTPUT_RETRY",
            bool(orch.get("structured_output_retry", True)),
        ),
        require_structured_worker_output=_env_bool(
            "HARNESS_REQUIRE_STRUCTURED_WORKER",
            bool(orch.get("require_structured_worker_output", True)),
        ),
        synthesis_use_evidence_digest=_env_bool(
            "HARNESS_SYNTHESIS_EVIDENCE_DIGEST",
            bool(orch.get("synthesis_use_evidence_digest", True)),
        ),
        planner_llm_confirm_enabled=_env_bool(
            "HARNESS_PLANNER_LLM_CONFIRM",
            bool(
                planner_cfg.get(
                    "llm_confirm_enabled",
                    planner_cfg.get("llm_enabled", True),
                )
            ),
        ),
        planner_llm_confirm_min_confidence=float(
            planner_cfg.get(
                "llm_confirm_min_confidence",
                planner_cfg.get("llm_min_confidence", 0.5),
            )
        ),
        planner_llm_enabled=_env_bool(
            "HARNESS_PLANNER_LLM_ENABLED",
            bool(planner_cfg.get("llm_enabled", True)),
        ),
        planner_llm_min_confidence=float(planner_cfg.get("llm_min_confidence", 0.5)),
        planner_clarification_enabled=_env_bool(
            "HARNESS_PLANNER_CLARIFICATION",
            bool(planner_cfg.get("clarification_enabled", True)),
        ),
        planner_clarification_auto_resolve=_env_bool(
            "HARNESS_PLANNER_CLARIFICATION_AUTO",
            bool(planner_cfg.get("clarification_auto_resolve", True)),
        ),
        planner_plan_review_min_confidence=float(
            planner_cfg.get("plan_review_min_confidence", 0.75)
        ),
        raw=raw,
    )
    return config


def get_harness_config() -> HarnessConfig:
    global _cached_config
    if _cached_config is None:
        _cached_config = load_harness_config()
    return _cached_config


def reload_harness_config() -> HarnessConfig:
    global _cached_config
    _cached_config = load_harness_config()
    return _cached_config
