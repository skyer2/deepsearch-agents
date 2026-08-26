"""

任务理解与计划生成



Phase 1 规则引擎；Phase 14 结构化槽位 + 置信度 + 歧义澄清 + Plan 校验。

"""



from __future__ import annotations



from app.agent.harness.intent_slots import (

    IntentSlots,

    apply_clarification_patch,

    build_clarification_question,

    compute_rule_confidence,

    detect_ambiguity_flags,

    extract_slots,

    infer_output_preference,

    resolve_deliverable_from_slots,

)

from app.agent.harness.state import ExecutionPlan, PlanStep, TaskIntent



NETWORK_KEYWORDS = ("搜索", "网络", "互联网", "公开", "新闻", "趋势", "资料", "检索")

DATABASE_KEYWORDS = ("数据库", "库存", "销售", "sql", "表", "查询", "药品", "商品")

KB_KEYWORDS = ("知识库", "ragflow", "内部", "文档", "白皮书", "研报")

FILE_READ_KEYWORDS = ("上传", "附件", "读取文件", "文档内容")

PDF_KEYWORDS = ("pdf", "PDF")

MD_KEYWORDS = ("markdown", "Markdown", "MD", "md")

REPORT_KEYWORDS = ("报告", "研报", "白皮书")

REPORT_ACTION_KEYWORDS = ("生成", "整理", "撰写", "输出", "导出")

STRUCTURED_LIST_KEYWORDS = ("列出", "列举", "条目")





def _looks_like_report_request(query: str) -> bool:

    """「整理报告/生成报告」才算 md，避免普通「生成」误判。"""

    has_report = any(k in query for k in REPORT_KEYWORDS)

    has_action = any(k in query for k in REPORT_ACTION_KEYWORDS)

    return has_report and has_action





def _infer_deliverable(query: str, slots: IntentSlots) -> str:

    q = query.lower()

    q_raw = query

    if any(k in q for k in PDF_KEYWORDS) or "pdf" in q:

        return "pdf"

    if any(k in q_raw for k in MD_KEYWORDS):

        return "md"

    if _looks_like_report_request(q_raw):

        return "md"

    # 【Phase 14】列出 N 条 + 来源链接 → 默认 md（可 HITL 澄清覆盖）

    if slots.item_count and slots.require_citations:

        return "md"

    if slots.require_citations and any(k in q_raw for k in STRUCTURED_LIST_KEYWORDS):

        return "md"

    pref = resolve_deliverable_from_slots(slots, "text")

    if pref != "text":

        return pref

    return "text"





def understand_task(task_query: str, has_uploaded_files: bool = False) -> TaskIntent:

    """规则理解 + 结构化槽位 + 置信度 + 歧义标记。"""

    q = task_query.lower()

    q_raw = task_query



    slots = extract_slots(q_raw)

    needs_network = any(k in q_raw for k in NETWORK_KEYWORDS)

    needs_database = any(k in q_raw for k in DATABASE_KEYWORDS)

    needs_knowledge_base = any(k in q_raw for k in KB_KEYWORDS)

    needs_file_read = has_uploaded_files or any(k in q_raw for k in FILE_READ_KEYWORDS)



    deliverable = _infer_deliverable(q_raw, slots)

    slots.output_preference = infer_output_preference(

        q_raw,

        deliverable=deliverable,

        require_citations=slots.require_citations,

        item_count=slots.item_count,

    )



    if not any([needs_network, needs_database, needs_knowledge_base, needs_file_read]):

        needs_network = True



    keywords = [k for k in NETWORK_KEYWORDS + DATABASE_KEYWORDS + KB_KEYWORDS if k in q_raw]

    ambiguity_flags = detect_ambiguity_flags(

        q_raw,

        deliverable=deliverable,

        slots=slots,

        needs_network=needs_network,

        needs_database=needs_database,

        needs_knowledge_base=needs_knowledge_base,

    )

    rule_confidence = compute_rule_confidence(

        query=q_raw,

        needs_network=needs_network,

        needs_database=needs_database,

        needs_knowledge_base=needs_knowledge_base,

        needs_file_read=needs_file_read,

        deliverable=deliverable,

        slots=slots,

        ambiguity_flags=ambiguity_flags,

    )

    needs_clarification = bool(ambiguity_flags) or rule_confidence < 0.72

    clarification_question = (

        build_clarification_question(deliverable, ambiguity_flags, slots)

        if needs_clarification

        else ""

    )



    return TaskIntent(

        raw_query=task_query,

        summary=f"研搜任务，交付物={deliverable}，置信度={rule_confidence}",

        needs_network=needs_network,

        needs_database=needs_database,

        needs_knowledge_base=needs_knowledge_base,

        needs_file_read=needs_file_read,

        deliverable=deliverable,  # type: ignore[arg-type]

        keywords=keywords,

        planner_source="rules",

        intent_confidence=rule_confidence,

        rule_confidence=rule_confidence,

        slots=slots,

        ambiguity_flags=ambiguity_flags,

        needs_clarification=needs_clarification,

        clarification_question=clarification_question,

    )





def build_plan(intent: TaskIntent) -> ExecutionPlan:

    """根据意图生成有序执行计划。"""

    steps: list[PlanStep] = []



    if intent.needs_file_read:

        steps.append(

            PlanStep(

                step_type="file_read",

                description="读取用户上传的附件内容",

            )

        )

    if intent.needs_network:

        steps.append(

            PlanStep(

                step_type="network_search",

                description="检索互联网公开资料",

                subagent="网络搜索助手",

            )

        )

    if intent.needs_database:

        steps.append(

            PlanStep(

                step_type="database_query",

                description="查询 MySQL 结构化业务数据",

                subagent="数据库查询助手",

            )

        )

    if intent.needs_knowledge_base:

        steps.append(

            PlanStep(

                step_type="knowledge_base",

                description="检索 RAGFlow 内部知识库",

                subagent="RAGFlow助手",

            )

        )

    if intent.deliverable == "md":

        steps.append(

            PlanStep(

                step_type="generate_markdown",

                description="汇总信息并生成 Markdown 报告",

            )

        )

    elif intent.deliverable == "pdf":

        steps.extend(

            [

                PlanStep(

                    step_type="generate_markdown",

                    description="汇总信息并生成 Markdown 报告",

                ),

                PlanStep(

                    step_type="convert_pdf",

                    description="将 Markdown 转换为 PDF",

                ),

            ]

        )

    else:

        steps.append(

            PlanStep(

                step_type="summarize",

                description="汇总多源信息并输出最终回答",

            )

        )



    summary = " → ".join(step.description for step in steps)

    return ExecutionPlan(steps=steps, summary=summary)





def validate_plan_against_intent(intent: TaskIntent, plan: ExecutionPlan) -> tuple[bool, list[str]]:

    """【Phase 14】Plan 与 Intent 一致性校验。"""

    issues: list[str] = []

    step_types = [s.step_type for s in plan.steps]



    if intent.needs_network and "network_search" not in step_types:

        issues.append("missing_network_search")

    if intent.needs_database and "database_query" not in step_types:

        issues.append("missing_database_query")

    if intent.needs_knowledge_base and "knowledge_base" not in step_types:

        issues.append("missing_knowledge_base")

    if intent.needs_file_read and "file_read" not in step_types:

        issues.append("missing_file_read")



    if intent.deliverable == "md" and "generate_markdown" not in step_types:

        issues.append("missing_generate_markdown")

    if intent.deliverable == "pdf":

        if "generate_markdown" not in step_types:

            issues.append("missing_generate_markdown")

        if "convert_pdf" not in step_types:

            issues.append("missing_convert_pdf")

    if intent.deliverable == "text" and "summarize" not in step_types:

        issues.append("missing_summarize")



    if not plan.steps:

        issues.append("empty_plan")



    return len(issues) == 0, issues





def finalize_plan(plan: ExecutionPlan) -> ExecutionPlan:

    """计划生成后标记并行组与初始状态。"""

    from app.agent.harness.orchestration import mark_parallel_retrieval_groups
    from app.research.runtime.scheduler import annotate_plan_tasks

    plan = annotate_plan_tasks(mark_parallel_retrieval_groups(plan))

    for step in plan.steps:

        step.metadata.setdefault("status", "pending")

    return plan





def detect_multi_intent(intent: TaskIntent) -> bool:

    """一句话多意图：需要 2+ 信息源时触发 plan HITL。"""

    count = sum(

        [

            intent.needs_network,

            intent.needs_database,

            intent.needs_knowledge_base,

            intent.needs_file_read,

        ]

    )

    return count >= 2





def should_request_plan_review(intent: TaskIntent, *, min_confidence: float = 0.75) -> bool:

    """【Phase 14】多意图 / 低置信 / 曾歧义 → 计划审批。"""

    if detect_multi_intent(intent):

        return True

    if intent.intent_confidence < min_confidence:

        return True

    if intent.ambiguity_flags and not intent.clarification_resolved:

        return True

    if "deliverable_ambiguous" in intent.ambiguity_flags:

        return True

    return False





def apply_intent_clarification(intent: TaskIntent, edited_action: dict) -> TaskIntent:

    """HITL 澄清决策合并回 TaskIntent。"""

    patched = apply_clarification_patch(intent.to_dict(), edited_action)

    updated = TaskIntent.from_dict(patched)

    updated.raw_query = intent.raw_query

    if not updated.summary:

        updated.summary = f"研搜任务，交付物={updated.deliverable}（已澄清）"

    updated.intent_confidence = max(updated.intent_confidence, updated.rule_confidence)

    return updated





def auto_resolve_clarification(intent: TaskIntent) -> TaskIntent:

    """无 HITL 时在超时/评测模式下保守解析歧义。"""

    if not intent.needs_clarification:

        return intent

    resolved = TaskIntent.from_dict(intent.to_dict())

    if "deliverable_ambiguous" in resolved.ambiguity_flags:

        if resolved.slots.require_citations and resolved.slots.item_count:

            resolved.deliverable = "md"

            resolved.slots.output_preference = "file_md"

        else:

            resolved.deliverable = "text"

            resolved.slots.output_preference = "chat"

    resolved.needs_clarification = False

    resolved.clarification_resolved = True

    resolved.clarification_question = ""

    resolved.planner_reason = (resolved.planner_reason or "") + " [auto_resolve]"

    resolved.summary = f"研搜任务，交付物={resolved.deliverable}（auto_resolve）"

    return resolved





def apply_plan_edits(plan: ExecutionPlan, steps_payload: list[dict]) -> ExecutionPlan:

    """HITL Edit-in-the-Loop：应用用户编辑后的计划步骤。"""

    new_steps: list[PlanStep] = []

    for item in steps_payload:

        if not isinstance(item, dict):

            continue

        step_type = str(item.get("step_type", "")).strip()

        if not step_type:

            continue

        new_steps.append(

            PlanStep(

                step_type=step_type,

                description=str(item.get("description", step_type)),

                subagent=item.get("subagent"),

                metadata={"hitl_edited": True},

            )

        )

    if not new_steps:

        return plan

    summary = " → ".join(step.description for step in new_steps)

    return finalize_plan(ExecutionPlan(steps=new_steps, summary=summary))





def dynamic_replan(

    plan: ExecutionPlan,

    insert_after_index: int,

    reason: str,

    extra_steps: list[PlanStep] | None = None,

) -> ExecutionPlan:

    """执行中动态插入步骤（如交叉验证、补充检索）。"""

    steps = list(plan.steps)

    inserts: list[PlanStep] = list(extra_steps or [])



    if not inserts:

        if reason in {"sql_empty", "wrong_subagent", "citation_coverage_low"}:

            inserts.append(

                PlanStep(

                    step_type="network_search",

                    description="【动态重规划】补充公开资料交叉验证",

                    subagent="网络搜索助手",

                    metadata={"replan_reason": reason},

                )

            )

        elif reason == "user_replan":

            inserts.append(

                PlanStep(

                    step_type="summarize",

                    description="【动态重规划】按用户编辑重新汇总",

                    metadata={"replan_reason": reason},

                )

            )



    pos = min(max(insert_after_index + 1, 0), len(steps))

    for offset, step in enumerate(inserts):

        steps.insert(pos + offset, step)



    summary = " → ".join(step.description for step in steps)

    return finalize_plan(ExecutionPlan(steps=steps, summary=summary))





def plan_to_editable_dict(plan: ExecutionPlan) -> list[dict]:

    """供前端 HITL 计划编辑面板使用的 JSON 结构。"""

    return [

        {

            "step_type": step.step_type,

            "description": step.description,

            "subagent": step.subagent,

        }

        for step in plan.steps

    ]


