"""
Agent Harness 主循环

【Phase 3】per-step 执行 + Memory recall/remember + MCP 工具上下文。
【Phase 4】harness.yml 配置 + JSONL 结构化日志 + budget 守卫。
【Phase 5】HITL interrupt_on + step gate + Command(resume) 恢复。
【Phase 6】Citation-First + HITL Edit-in-the-Loop + Dynamic Re-plan。
【Phase 7】多 Agent 编排：检索并行 fan-out、步级 checkpoint、计划绑定、工人结构化回传。
【Phase 8】混合 Planner（规则+LLM）、结构化重试、evidence digest 写报告整合。
【Phase 9】可观测性快照 + JSONL 聚合 metrics API + Eval 扩展指标（JCR/OVR/tokens_saved）。
【Phase 11】上下文分层预算、prior 步数限制、untrusted 包裹、压缩阈值可配置。
【Phase 12】Memory TTL + user_id 解析 + 结构化 MemoryRecord + recall untrusted 包裹。
【Phase 15】生产级 Memory：SQLite + Hybrid Recall + 类型化 fact + 步内增量 + 治理/审计。
【Phase 13】运行时护栏：墙钟时限、重规划上限、计划步数上限、标准化 abort_reason。
【Phase 14】结构化槽位 + 置信度 + HITL 歧义澄清 + 默认 LLM Planner + Plan 校验强化。
"""

import asyncio
import copy
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from app.agent.harness.citations import CitationManager
from app.agent.harness.compressor import ContextCompressor
from app.agent.harness.context_builder import ContextBuilder
from app.agent.harness.hitl import hitl_coordinator
from app.agent.harness.planner import (
    apply_intent_clarification,
    apply_plan_edits,
    auto_resolve_clarification,
    dynamic_replan,
    plan_to_editable_dict,
    should_request_plan_review,
)
from app.agent.harness.orchestration import (
    IdempotencyRegistry,
    StepCheckpointStore,
    attach_structured_payload,
    build_strict_json_retry_instruction,
    check_subagent_binding,
    check_unauthorized_tools,
    find_parallel_batch,
    parse_worker_payload,
    step_idempotency_key,
    task_query_fingerprint,
    validate_structured_worker_payload,
)
from app.agent.harness.guardrails import can_replan, evaluate_run_guardrails
from app.agent.harness.observability import build_observability_snapshot
from app.agent.harness.planner_llm import build_plan_for_intent, understand_intent
from app.agent.llm import compression_model
from app.agent.harness.recovery import RecoveryManager
from app.agent.harness.state import (
    HarnessResult,
    LoopState,
    Phase,
    PhaseEvent,
    PlanStep,
    StepResult,
    StepStatus,
)
from app.agent.harness.validator import ResultValidator
from app.agent.memory.extractor import MemoryExtractor
from app.agent.memory.policy import get_memory_policy, resolve_memory_tenant_id, resolve_memory_user_id
from app.agent.memory.store import MemoryStore
from app.api.context import (
    reset_session_context,
    set_session_context,
    set_thread_context,
)
from app.api.monitor import monitor
from app.api.trace_logger import JsonlTraceLogger, get_trace_logger
from app.api.tracing import HarnessTracer, build_run_config
from app.config.loader import HarnessConfig, get_harness_config


class AgentHarness:
    def __init__(
        self,
        agent: Any,
        project_root: Path,
        validator: Optional[ResultValidator] = None,
        recovery: Optional[RecoveryManager] = None,
        compressor: Optional[ContextCompressor] = None,
        context_builder: Optional[ContextBuilder] = None,
        memory: Optional[MemoryStore] = None,
        memory_extractor: Optional[MemoryExtractor] = None,
        harness_config: Optional[HarnessConfig] = None,
        trace_logger: Optional[JsonlTraceLogger] = None,
        max_retries: Optional[int] = None,
    ):
        self.harness_config = harness_config or get_harness_config()
        self.agent = agent
        self.project_root = project_root
        self.validator = validator or ResultValidator()
        self.recovery = recovery or RecoveryManager()
        self.compressor = compressor or ContextCompressor()
        self.context_builder = context_builder or ContextBuilder.from_harness_config()
        self.memory = memory or MemoryStore()
        self.memory_extractor = memory_extractor or MemoryExtractor()
        self.trace_logger = trace_logger or get_trace_logger(project_root)
        self.max_retries = (
            max_retries
            if max_retries is not None
            else self.harness_config.max_retries
        )
        self._current_tracer: Optional[HarnessTracer] = None
        self._current_trace_id: str = ""

    async def run(self, task_query: str, session_id: str) -> HarnessResult:
        state = LoopState(session_id=session_id, max_retries=self.max_retries)
        state.metadata["strict_validation"] = self.harness_config.validation_strict_mode
        run_started = time.perf_counter()
        self._current_trace_id = self.trace_logger.new_trace_id()
        session_dir, relative_session_dir, uploaded_prompt, tokens = (
            self._prepare_session(session_id)
        )
        tracer = HarnessTracer(session_id=session_id, task_query=task_query)
        tracer.start()
        self._current_tracer = tracer
        citation_manager = CitationManager() if self.harness_config.citations_enabled else None
        checkpoint_store = StepCheckpointStore(session_dir)
        idempotency = IdempotencyRegistry()
        state.task_fingerprint = task_query_fingerprint(task_query)
        memory_policy = get_memory_policy()
        state.memory_user_id = resolve_memory_user_id(session_id)
        state.memory_tenant_id = resolve_memory_tenant_id()
        state.memory_wrap_untrusted = memory_policy.wrap_untrusted

        try:
            state = await self._phase_understand(state, task_query, bool(uploaded_prompt))
            state = await self._maybe_intent_clarification(state)
            state = await self._phase_plan(state)
            state = await self._maybe_plan_hitl_review(state)
            state = await self._phase_build_context(state, task_query)
            monitor.report_session_dir(str(session_dir).replace("\\", "/"))

            if not state.plan or not state.plan.steps:
                raise RuntimeError("Harness plan is empty")

            state, step_index = self._try_restore_checkpoint(
                state,
                task_query,
                checkpoint_store,
                idempotency,
            )

            while step_index < len(state.plan.steps):
                step = state.plan.steps[step_index]
                state.step_index = step_index
                step.metadata["status"] = StepStatus.RUNNING.value
                if self._apply_run_guardrails(state, run_started):
                    self._report_phase(
                        Phase.ABORT,
                        state.abort_reason or "guardrail",
                        state=state,
                        tool_calls=state.tool_calls_count,
                        abort_reason=state.abort_reason,
                        abort_message=state.abort_message,
                    )
                    break

                batch_indices = find_parallel_batch(
                    state.plan.steps,
                    step_index,
                    enabled=(
                        self.harness_config.parallel_retrieval_enabled
                        and not (
                            self.harness_config.hitl_enabled
                            and any(
                                candidate.step_type
                                in self.harness_config.hitl_step_gate_types
                                for candidate in state.plan.steps[step_index:]
                                if candidate.metadata.get("parallel_group")
                                == step.metadata.get("parallel_group")
                            )
                        )
                    ),
                )
                if len(batch_indices) >= 2:
                    batch_passed = await self._run_parallel_retrieval_batch(
                        state,
                        batch_indices,
                        task_query,
                        relative_session_dir,
                        uploaded_prompt,
                        session_id,
                        session_dir,
                        citation_manager,
                        idempotency,
                        checkpoint_store,
                    )
                    for idx in batch_indices:
                        state.step_validation_results.append(
                            {
                                "step_index": idx,
                                "step_type": state.plan.steps[idx].step_type,
                                "passed": batch_passed,
                                "parallel": True,
                            }
                        )
                    if not batch_passed:
                        if can_replan(state, self.harness_config):
                            state.plan = dynamic_replan(
                                state.plan,
                                batch_indices[-1],
                                "step_failed",
                            )
                            state.replan_count += 1
                            step_index = batch_indices[-1] + 1
                            continue
                        break
                    step_index = batch_indices[-1] + 1
                    continue

                step_passed = await self._run_single_step(
                    state,
                    step,
                    step_index,
                    task_query,
                    relative_session_dir,
                    uploaded_prompt,
                    session_id,
                    session_dir,
                    citation_manager,
                    idempotency,
                    checkpoint_store,
                )
                state.step_validation_results.append(
                    {
                        "step_index": step_index,
                        "step_type": step.step_type,
                        "passed": step_passed,
                    }
                )
                if not step_passed:
                    step.metadata["status"] = StepStatus.FAILED.value
                    if can_replan(state, self.harness_config):
                        state.plan = dynamic_replan(
                            state.plan,
                            step_index,
                            "step_failed",
                        )
                        state.replan_count += 1
                        self._report_phase(
                            Phase.REPLAN,
                            "done",
                            state=state,
                            step_index=step_index,
                            reason="step_failed",
                            new_steps=len(state.plan.steps),
                        )
                        step_index += 1
                        continue
                    break
                step.metadata["status"] = StepStatus.DONE.value
                step_index += 1

            if citation_manager and state.final_content:
                cited = citation_manager.build_cited_report(state.final_content)
                state.final_content = cited
                metrics = citation_manager.compute_metrics(cited)
                state.citation_coverage_rate = metrics["citation_coverage_rate"]
                state.hallucination_rate = metrics["hallucination_rate"]
                state.evidence_source_count = metrics["registered_sources"]
                citation_manager.save_evidence_json(session_dir)

            finalize_outcome = self.validator.validate_finalize(
                state,
                session_dir,
                citation_manager=citation_manager,
                min_citation_coverage=self.harness_config.citations_min_coverage_rate,
            )
            state = await self._phase_validate(
                state,
                finalize_outcome,
                step_index=state.step_index,
                scope="finalize",
            )
            success = (
                (finalize_outcome.passed or finalize_outcome.severity == "warning")
                and not state.abort_reason
            )

            return await self._phase_finalize(
                state,
                session_dir,
                success=success,
                started_at=run_started,
            )

        except asyncio.CancelledError:
            state.abort_reason = "cancelled"
            state.abort_message = "任务被取消"
            self._report_phase(Phase.ABORT, "cancelled", state=state)
            monitor.report_task_cancelled()
            tracer.finish({"status": "cancelled"})
            raise
        except Exception as e:
            state.abort_reason = "error"
            state.abort_message = str(e)
            self._report_phase(Phase.ABORT, "error", state=state, error=str(e))
            monitor._emit("error", f"Harness 执行异常：{str(e)}")
            tracer.finish({"status": "failed", "error": str(e)})
            return HarnessResult(
                session_id=session_id,
                status="failed",
                content=state.final_content,
                trace=state.trace,
                retry_count=state.retry_count,
                metadata={
                    "error": str(e),
                    "abort_reason": state.abort_reason,
                    "abort_message": state.abort_message,
                },
            )
        finally:
            self._current_tracer = None
            reset_session_context(tokens[0], tokens[1])

    async def _execute_and_validate_step(
        self,
        state: LoopState,
        step: PlanStep,
        step_index: int,
        task_query: str,
        relative_session_dir: str,
        uploaded_prompt: str,
        session_id: str,
        session_dir: Path,
        citation_manager: Optional[CitationManager],
        timeout_sec: int,
        context_builder: Optional[ContextBuilder] = None,
        run_session_id: str = "",
    ) -> tuple[bool, StepResult, str]:
        """执行单步（含超时、结构化重试）并校验，不写入 state.step_results。"""
        max_attempts = (
            2
            if (
                self.harness_config.structured_output_retry
                and step.step_type in {"network_search", "database_query", "knowledge_base"}
            )
            else 1
        )
        extra_instruction = ""
        result: Optional[StepResult] = None
        fail_reason = ""

        for attempt in range(max_attempts):
            try:
                result = await asyncio.wait_for(
                    self._phase_execute_step(
                        state,
                        step,
                        step_index,
                        task_query,
                        relative_session_dir,
                        uploaded_prompt,
                        session_id,
                        extra_instruction=extra_instruction,
                        context_builder=context_builder,
                        run_session_id=run_session_id,
                    ),
                    timeout=timeout_sec,
                )
            except asyncio.TimeoutError:
                result = StepResult(
                    step_type=step.step_type,
                    content="步骤执行超时",
                    metadata={"step_timeout": True, "timeout_sec": timeout_sec},
                )

            assert result is not None
            result = self._enrich_worker_result(step, result, state)
            structured_ok, struct_reason = self._check_structured_output(step, result)
            if step.step_type in {"network_search", "database_query", "knowledge_base"}:
                state.obs_structured_checks += 1
                if structured_ok:
                    state.obs_structured_passes += 1
            if structured_ok or attempt >= max_attempts - 1:
                break
            state.obs_structured_retries += 1
            extra_instruction = build_strict_json_retry_instruction(step)
            self._report_phase(
                Phase.RECOVER,
                "structured_retry",
                state=state,
                step_index=step_index,
                attempt=attempt + 1,
                reason=struct_reason,
            )

        assert result is not None
        result = await self._phase_compress_step(
            state, result, step_index, citation_manager
        )
        outcome = self.validator.validate_step(step, result, session_dir, state)
        await self._phase_validate(
            state,
            outcome,
            step_index=step_index,
            scope="step",
        )
        passed = outcome.passed or outcome.severity == "warning"
        return passed, result, outcome.reason

    def _check_structured_output(self, step: PlanStep, result: StepResult) -> tuple[bool, str]:
        """【Phase 8】子 Agent 步是否满足结构化 JSON 要求。"""
        if not self.harness_config.require_structured_worker_output:
            return True, ""
        payload_raw = (result.metadata or {}).get("worker_payload") or {}
        if not isinstance(payload_raw, dict):
            return False, "invalid_structured_output"
        from app.agent.harness.orchestration import WorkerResultPayload

        payload = WorkerResultPayload(
            ok=bool(payload_raw.get("ok", True)),
            summary=str(payload_raw.get("summary", "")),
            facts=list(payload_raw.get("facts") or []),
            sources=list(payload_raw.get("sources") or []),
            confidence=float(payload_raw.get("confidence", 1.0) or 1.0),
            error_code=str(payload_raw.get("error_code", "")),
            worker=str(payload_raw.get("worker", "")),
            step_type=str(payload_raw.get("step_type", step.step_type)),
        )
        return validate_structured_worker_payload(
            payload,
            step,
            require_json=True,
        )

    async def _run_single_step(
        self,
        state: LoopState,
        step: PlanStep,
        step_index: int,
        task_query: str,
        relative_session_dir: str,
        uploaded_prompt: str,
        session_id: str,
        session_dir: Path,
        citation_manager: Optional[CitationManager] = None,
        idempotency: Optional[IdempotencyRegistry] = None,
        checkpoint_store: Optional[StepCheckpointStore] = None,
    ) -> bool:
        idem_key = step_idempotency_key(session_id, step_index, step.step_type)
        if idempotency is not None:
            cached = idempotency.get(idem_key)
            if cached is not None:
                state.step_results.append(cached)
                state.final_content = cached.compressed_content or cached.content
                if idem_key not in state.completed_step_keys:
                    state.completed_step_keys.append(idem_key)
                return True

        step_retry = 0
        timeout_sec = max(10, int(self.harness_config.step_timeout_sec))
        while step_retry <= state.max_retries:
            passed, result, fail_reason = await self._execute_and_validate_step(
                state,
                step,
                step_index,
                task_query,
                relative_session_dir,
                uploaded_prompt,
                session_id,
                session_dir,
                citation_manager,
                timeout_sec=timeout_sec,
            )
            if passed:
                state.step_results.append(result)
                state.final_content = result.compressed_content or result.content
                await self._maybe_remember_step(state, step, result)
                if idempotency is not None:
                    idempotency.register(idem_key, result)
                state.completed_step_keys.append(idem_key)
                self._save_step_checkpoint(
                    state,
                    session_id,
                    step_index + 1,
                    checkpoint_store,
                )
                return True

            if step_retry >= state.max_retries:
                return False

            state = await self._phase_recover(state, fail_reason, step_index)
            if (
                can_replan(state, self.harness_config)
                and fail_reason
                in {"sql_empty", "search_too_short", "wrong_subagent", "step_timeout", "invalid_structured_output"}
            ):
                state.plan = dynamic_replan(state.plan, step_index, fail_reason)
                state.replan_count += 1
                self._report_phase(
                    Phase.REPLAN,
                    "done",
                    state=state,
                    step_index=step_index,
                    reason=fail_reason,
                    new_steps=len(state.plan.steps),
                )
            step_retry += 1
            state.retry_count += 1

        return False

    async def _run_parallel_retrieval_batch(
        self,
        state: LoopState,
        batch_indices: list[int],
        task_query: str,
        relative_session_dir: str,
        uploaded_prompt: str,
        session_id: str,
        session_dir: Path,
        citation_manager: Optional[CitationManager],
        idempotency: IdempotencyRegistry,
        checkpoint_store: Optional[StepCheckpointStore],
    ) -> bool:
        """【Phase 7】无依赖检索步 fan-out + join。"""
        state.phase = Phase.PARALLEL_EXECUTE
        self._report_phase(
            Phase.PARALLEL_EXECUTE,
            "start",
            state=state,
            batch_indices=batch_indices,
            batch_size=len(batch_indices),
        )
        sem = asyncio.Semaphore(max(1, self.harness_config.max_parallel_workers))
        timeout_sec = max(10, int(self.harness_config.step_timeout_sec))

        async def _run_one(
            idx: int,
        ) -> tuple[int, bool, Optional[StepResult], str, Optional[LoopState]]:
            step = state.plan.steps[idx]
            idem_key = step_idempotency_key(session_id, idx, step.step_type)
            cached = idempotency.get(idem_key)
            if cached is not None:
                return idx, True, cached, "", None

            # fan-out 任务只读父状态，并在独立副本上累计 trace/counter。
            # join 阶段按 step_index 单线程合并，杜绝共享 LoopState 的竞态。
            child_state = copy.deepcopy(state)
            child_state.metadata["_parallel_child"] = True
            child_state.step_index = idx
            child_state.trace = []
            child_state.assistants_called = []
            child_state.compression_ratios = []
            child_state.tool_calls_count = 0
            for field_name in (
                "obs_structured_checks",
                "obs_structured_passes",
                "obs_structured_retries",
                "obs_orchestration_violations",
                "obs_binding_violations",
                "obs_unauthorized_tool_hits",
                "obs_estimated_tokens_saved",
                "obs_step_message_tokens_peak",
                "obs_context_budget_trims",
            ):
                setattr(child_state, field_name, 0)

            try:
                async with sem:
                    passed, result, fail_reason = await self._execute_and_validate_step(
                        child_state,
                        step,
                        idx,
                        task_query,
                        relative_session_dir,
                        uploaded_prompt,
                        session_id,
                        session_dir,
                        None,
                        timeout_sec=timeout_sec,
                        context_builder=ContextBuilder.from_harness_config(),
                        run_session_id=f"{session_id}:parallel:{idx}",
                    )
            except Exception as exc:
                result = StepResult(
                    step_type=step.step_type,
                    content="并行步骤执行异常",
                    metadata={"parallel_error": str(exc)},
                )
                passed = False
                fail_reason = "parallel_step_error"
            return (
                idx,
                passed,
                result if passed else None,
                fail_reason,
                child_state,
            )

        raw = await asyncio.gather(
            *[_run_one(idx) for idx in batch_indices],
        )

        all_passed = True
        ordered: list[tuple[int, StepResult, Optional[LoopState]]] = []
        for item in sorted(raw, key=lambda x: x[0]):
            idx, passed, result, _reason, child_state = item
            if not passed or result is None:
                all_passed = False
                if child_state is not None:
                    self._merge_parallel_child_state(state, child_state)
                state.plan.steps[idx].metadata["status"] = StepStatus.FAILED.value
                continue
            ordered.append((idx, result, child_state))
            state.plan.steps[idx].metadata["status"] = StepStatus.DONE.value

        for idx, result, child_state in ordered:
            idem_key = step_idempotency_key(session_id, idx, result.step_type)
            if child_state is not None:
                self._merge_parallel_child_state(state, child_state)
            if citation_manager is not None:
                registered = citation_manager.register_from_step(
                    idx,
                    result.step_type,
                    result.content,
                    result.metadata,
                )
                if registered:
                    evidence = [source.__dict__.copy() for source in registered]
                    result.metadata["evidence_sources"] = evidence
                    source_meta = result.metadata.setdefault("source_metadata", {})
                    if isinstance(source_meta, dict):
                        source_meta["source_ids"] = [
                            source.source_id for source in registered
                        ]
            state.step_results.append(result)
            step = state.plan.steps[idx]
            await self._maybe_remember_step(state, step, result)
            idempotency.register(idem_key, result)
            if idem_key not in state.completed_step_keys:
                state.completed_step_keys.append(idem_key)
            for assistant in result.metadata.get("step_assistants_called") or []:
                if assistant not in state.assistants_called:
                    state.assistants_called.append(assistant)
        if ordered:
            last_result = ordered[-1][1]
            state.final_content = last_result.compressed_content or last_result.content

        if all_passed and checkpoint_store is not None:
            next_index = batch_indices[-1] + 1
            self._save_step_checkpoint(state, session_id, next_index, checkpoint_store)

        state.obs_parallel_batch_count += 1
        state.obs_parallel_steps_executed += len(batch_indices)
        self._report_phase(
            Phase.PARALLEL_EXECUTE,
            "done" if all_passed else "failed",
            state=state,
            batch_indices=batch_indices,
            passed=all_passed,
            timeout_sec=timeout_sec,
        )
        return all_passed

    @staticmethod
    def _merge_parallel_child_state(parent: LoopState, child: LoopState) -> None:
        """只合并可加和/可取最大值的执行增量，不覆盖父状态权威字段。"""
        parent.trace.extend(child.trace)
        parent.tool_calls_count += child.tool_calls_count
        parent.compression_ratios.extend(child.compression_ratios)
        for field_name in (
            "obs_structured_checks",
            "obs_structured_passes",
            "obs_structured_retries",
            "obs_orchestration_violations",
            "obs_binding_violations",
            "obs_unauthorized_tool_hits",
            "obs_estimated_tokens_saved",
            "obs_context_budget_trims",
        ):
            setattr(
                parent,
                field_name,
                getattr(parent, field_name) + getattr(child, field_name),
            )
        parent.obs_step_message_tokens_peak = max(
            parent.obs_step_message_tokens_peak,
            child.obs_step_message_tokens_peak,
        )

    def _enrich_worker_result(
        self,
        step: PlanStep,
        result: StepResult,
        state: LoopState,
    ) -> StepResult:
        """【Phase 7】结构化解析 + 计划绑定 / 越权工具校验。"""
        payload = parse_worker_payload(
            result.content,
            step_type=step.step_type,
            subagent=step.subagent or "",
        )
        attach_structured_payload(result, payload)

        struct_ok, struct_reason = validate_structured_worker_payload(
            payload,
            step,
            require_json=self.harness_config.require_structured_worker_output,
        )
        if not struct_ok:
            result.metadata["invalid_structured_output"] = True
            result.metadata["error_code"] = struct_reason
            payload.ok = False
            attach_structured_payload(result, payload)

        tools_invoked = list(result.metadata.get("tools_invoked") or [])
        enforce = self.harness_config.enforce_subagent_binding
        assistants_for_binding = list(
            result.metadata.get("step_assistants_called") or state.assistants_called
        )
        binding_ok, binding_reason = check_subagent_binding(
            step,
            assistants_for_binding,
            enforce=enforce,
        )
        if not binding_ok:
            result.metadata["binding_failed"] = True
            result.metadata["error_code"] = binding_reason
            payload.ok = False
            payload.error_code = binding_reason
            attach_structured_payload(result, payload)
            state.obs_binding_violations += 1
            state.obs_orchestration_violations += 1

        auth_ok, unauthorized = check_unauthorized_tools(
            step,
            tools_invoked,
            enforce=enforce,
        )
        if not auth_ok:
            result.metadata["unauthorized_tools"] = unauthorized
            payload.ok = False
            payload.error_code = "unauthorized_tool"
            attach_structured_payload(result, payload)
            state.obs_unauthorized_tool_hits += len(unauthorized)
            state.obs_orchestration_violations += 1
        return result

    def _try_restore_checkpoint(
        self,
        state: LoopState,
        task_query: str,
        checkpoint_store: StepCheckpointStore,
        idempotency: IdempotencyRegistry,
    ) -> tuple[LoopState, int]:
        if not (
            self.harness_config.step_checkpoint_enabled
            and self.harness_config.resume_checkpoint
        ):
            return state, 0

        data = checkpoint_store.load()
        if not data:
            return state, 0
        if data.get("task_fingerprint") != state.task_fingerprint:
            return state, 0
        if data.get("session_id") != state.session_id:
            return state, 0

        state.step_results = checkpoint_store.restore_step_results(data)
        state.assistants_called = list(data.get("assistants_called") or [])
        state.completed_step_keys = list(data.get("completed_step_keys") or [])
        idempotency.load_from_checkpoint(data, checkpoint_store)
        state.resumed_from_checkpoint = True
        next_index = int(data.get("next_step_index") or 0)
        for idx in range(min(next_index, len(state.plan.steps))):
            state.plan.steps[idx].metadata["status"] = StepStatus.DONE.value
        self._report_phase(
            Phase.BUILD_CONTEXT,
            "checkpoint_resumed",
            state=state,
            next_step_index=next_index,
            restored_steps=len(state.step_results),
        )
        return state, next_index

    def _save_step_checkpoint(
        self,
        state: LoopState,
        session_id: str,
        next_step_index: int,
        checkpoint_store: Optional[StepCheckpointStore],
    ) -> None:
        if not self.harness_config.step_checkpoint_enabled or checkpoint_store is None:
            return
        checkpoint_store.save(
            session_id=session_id,
            task_fingerprint=state.task_fingerprint,
            next_step_index=next_step_index,
            step_results=state.step_results,
            assistants_called=state.assistants_called,
            completed_keys=state.completed_step_keys,
            plan_summary=state.plan.summary if state.plan else "",
        )

    def _prepare_session(self, session_id: str):
        session_dir = self.project_root / "output" / f"session_{session_id}"
        session_dir.mkdir(parents=True, exist_ok=True)

        session_dir_str = str(session_dir).replace("\\", "/")
        relative_session_dir = str(session_dir.relative_to(self.project_root)).replace(
            "\\", "/"
        )

        updated_dir = self.project_root / "updated" / f"session_{session_id}"
        uploaded_prompt = ""
        if updated_dir.exists():
            files = [f.name for f in updated_dir.iterdir() if f.is_file()]
            if files:
                for filename in files:
                    shutil.copy2(updated_dir / filename, session_dir / filename)
                uploaded_prompt = (
                    "\n    [已上传文件] 已加载到工作目录:\n"
                    + "\n".join([f"    - {f}" for f in files])
                    + "\n    请优先使用工具（read_file_content）读取并参考这些文件。"
                )

        session_dir_token = set_session_context(session_dir_str)
        session_id_token = set_thread_context(session_id)
        return session_dir, relative_session_dir, uploaded_prompt, (
            session_dir_token,
            session_id_token,
        )

    async def _phase_understand(
        self,
        state: LoopState,
        task_query: str,
        has_uploaded_files: bool,
    ) -> LoopState:
        """【Phase 14】仅理解：规则 + 默认 LLM → TaskIntent（不含 Plan）。"""
        started = time.perf_counter()
        self._report_phase(Phase.UNDERSTAND, "start", state=state)
        state.phase = Phase.UNDERSTAND
        from app.agent.harness.usage_tracker import set_llm_phase, set_llm_session

        set_llm_session(state.session_id)
        set_llm_phase(Phase.UNDERSTAND.value)

        state.intent = await understand_intent(
            task_query,
            session_id=state.session_id,
            has_uploaded_files=has_uploaded_files,
            model=compression_model,
            llm_enabled=self.harness_config.planner_llm_enabled,
            llm_min_confidence=self.harness_config.planner_llm_min_confidence,
            clarification_auto_resolve=self.harness_config.planner_clarification_auto_resolve,
        )

        duration = int((time.perf_counter() - started) * 1000)
        self._report_phase(
            Phase.UNDERSTAND,
            "done",
            state=state,
            duration_ms=duration,
            intent=state.intent.summary if state.intent else "",
            deliverable=state.intent.deliverable if state.intent else "",
            planner_source=state.intent.planner_source if state.intent else "rules",
            intent_confidence=state.intent.intent_confidence if state.intent else 0,
            slots=state.intent.slots.to_dict() if state.intent else {},
            needs_clarification=state.intent.needs_clarification if state.intent else False,
        )
        return state

    async def _maybe_intent_clarification(self, state: LoopState) -> LoopState:
        """【Phase 14】低置信 / 歧义 → HITL 意图澄清。"""
        if not self.harness_config.hitl_enabled:
            return state
        if not self.harness_config.planner_clarification_enabled:
            return state
        if not state.intent or not state.intent.needs_clarification:
            return state
        if state.intent.clarification_resolved:
            return state
        if self.harness_config.planner_clarification_auto_resolve:
            state.intent = auto_resolve_clarification(state.intent)
            return state

        intent = state.intent
        allowed = ["approve", "reject"]
        if self.harness_config.hitl_allow_edit:
            allowed.append("edit")

        payload = {
            "action_requests": [
                {
                    "name": "task_intent",
                    "args": {
                        "question": intent.clarification_question,
                        "intent": intent.to_dict(),
                        "suggested_deliverables": ["text", "md", "pdf"],
                    },
                }
            ],
            "review_configs": [
                {
                    "action_name": "task_intent",
                    "allowed_decisions": allowed,
                }
            ],
            "gate_type": "intent_clarification",
            "editable": self.harness_config.hitl_allow_edit,
        }
        monitor.report_hitl_interrupt(
            state.session_id,
            payload["action_requests"],
            payload["review_configs"],
            step_index=-1,
            gate_type="intent_clarification",
            editable=self.harness_config.hitl_allow_edit,
        )
        self._report_phase(
            Phase.UNDERSTAND,
            "awaiting_clarification",
            state=state,
            gate_type="intent_clarification",
        )
        try:
            decisions = await hitl_coordinator.wait_for_decisions(
                state.session_id,
                payload,
                timeout_sec=self.harness_config.hitl_timeout_sec,
            )
        except TimeoutError:
            state.intent = auto_resolve_clarification(state.intent)
            return state

        state = self._apply_hitl_decisions(state, decisions, step=None, step_index=-1)
        self._report_phase(
            Phase.UNDERSTAND,
            "clarification_resolved",
            state=state,
            gate_type="intent_clarification",
            decisions=decisions,
        )
        return state

    async def _phase_plan(self, state: LoopState) -> LoopState:
        """【Phase 14】由 TaskIntent 生成 ExecutionPlan 并校验。"""
        started = time.perf_counter()
        self._report_phase(Phase.PLAN, "start", state=state)
        state.phase = Phase.PLAN
        from app.agent.harness.usage_tracker import set_llm_phase

        set_llm_phase(Phase.PLAN.value)
        if state.intent:
            plan, issues = build_plan_for_intent(state.intent)
            state.plan = plan
            if issues:
                state.metadata["plan_validation_issues"] = issues
        duration = int((time.perf_counter() - started) * 1000)
        self._report_phase(
            Phase.PLAN,
            "done",
            state=state,
            duration_ms=duration,
            steps=len(state.plan.steps) if state.plan else 0,
            summary=state.plan.summary if state.plan else "",
            plan_validation_issues=state.metadata.get("plan_validation_issues", []),
        )
        return state

    async def _maybe_plan_hitl_review(self, state: LoopState) -> LoopState:
        """【Phase 6/14】多意图 / 低置信 / 歧义 → 计划审批 + Edit-in-the-Loop。"""
        if not self.harness_config.hitl_enabled:
            return state
        if not self.harness_config.hitl_plan_review_enabled:
            return state
        if not state.intent or not state.plan:
            return state
        if not should_request_plan_review(
            state.intent,
            min_confidence=self.harness_config.planner_plan_review_min_confidence,
        ):
            return state

        allowed = ["approve", "reject"]
        if self.harness_config.hitl_allow_edit:
            allowed.append("edit")

        payload = {
            "action_requests": [
                {
                    "name": "execution_plan",
                    "args": {
                        "summary": state.plan.summary,
                        "steps": plan_to_editable_dict(state.plan),
                        "intent": state.intent.to_dict() if state.intent else {},
                        "intent_confidence": state.intent.intent_confidence if state.intent else 1.0,
                    },
                }
            ],
            "review_configs": [
                {
                    "action_name": "execution_plan",
                    "allowed_decisions": allowed,
                }
            ],
            "gate_type": "plan_review",
            "editable": self.harness_config.hitl_allow_edit,
        }
        monitor.report_hitl_interrupt(
            state.session_id,
            payload["action_requests"],
            payload["review_configs"],
            step_index=-1,
            gate_type="plan_review",
            editable=self.harness_config.hitl_allow_edit,
        )
        self._report_phase(Phase.PLAN, "awaiting_approval", state=state, gate_type="plan_review")
        try:
            decisions = await hitl_coordinator.wait_for_decisions(
                state.session_id,
                payload,
                timeout_sec=self.harness_config.hitl_timeout_sec,
            )
        except TimeoutError:
            return state

        state = self._apply_hitl_decisions(state, decisions, step=None, step_index=-1)
        self._report_phase(
            Phase.PLAN,
            "resumed",
            state=state,
            gate_type="plan_review",
            decisions=decisions,
        )
        return state

    def _apply_hitl_decisions(
        self,
        state: LoopState,
        decisions: list[dict[str, Any]],
        step: Optional[PlanStep],
        step_index: int,
    ) -> LoopState:
        """【Phase 6】统一处理 approve / reject / edit 决策。"""
        for decision in decisions:
            dtype = decision.get("type", "approve")
            if dtype == "approve":
                if state.intent and state.intent.needs_clarification:
                    state.intent.needs_clarification = False
                    state.intent.clarification_resolved = True
                continue
            if dtype == "reject":
                if step is not None:
                    step.metadata["hitl_rejected"] = True
                continue
            if dtype == "edit":
                edited = decision.get("edited_action") or {}
                if step is None and edited and state.intent and (
                    edited.get("deliverable")
                    or edited.get("slots")
                    or edited.get("intent")
                ):
                    patch = edited.get("intent") if isinstance(edited.get("intent"), dict) else edited
                    state.intent = apply_intent_clarification(state.intent, patch)
                    if state.intent:
                        plan, issues = build_plan_for_intent(state.intent)
                        state.plan = plan
                        if issues:
                            state.metadata["plan_validation_issues"] = issues
                if step is not None and edited.get("description"):
                    step.description = str(edited["description"])
                    step.metadata["hitl_edited"] = True
                if edited.get("steps") and state.plan:
                    state.plan = apply_plan_edits(state.plan, edited["steps"])
                    state.replan_count += 1
                if edited.get("replan") and self.harness_config.hitl_allow_replan and state.plan:
                    state.plan = dynamic_replan(
                        state.plan,
                        max(step_index, 0),
                        "user_replan",
                    )
                    state.replan_count += 1
                    self._report_phase(
                        Phase.REPLAN,
                        "done",
                        state=state,
                        step_index=step_index,
                        reason="user_replan",
                        new_steps=len(state.plan.steps),
                    )
        return state

    async def _maybe_remember_step(
        self,
        state: LoopState,
        step: PlanStep,
        result: StepResult,
    ) -> None:
        """【Phase 15】检索步成功后步内增量写入 episodic/semantic fact。"""
        policy = get_memory_policy()
        if not policy.enabled or not policy.step_incremental_enabled:
            return
        if step.step_type not in {"network_search", "database_query", "knowledge_base", "file_read"}:
            return
        content = result.compressed_content or result.content
        writes = self.memory_extractor.extract_step_writes(
            content,
            step.step_type,
            session_id=state.session_id,
            task=state.intent.raw_query if state.intent else "",
        )
        if not writes:
            return
        saved = await self.memory.remember_writes(
            writes,
            user_id=state.memory_user_id,
            tenant_id=state.memory_tenant_id,
        )
        if saved:
            state.obs_memory_saved_count += saved
            monitor.report_phase(
                "memory",
                "step_incremental",
                session_id=state.session_id,
                count=saved,
                step_type=step.step_type,
            )

    async def _phase_build_context(self, state: LoopState, task_query: str) -> LoopState:
        started = time.perf_counter()
        self._report_phase(Phase.BUILD_CONTEXT, "start", state=state)
        state.phase = Phase.BUILD_CONTEXT
        from app.agent.harness.usage_tracker import set_llm_phase

        set_llm_phase(Phase.BUILD_CONTEXT.value)

        recalled_result = await self.memory.recall_with_metrics(
            task_query,
            state.memory_user_id,
            tenant_id=state.memory_tenant_id,
            top_k=self.harness_config.memory_recall_top_k,
        )
        recalled = recalled_result.records
        state.memory_records = recalled
        state.memory_facts = [r.fact for r in recalled if r.fact]
        state.memory_recalled = bool(state.memory_facts)
        state.obs_memory_recalled_count = len(state.memory_facts)
        state.obs_memory_recall_at_k = recalled_result.recall_at_k
        state.obs_memory_embedding_used = recalled_result.embedding_used
        if state.memory_facts:
            monitor.report_phase(
                "memory",
                "done",
                session_id=state.session_id,
                count=len(state.memory_facts),
                source="recall",
                user_id=state.memory_user_id,
            )

        duration = int((time.perf_counter() - started) * 1000)
        self._report_phase(
            Phase.BUILD_CONTEXT,
            "done",
            state=state,
            duration_ms=duration,
            memory_count=len(recalled),
        )
        return state

    async def _phase_execute_step(
        self,
        state: LoopState,
        step: PlanStep,
        step_index: int,
        task_query: str,
        relative_session_dir: str,
        uploaded_prompt: str,
        session_id: str,
        extra_instruction: str = "",
        context_builder: Optional[ContextBuilder] = None,
        run_session_id: str = "",
    ) -> StepResult:
        started = time.perf_counter()
        self._report_phase(
            Phase.EXECUTE,
            "start",
            state=state,
            step_index=step_index,
            step_type=step.step_type,
            total_steps=len(state.plan.steps),
        )
        state.phase = Phase.EXECUTE
        from app.agent.harness.usage_tracker import set_llm_phase

        set_llm_phase(Phase.EXECUTE.value)

        if not await self._maybe_step_hitl_gate(state, step, step_index):
            duration = int((time.perf_counter() - started) * 1000)
            self._report_phase(
                Phase.EXECUTE,
                "rejected",
                state=state,
                step_index=step_index,
                step_type=step.step_type,
                duration_ms=duration,
            )
            return StepResult(
                step_type=step.step_type,
                content="用户拒绝了该步骤的人工审批，已跳过执行。",
                metadata={"hitl_rejected": True, "duration_ms": duration},
            )

        builder = context_builder or self.context_builder
        user_message = builder.build_step_message(
            task_query,
            state,
            step,
            step_index,
            relative_session_dir,
            uploaded_prompt,
            enforce_binding=self.harness_config.enforce_subagent_binding,
            use_evidence_digest=self.harness_config.synthesis_use_evidence_digest,
            extra_instruction=extra_instruction,
        )
        step_ctx_metrics = builder.last_step_metrics
        if step_ctx_metrics is not None:
            state.obs_step_message_tokens_peak = max(
                state.obs_step_message_tokens_peak,
                step_ctx_metrics.total_tokens,
            )
            if step_ctx_metrics.layers.get("budget_trimmed"):
                state.obs_context_budget_trims += 1
            self._report_phase(
                Phase.EXECUTE,
                "context_built",
                state=state,
                step_index=step_index,
                context_metrics=step_ctx_metrics.to_dict(),
            )
        config = build_run_config(
            run_session_id or session_id,
            metadata={
                "phase": "execute",
                "step_index": step_index,
                "step_type": step.step_type,
                "usage_session_id": session_id,
            },
        )
        final_content = ""
        tool_calls = 0
        tools_invoked: list[str] = []
        step_assistants: list[str] = []

        async for chunk in self.agent.astream(
            {"messages": [{"role": "user", "content": user_message}]},
            config=config,
        ):
            for node_name, node_state in chunk.items():
                if not node_state or "messages" not in node_state:
                    continue
                messages = node_state["messages"]
                if not messages or not isinstance(messages, list):
                    continue
                last_msg = messages[-1]
                if node_name == "model":
                    if getattr(last_msg, "tool_calls", None):
                        tool_calls += len(last_msg.tool_calls)
                        for tool_call in last_msg.tool_calls:
                            tool_name = tool_call["name"]
                            tools_invoked.append(tool_name)
                            if tool_name == "task":
                                subagent = tool_call["args"].get("subagent_type", "")
                                if subagent and subagent not in step_assistants:
                                    step_assistants.append(subagent)
                                if subagent and subagent not in state.assistants_called:
                                    state.assistants_called.append(subagent)
                                monitor.report_assistant(
                                    subagent,
                                    {"description": tool_call["args"].get("description")},
                                )
                            else:
                                monitor.report_tool(
                                    tool_name,
                                    tool_call.get("args", {}),
                                )
                    elif getattr(last_msg, "content", None):
                        content = last_msg.content
                        if isinstance(content, str):
                            final_content = content

        await self._await_hitl_interrupt_resumes(config, state, step_index)
        snapshot = await self.agent.aget_state(config)
        if snapshot is not None:
            extracted = self._extract_final_content_from_snapshot(snapshot)
            if extracted:
                final_content = extracted

        state.tool_calls_count += tool_calls
        duration = int((time.perf_counter() - started) * 1000)
        self._report_phase(
            Phase.EXECUTE,
            "done",
            state=state,
            duration_ms=duration,
            step_index=step_index,
            step_type=step.step_type,
            tool_calls=tool_calls,
        )
        return StepResult(
            step_type=step.step_type,
            content=final_content,
            metadata={
                "tool_calls": tool_calls,
                "duration_ms": duration,
                "tools_invoked": tools_invoked,
                "step_assistants_called": step_assistants,
            },
        )

    async def _maybe_step_hitl_gate(
        self,
        state: LoopState,
        step: PlanStep,
        step_index: int,
    ) -> bool:
        if not self.harness_config.hitl_enabled:
            return True
        if step.step_type not in self.harness_config.hitl_step_gate_types:
            return True

        allowed = ["approve", "reject"]
        if self.harness_config.hitl_allow_edit:
            allowed.append("edit")

        payload = {
            "action_requests": [
                {
                    "name": step.step_type,
                    "args": {
                        "description": step.description,
                        "subagent": step.subagent,
                    },
                }
            ],
            "review_configs": [
                {
                    "action_name": step.step_type,
                    "allowed_decisions": allowed,
                }
            ],
            "step_index": step_index,
            "gate_type": "step",
            "editable": self.harness_config.hitl_allow_edit,
        }
        monitor.report_hitl_interrupt(
            state.session_id,
            payload["action_requests"],
            payload["review_configs"],
            step_index=step_index,
            gate_type="step",
            editable=self.harness_config.hitl_allow_edit,
        )
        self._report_phase(
            Phase.EXECUTE,
            "awaiting_approval",
            state=state,
            step_index=step_index,
            gate_type="step",
        )
        try:
            decisions = await hitl_coordinator.wait_for_decisions(
                state.session_id,
                payload,
                timeout_sec=self.harness_config.hitl_timeout_sec,
            )
        except TimeoutError:
            return False

        if any(decision.get("type") == "reject" for decision in decisions):
            approved = False
        else:
            state = self._apply_hitl_decisions(state, decisions, step, step_index)
            approved = True

        self._report_phase(
            Phase.EXECUTE,
            "resumed" if approved else "rejected",
            state=state,
            step_index=step_index,
            gate_type="step",
            decisions=decisions,
        )
        return approved

    async def _await_hitl_interrupt_resumes(
        self,
        config: dict[str, Any],
        state: LoopState,
        step_index: int,
    ) -> None:
        if not self.harness_config.hitl_enabled:
            return

        from langgraph.types import Command

        while True:
            snapshot = await self.agent.aget_state(config)
            payload = self._extract_interrupt_payload(snapshot)
            if not payload:
                break

            action_requests = payload.get("action_requests", [])
            review_configs = payload.get("review_configs", [])
            monitor.report_hitl_interrupt(
                state.session_id,
                action_requests,
                review_configs,
                step_index=step_index,
                gate_type="interrupt_on",
            )
            self._report_phase(
                Phase.EXECUTE,
                "awaiting_approval",
                state=state,
                step_index=step_index,
                gate_type="interrupt_on",
                action_count=len(action_requests),
            )
            decisions = await hitl_coordinator.wait_for_decisions(
                state.session_id,
                {
                    "action_requests": action_requests,
                    "review_configs": review_configs,
                    "step_index": step_index,
                    "gate_type": "interrupt_on",
                    "editable": self.harness_config.hitl_allow_edit,
                },
                timeout_sec=self.harness_config.hitl_timeout_sec,
            )
            if any(d.get("type") == "edit" for d in decisions):
                state = self._apply_hitl_decisions(state, decisions, None, step_index)
            await self.agent.ainvoke(
                Command(resume={"decisions": decisions}),
                config=config,
            )
            self._report_phase(
                Phase.EXECUTE,
                "resumed",
                state=state,
                step_index=step_index,
                gate_type="interrupt_on",
                decisions=decisions,
            )

    def _extract_interrupt_payload(self, snapshot: Any) -> dict[str, Any] | None:
        if snapshot is None:
            return None

        interrupts = getattr(snapshot, "interrupts", None) or ()
        if interrupts:
            first = interrupts[0]
            value = getattr(first, "value", first)
            if isinstance(value, dict) and value.get("action_requests"):
                return value

        values = getattr(snapshot, "values", None) or {}
        raw_interrupt = values.get("__interrupt__")
        if raw_interrupt:
            if isinstance(raw_interrupt, list) and raw_interrupt:
                value = getattr(raw_interrupt[0], "value", raw_interrupt[0])
                if isinstance(value, dict):
                    return value
            if isinstance(raw_interrupt, dict):
                return raw_interrupt

        return None

    def _extract_final_content_from_snapshot(self, snapshot: Any) -> str:
        values = getattr(snapshot, "values", None) or {}
        messages = values.get("messages", [])
        for msg in reversed(messages):
            content = getattr(msg, "content", None)
            if isinstance(content, str) and content.strip():
                return content
        return ""

    async def _phase_compress_step(
        self,
        state: LoopState,
        result: StepResult,
        step_index: int,
        citation_manager: Optional[CitationManager] = None,
    ) -> StepResult:
        started = time.perf_counter()
        self._report_phase(
            Phase.COMPRESS,
            "start",
            state=state,
            step_index=step_index,
            step_type=result.step_type,
        )
        state.phase = Phase.COMPRESS
        from app.agent.harness.usage_tracker import set_llm_phase

        set_llm_phase(Phase.COMPRESS.value)

        source_meta: dict[str, Any] = {"step_index": step_index}
        if citation_manager is not None:
            registered = citation_manager.register_from_step(
                step_index,
                result.step_type,
                result.content,
                result.metadata,
            )
            source_meta["source_ids"] = [s.source_id for s in registered]
            result.metadata["evidence_sources"] = (
                citation_manager.to_dict_list()[-len(registered):]
                if registered
                else []
            )

        compressed, meta = await self.compressor.compress(
            result.content,
            result.step_type,
            source_metadata=source_meta,
        )
        result.compressed_content = compressed
        result.metadata.update(meta)
        ratio = meta.get("compression_ratio")
        if isinstance(ratio, (int, float)):
            state.compression_ratios.append(float(ratio))
            original_chars = int(meta.get("original_chars") or 0)
            compressed_chars = int(meta.get("compressed_chars") or 0)
            if original_chars > compressed_chars > 0:
                state.obs_estimated_tokens_saved += max(
                    1, (original_chars - compressed_chars) // 4
                )

        duration = int((time.perf_counter() - started) * 1000)
        self._report_phase(
            Phase.COMPRESS,
            "done",
            state=state,
            duration_ms=duration,
            step_index=step_index,
            step_type=result.step_type,
            compression_method=meta.get("method"),
            compression_ratio=ratio,
            evidence_sources=result.metadata.get("evidence_sources"),
        )
        return result

    async def _phase_validate(
        self,
        state: LoopState,
        outcome,
        step_index: int = 0,
        scope: str = "step",
    ) -> LoopState:
        started = time.perf_counter()
        state.phase = Phase.VALIDATE
        from app.agent.harness.usage_tracker import set_llm_phase

        set_llm_phase(Phase.VALIDATE.value)
        status = "done" if outcome.passed else "failed"
        duration = int((time.perf_counter() - started) * 1000)
        self._report_phase(
            Phase.VALIDATE,
            status,
            state=state,
            duration_ms=duration,
            step_index=step_index,
            scope=scope,
            reason=outcome.reason,
            passed=outcome.passed,
        )
        return state

    async def _phase_recover(
        self,
        state: LoopState,
        reason: str,
        step_index: int,
    ) -> LoopState:
        started = time.perf_counter()
        self._report_phase(
            Phase.RECOVER,
            "start",
            state=state,
            reason=reason,
            step_index=step_index,
        )
        state.phase = Phase.RECOVER
        from app.agent.harness.usage_tracker import set_llm_phase

        set_llm_phase(Phase.RECOVER.value)
        hint = self.recovery.build_recovery_hint(reason, state)
        state.recovery_hints.append(hint)
        duration = int((time.perf_counter() - started) * 1000)
        self._report_phase(
            Phase.RECOVER,
            "done",
            state=state,
            duration_ms=duration,
            step_index=step_index,
            hint=hint[:200],
        )
        return state

    async def _phase_finalize(
        self,
        state: LoopState,
        session_dir: Path,
        success: bool,
        started_at: float,
    ) -> HarnessResult:
        phase_started = time.perf_counter()
        self._report_phase(Phase.FINALIZE, "start", state=state)
        state.phase = Phase.FINALIZE
        from app.agent.harness.usage_tracker import set_llm_phase

        set_llm_phase(Phase.FINALIZE.value)

        artifacts = [
            f.name
            for f in session_dir.iterdir()
            if f.is_file() and f.suffix in (".md", ".pdf", ".txt")
        ]

        saved = 0
        policy = get_memory_policy()
        should_remember = success or policy.remember_on_partial
        if should_remember and state.final_content.strip():
            writes = await self.memory_extractor.extract_writes(
                state.final_content,
                max_facts=self.harness_config.memory_max_facts_per_remember,
                task=state.intent.raw_query if state.intent else "",
                topic=(state.intent.summary if state.intent else "")[:120],
                session_id=state.session_id,
            )
            saved = await self.memory.remember_writes(
                writes,
                user_id=state.memory_user_id,
                tenant_id=state.memory_tenant_id,
            )
        state.obs_memory_saved_count = saved
        if saved:
            monitor.report_phase(
                "memory",
                "done",
                session_id=state.session_id,
                count=saved,
                source="remember",
            )

        if state.final_content:
            monitor.report_task_result(state.final_content)

        duration = int((time.perf_counter() - phase_started) * 1000)
        status = "success" if success else "partial"
        total_latency_ms = int((time.perf_counter() - started_at) * 1000)
        step_passed = sum(1 for v in state.step_validation_results if v.get("passed"))
        step_total = len(state.step_validation_results)
        step_success_rate = step_passed / step_total if step_total else 0.0
        avg_compression = (
            sum(state.compression_ratios) / len(state.compression_ratios)
            if state.compression_ratios
            else 1.0
        )

        self._report_phase(
            Phase.FINALIZE,
            "done",
            state=state,
            duration_ms=duration,
            result_status=status,
            artifacts=artifacts,
        )

        obs_snapshot = build_observability_snapshot(state)
        from app.agent.harness.usage_tracker import get_usage_tracker

        usage_summary = get_usage_tracker().session_summary(state.session_id)
        result = HarnessResult(
            session_id=state.session_id,
            status=status,
            content=state.final_content,
            trace=state.trace,
            artifacts=artifacts,
            retry_count=state.retry_count,
            metadata={
                "assistants_called": state.assistants_called,
                "plan_steps": len(state.plan.steps) if state.plan else 0,
                "tool_calls_count": state.tool_calls_count,
                "latency_ms": total_latency_ms,
                "step_success_rate": round(step_success_rate, 3),
                "avg_compression_ratio": round(avg_compression, 3),
                "memory_recalled": state.memory_recalled,
                "memory_saved_count": saved,
                "memory_user_id": state.memory_user_id,
                "memory_tenant_id": state.memory_tenant_id,
                "memory_recalled_count": getattr(state, "obs_memory_recalled_count", 0),
                "memory_recall_at_k": getattr(state, "obs_memory_recall_at_k", 0.0),
                "memory_embedding_used": getattr(state, "obs_memory_embedding_used", False),
                "step_validation_results": state.step_validation_results,
                "replan_count": state.replan_count,
                "citation_coverage_rate": state.citation_coverage_rate,
                "hallucination_rate": state.hallucination_rate,
                "evidence_source_count": state.evidence_source_count,
                "resumed_from_checkpoint": state.resumed_from_checkpoint,
                "completed_step_keys": state.completed_step_keys,
                "abort_reason": state.abort_reason,
                "abort_message": state.abort_message,
                "observability": obs_snapshot.to_dict(),
                "usage": usage_summary,
            },
        )
        if self._current_tracer is not None:
            self._current_tracer.finish(
                {
                    "status": result.status,
                    "retry_count": result.retry_count,
                    "artifacts": result.artifacts,
                    "metadata": result.metadata,
                }
            )
        self.trace_logger.log_run_summary(
            trace_id=self._current_trace_id,
            session_id=state.session_id,
            status=result.status,
            duration_ms=total_latency_ms,
            metadata=result.metadata,
        )
        return result

    def _estimate_run_tokens(self, state: LoopState) -> int:
        estimated_tokens = sum(
            self.compressor.estimate_tokens(result.content)
            for result in state.step_results
        )
        estimated_tokens += self.compressor.estimate_tokens(state.final_content)
        return estimated_tokens

    def _apply_run_guardrails(self, state: LoopState, run_started: float) -> bool:
        """【Phase 13】每步前评估护栏；命中则写入 abort_reason 并返回 True。"""
        decision = evaluate_run_guardrails(
            state,
            self.harness_config,
            elapsed_sec=time.perf_counter() - run_started,
            estimated_tokens=self._estimate_run_tokens(state),
        )
        if not decision.abort:
            return False
        state.abort_reason = decision.reason
        state.abort_message = decision.message
        return True

    def _budget_exceeded(self, state: LoopState) -> bool:
        """向后兼容：仅检查工具次数与 token 预算。"""
        return evaluate_run_guardrails(
            state,
            self.harness_config,
            elapsed_sec=0.0,
            estimated_tokens=self._estimate_run_tokens(state),
        ).abort

    def _report_phase(
        self,
        phase: Phase,
        status: str,
        state: LoopState,
        **data: Any,
    ) -> None:
        # 并行子任务共享一个 HarnessTracer 时，按 phase 作为 key 会互相覆盖 span。
        # 子任务仍写 JSONL/内存 trace；父级保留 parallel_execute 聚合 span。
        tracer = None if state.metadata.get("_parallel_child") else self._current_tracer
        if tracer is not None:
            if status == "start":
                tracer.phase_start(phase.value, data)
            else:
                tracer.phase_end(phase.value, status, data)

        event = PhaseEvent(phase=phase.value, status=status, data=data)
        if "duration_ms" in data:
            event.duration_ms = data["duration_ms"]
        state.trace.append(event)
        monitor_data = {k: v for k, v in data.items() if k != "status"}
        monitor.report_phase(phase.value, status, session_id=state.session_id, **monitor_data)

        log_status = status
        if status in {"done", "start"}:
            log_status = "ok" if status == "done" else "start"
        elif status in {
            "failed",
            "error",
            "cancelled",
            "budget_exceeded",
            "budget_tool_calls",
            "budget_tokens",
            "deadline_exceeded",
            "max_replan",
            "max_plan_steps",
            "guardrail",
        }:
            log_status = status

        self.trace_logger.log_event(
            trace_id=self._current_trace_id,
            session_id=state.session_id,
            phase=phase.value,
            status=log_status,
            step_index=data.get("step_index"),
            step_type=data.get("step_type"),
            duration_ms=data.get("duration_ms"),
            tool_calls=data.get("tool_calls"),
            tokens_used=data.get("tokens_used"),
            extra={k: v for k, v in data.items() if k not in {
                "step_index", "step_type", "duration_ms", "tool_calls", "tokens_used"
            }},
        )
