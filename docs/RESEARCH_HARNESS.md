# Deep Research Domain Harness

> **原则**：One authoritative control plane, one durable workflow state, bounded local agent autonomy.

本仓库的 Harness 是 **领域控制面**，LangGraph 是它选择的 **execution runtime**，不是上一层产品。

```text
Deep Research Domain Harness     Intent / Plan / Policy / Eval / Evidence
        │
        ▼
LangGraph StateGraph             可执行表示（node / edge / Send / interrupt）
                                 ← 唯一 workflow authority
        │
        ▼
LangGraph Runtime                State / Checkpoint / Stream
        │
        ▼
Leaf Agent / Tool / MCP          create_agent()；必要时才用精简 DeepAgent
```

## 决策边界

| 决策 | 谁负责 |
|------|--------|
| 并发上限、budget、timeout、retry、依赖、权限、HITL 是否等待 | **Code** |
| checkpoint / resume | **LangGraph Runtime** |
| 不要联网 / DB 权限 / 工具白名单 | **Source policy（代码）** |
| 应研究哪些实体与维度 | **Lead Planner LLM（仅 DYNAMIC）** |
| 某个任务搜什么 query | **Worker Agent** |
| PlanPatch 是否接受 | **Code validation** |
| 何时强制停止 | **Code** |

> Control invariants deterministic，semantic decisions agentic.

LangGraph 是成熟的 durable workflow runtime 之一，不是行业协议。选用它是因为本项目已依赖它，并且需要 interrupt、fan-out、subgraph、streaming。

## 当前落地

已完成：

- 删除 Main `create_deep_agent` 二次路由
- `WorkerRegistry` 按 `step_type` 直调 `langchain.agents.create_agent` Leaf；未注册 step **fail-closed**
- 合成工人 prompt 只消费证据，不再假装调度三个专家
- 写文件 HITL：Leaf `interrupt()` 在副作用之前（PURE → HITL → SIDE EFFECT）
- 全局 HITL（澄清 / 计划审批 / step gate）：图内 `interrupt()`，`HitlCoordinator` 只桥接 `POST /api/task/{id}/resume`
- Plan 带 `task_id` / `depends_on` / `plan_version`
- 幂等键：`run_id + plan_version + task_id + action_id`（兼容旧 `step_index` 键）
- **生产入口** `run_deep_agent` → `AgentHarness.run` → `research_graph.ainvoke`（`graph_runtime_enabled: true`）
- 并行研究任务走图内 `Send`（`research` + 旧数据源步）
- **Hybrid planning**：DIRECT / TEMPLATE / DYNAMIC。Lead Planner 只输出 objective DAG，不掌握 runtime；来源禁令由 policy 强制

刻意保留：

- `IdempotencyRegistry`（checkpointer ≠ 外部副作用 exactly-once）
- planner / ContextBuilder / MemoryPolicy / Citation / MCP Gateway / Validator / Recovery / Eval
- `StepCheckpointStore` 仍用于 LoopState 热恢复；LangGraph checkpointer 是 interrupt/resume 权威（Postgres checkpointer 为后续加固）

`AgentHarness._run_legacy_loop()` 仅在 `graph_runtime_enabled: false` 或未安装 langgraph 时回退。

下一步：

1. 把 LoopState 热恢复完全交给 durable LangGraph checkpointer，再删除 `StepCheckpointStore`
2. 合成后独立 claim / citation verifier 节点
3. 删除 `check_subagent_binding` 兼容 metrics 与 Main Agent fallback 残留

## 几个 Agent？

只有 **N × Research Worker**（以及需要时的 DB/文档专家）是 Agent。Intent / Planner / Scheduler / Memory / Gateway 都不是 Agent。
