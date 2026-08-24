"""
【Phase 9】Harness 运行时观测快照

将 Loop 内计数器聚合为结构化指标，写入 JSONL run_summary 与 Langfuse output，
供离线 eval 与在线 /api/metrics 聚合。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.agent.harness.state import LoopState


@dataclass
class RunObservabilitySnapshot:
    """单次 Harness run 的可观测性快照。"""

    structured_output_checks: int = 0
    structured_output_passes: int = 0
    structured_output_retries: int = 0
    parallel_batch_count: int = 0
    parallel_steps_executed: int = 0
    orchestration_violation_count: int = 0
    binding_violation_count: int = 0
    unauthorized_tool_count: int = 0
    estimated_tokens_saved: int = 0
    compression_steps: int = 0
    step_message_tokens_peak: int = 0
    context_budget_trims: int = 0
    memory_recalled_count: int = 0
    memory_saved_count: int = 0

    @property
    def structured_output_compliance_rate(self) -> float | None:
        if self.structured_output_checks <= 0:
            return None
        return round(self.structured_output_passes / self.structured_output_checks, 3)

    @property
    def orchestration_violation_rate(self) -> float | None:
        checks = self.structured_output_checks + self.binding_violation_count
        if checks <= 0:
            return None
        return round(self.orchestration_violation_count / checks, 3)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["structured_output_compliance_rate"] = self.structured_output_compliance_rate
        payload["orchestration_violation_rate"] = self.orchestration_violation_rate
        return payload


def build_observability_snapshot(state: "LoopState") -> RunObservabilitySnapshot:
    """从 LoopState 观测字段构建快照。"""
    return RunObservabilitySnapshot(
        structured_output_checks=state.obs_structured_checks,
        structured_output_passes=state.obs_structured_passes,
        structured_output_retries=state.obs_structured_retries,
        parallel_batch_count=state.obs_parallel_batch_count,
        parallel_steps_executed=state.obs_parallel_steps_executed,
        orchestration_violation_count=state.obs_orchestration_violations,
        binding_violation_count=state.obs_binding_violations,
        unauthorized_tool_count=state.obs_unauthorized_tool_hits,
        estimated_tokens_saved=state.obs_estimated_tokens_saved,
        compression_steps=len(state.compression_ratios),
        step_message_tokens_peak=state.obs_step_message_tokens_peak,
        context_budget_trims=state.obs_context_budget_trims,
        memory_recalled_count=state.obs_memory_recalled_count,
        memory_saved_count=state.obs_memory_saved_count,
    )
