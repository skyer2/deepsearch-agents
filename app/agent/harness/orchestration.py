"""
多 Agent 编排增强（Phase 7）

【修改点】工业级协作原语：
- 检索步并行分组（无依赖 fan-out + join）
- 工人结构化回传解析
- 步级 checkpoint 持久化
- 幂等键防重复执行
- 子 Agent 绑定校验辅助
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.agent.harness.state import ExecutionPlan, PlanStep, StepResult, StepStatus

# 可并行 fan-out 的检索类步骤（写文件 / 汇总不在此列）
RETRIEVAL_STEP_TYPES = frozenset(
    {"network_search", "database_query", "knowledge_base", "file_read"}
)

# 子 Agent 步骤必须走 task，禁止主 Agent 直接调文件工具
SUBAGENT_STEP_TYPES = frozenset(
    {"network_search", "database_query", "knowledge_base"}
)

FORBIDDEN_TOOLS_BY_STEP: dict[str, frozenset[str]] = {
    "network_search": frozenset({"generate_markdown", "convert_md_to_pdf"}),
    "database_query": frozenset({"generate_markdown", "convert_md_to_pdf", "internet_search"}),
    "knowledge_base": frozenset({"generate_markdown", "convert_md_to_pdf"}),
    "generate_markdown": frozenset({"internet_search", "execute_sql_query", "list_sql_tables"}),
}


@dataclass
class WorkerResultPayload:
    """工人回传的结构化载荷（监督者消费）。"""

    ok: bool = True
    summary: str = ""
    facts: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    confidence: float = 1.0
    error_code: str = ""
    worker: str = ""
    step_type: str = ""

    def to_context_snippet(self, max_chars: int = 600) -> str:
        parts = [self.summary or ""]
        if self.facts:
            parts.append("要点: " + "; ".join(self.facts[:5]))
        if self.sources:
            parts.append("来源: " + ", ".join(self.sources[:5]))
        if not self.ok and self.error_code:
            parts.append(f"[{self.error_code}]")
        text = " | ".join(p for p in parts if p.strip())
        return text[:max_chars]


@dataclass
class StepExecutionDelta:
    """单步执行对共享状态的增量（并行 join 用）。"""

    step_index: int
    step_result: StepResult
    assistants_called: list[str] = field(default_factory=list)
    tool_calls: int = 0
    unauthorized_tools: list[str] = field(default_factory=list)


def step_idempotency_key(session_id: str, step_index: int, step_type: str) -> str:
    return f"{session_id}:{step_index}:{step_type}"


def task_query_fingerprint(task_query: str) -> str:
    normalized = re.sub(r"\s+", " ", task_query.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def mark_parallel_retrieval_groups(plan: ExecutionPlan) -> ExecutionPlan:
    """【修改点】为连续检索步标记 parallel_group，供 Loop fan-out。"""
    group_id = 0
    i = 0
    steps = plan.steps
    while i < len(steps):
        if steps[i].step_type not in RETRIEVAL_STEP_TYPES:
            i += 1
            continue
        j = i
        while j < len(steps) and steps[j].step_type in RETRIEVAL_STEP_TYPES:
            j += 1
        if j - i >= 2:
            for k in range(i, j):
                steps[k].metadata["parallel_group"] = group_id
                steps[k].metadata["parallel_size"] = j - i
            group_id += 1
        i = j
    return plan


def find_parallel_batch(
    steps: list[PlanStep],
    start_index: int,
    *,
    enabled: bool,
) -> list[int]:
    """从 start_index 起，返回可并行执行的 step 下标列表（至少 2 步才并行）。"""
    if not enabled or start_index >= len(steps):
        return [start_index] if start_index < len(steps) else []

    step = steps[start_index]
    group = step.metadata.get("parallel_group")
    if group is None:
        return [start_index]

    batch = [
        idx
        for idx in range(start_index, len(steps))
        if steps[idx].metadata.get("parallel_group") == group
    ]
    return batch if len(batch) >= 2 else [start_index]


def parse_worker_payload(
    raw_content: str,
    *,
    step_type: str = "",
    subagent: str = "",
) -> WorkerResultPayload:
    """解析工人回传：优先 JSON，否则包装为 summary。"""
    text = (raw_content or "").strip()
    if not text:
        return WorkerResultPayload(
            ok=False,
            summary="",
            error_code="empty_worker_result",
            worker=subagent,
            step_type=step_type,
        )

    json_blob = _extract_json_object(text)
    if json_blob is not None:
        return WorkerResultPayload(
            ok=bool(json_blob.get("ok", True)),
            summary=str(json_blob.get("summary", text))[:4000],
            facts=[str(f) for f in json_blob.get("facts", []) if f][:10],
            sources=[str(s) for s in json_blob.get("sources", []) if s][:10],
            confidence=float(json_blob.get("confidence", 1.0) or 1.0),
            error_code=str(json_blob.get("error_code", "")),
            worker=str(json_blob.get("worker", subagent)),
            step_type=str(json_blob.get("step_type", step_type)),
        )

    return WorkerResultPayload(
        ok=True,
        summary=text[:4000],
        worker=subagent,
        step_type=step_type,
    )


def attach_structured_payload(result: StepResult, payload: WorkerResultPayload) -> StepResult:
    """将结构化载荷写入 StepResult.metadata。"""
    result.metadata["worker_payload"] = asdict(payload)
    result.metadata["structured_ok"] = payload.ok
    result.metadata["structured_json"] = bool(payload.facts or payload.sources) or (
        _extract_json_object((result.content or "").strip()) is not None
    )
    if payload.error_code:
        result.metadata["error_code"] = payload.error_code
    return result


def validate_structured_worker_payload(
    payload: WorkerResultPayload,
    step: PlanStep,
    *,
    require_json: bool = True,
) -> tuple[bool, str]:
    """【Phase 8】校验工人是否返回可用结构化载荷。"""
    if step.step_type not in SUBAGENT_STEP_TYPES:
        return True, ""
    if not payload.ok:
        return False, payload.error_code or "worker_failed"
    if not payload.summary.strip():
        return False, "empty_worker_result"
    if not require_json:
        return True, ""
    if payload.facts or payload.sources:
        return True, ""
    return False, "invalid_structured_output"


def build_strict_json_retry_instruction(step: PlanStep) -> str:
    """【Phase 8】结构化回传失败后的重试指令。"""
    worker = step.subagent or step.step_type
    return f"""
    【重试 — 必须纯 JSON】
    上次回传不符合结构化格式。请仅输出 JSON，不要任何解释文字：
    {{"ok":true,"summary":"...","facts":["..."],"sources":["..."],"confidence":0.9,"error_code":"","worker":"{worker}","step_type":"{step.step_type}"}}
    """


SYNTHESIS_STEP_TYPES = frozenset({"generate_markdown", "summarize", "convert_pdf"})


def aggregate_evidence_digest(step_results: list[StepResult]) -> dict[str, Any]:
    """【Phase 8】汇总多工人 facts/sources，供写报告步骤使用。"""
    facts_by_step: list[dict[str, Any]] = []
    all_facts: list[str] = []
    all_sources: list[str] = []
    seen_facts: set[str] = set()
    seen_sources: set[str] = set()

    for idx, result in enumerate(step_results, 1):
        payload = (result.metadata or {}).get("worker_payload") or {}
        if not isinstance(payload, dict):
            continue
        step_facts = [str(f) for f in payload.get("facts", []) if f]
        step_sources = [str(s) for s in payload.get("sources", []) if s]
        summary = str(payload.get("summary", ""))
        facts_by_step.append(
            {
                "step_index": idx,
                "step_type": result.step_type,
                "summary": summary[:800],
                "facts": step_facts[:10],
                "sources": step_sources[:10],
                "confidence": payload.get("confidence", 1.0),
            }
        )
        for fact in step_facts:
            key = fact.strip().lower()
            if key and key not in seen_facts:
                seen_facts.add(key)
                all_facts.append(fact)
        for source in step_sources:
            key = source.strip().lower()
            if key and key not in seen_sources:
                seen_sources.add(key)
                all_sources.append(source)

    return {
        "facts_by_step": facts_by_step,
        "all_facts": all_facts[:40],
        "all_sources": all_sources[:30],
        "step_count": len(facts_by_step),
    }


def format_evidence_digest_for_prompt(digest: dict[str, Any]) -> str:
    """格式化为写报告/汇总步骤的上下文块。"""
    if not digest.get("facts_by_step"):
        return ""
    lines = ["    【多源证据_digest — 写报告必须引用】"]
    for block in digest["facts_by_step"]:
        lines.append(
            f"  步骤{block['step_index']} [{block['step_type']}] "
            f"confidence={block.get('confidence', 1.0)}"
        )
        if block.get("summary"):
            lines.append(f"    摘要: {block['summary'][:400]}")
        for fact in block.get("facts") or []:
            lines.append(f"    - 事实: {fact}")
        for src in block.get("sources") or []:
            lines.append(f"    - 来源: {src}")
    if digest.get("all_facts"):
        lines.append("    【合并事实清单】")
        for fact in digest["all_facts"][:25]:
            lines.append(f"    * {fact}")
    return "\n".join(lines)


def check_subagent_binding(
    step: PlanStep,
    assistants_called: list[str],
    *,
    enforce: bool,
) -> tuple[bool, str]:
    """校验计划指定的子 Agent 是否被调用。"""
    if not enforce or not step.subagent:
        return True, ""
    if step.subagent in assistants_called:
        return True, ""
    return False, "wrong_subagent"


def check_unauthorized_tools(
    step: PlanStep,
    tools_invoked: list[str],
    *,
    enforce: bool,
) -> tuple[bool, list[str]]:
    """校验本步是否调用了禁止工具（计划绑定）。"""
    if not enforce:
        return True, []
    forbidden = FORBIDDEN_TOOLS_BY_STEP.get(step.step_type, frozenset())
    bad = [t for t in tools_invoked if t in forbidden]
    return len(bad) == 0, bad


def build_worker_output_instruction(step: PlanStep) -> str:
    """【修改点】要求子 Agent 回传结构化 JSON（监督者解析）。"""
    if step.step_type not in SUBAGENT_STEP_TYPES:
        return ""
    worker = step.subagent or step.step_type
    return f"""
    【工人结构化回传 — 必须遵守】
    完成本步后，你的最终回复必须是纯 JSON（不要 markdown 代码块），格式：
    {{
      "ok": true,
      "summary": "本步结论摘要",
      "facts": ["关键事实1", "关键事实2"],
      "sources": ["URL或表名或文件名"],
      "confidence": 0.0到1.0,
      "error_code": "",
      "worker": "{worker}",
      "step_type": "{step.step_type}"
    }}
    若失败：ok=false，并填写 error_code（如 search_empty / sql_empty / timeout）。
    """


class StepCheckpointStore:
    """步级 checkpoint：进程重启后可从已完成步骤继续。"""

    def __init__(self, session_dir: Path):
        self.path = session_dir / ".harness" / "checkpoint.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        *,
        session_id: str,
        task_fingerprint: str,
        next_step_index: int,
        step_results: list[StepResult],
        assistants_called: list[str],
        completed_keys: list[str],
        plan_summary: str = "",
    ) -> None:
        payload = {
            "session_id": session_id,
            "task_fingerprint": task_fingerprint,
            "next_step_index": next_step_index,
            "plan_summary": plan_summary,
            "assistants_called": assistants_called,
            "completed_step_keys": completed_keys,
            "step_results": [
                {
                    "step_type": r.step_type,
                    "content": r.content,
                    "compressed_content": r.compressed_content,
                    "metadata": r.metadata,
                }
                for r in step_results
            ],
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self) -> Optional[dict[str, Any]]:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def restore_step_results(self, data: dict[str, Any]) -> list[StepResult]:
        rows = data.get("step_results") or []
        return [
            StepResult(
                step_type=str(row.get("step_type", "")),
                content=str(row.get("content", "")),
                compressed_content=row.get("compressed_content"),
                metadata=dict(row.get("metadata") or {}),
            )
            for row in rows
        ]


class IdempotencyRegistry:
    """已完成步骤登记，避免 resume 重复调用外部工具。"""

    def __init__(self) -> None:
        self._completed: dict[str, StepResult] = {}

    def register(self, key: str, result: StepResult) -> None:
        self._completed[key] = result

    def get(self, key: str) -> Optional[StepResult]:
        return self._completed.get(key)

    def keys(self) -> list[str]:
        return list(self._completed.keys())

    def load_from_checkpoint(self, data: dict[str, Any], store: StepCheckpointStore) -> None:
        results = store.restore_step_results(data)
        keys = data.get("completed_step_keys") or []
        for key, result in zip(keys, results):
            self._completed[key] = result


def apply_step_status(plan: ExecutionPlan, completed_count: int) -> None:
    for idx, step in enumerate(plan.steps):
        if idx < completed_count:
            step.metadata["status"] = StepStatus.DONE.value
        else:
            step.metadata.setdefault("status", StepStatus.PENDING.value)


def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None
