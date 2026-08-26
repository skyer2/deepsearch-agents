# Deep Research Domain Harness

> **原则**：One authoritative control plane, one durable workflow state, bounded local agent autonomy.

本仓库的 Harness 是 **领域控制面**，LangGraph 是它选择的 **execution runtime**，不是上一层产品。

```text
Deep Research Domain Harness     Intent / Plan / Policy / Eval / Evidence
        │
        ▼
LangGraph StateGraph             可执行表示（node / edge / Send / interrupt）
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
| 问题拆成哪些研究方向、query 怎么迭代 | **LLM Planner / Worker** |
| PlanPatch 是否接受 | **Code validation** |
| 何时强制停止 | **Code** |

> Control invariants deterministic，semantic decisions agentic.

LangGraph 是成熟的 durable workflow runtime 之一，不是行业协议。选用它是因为本项目已依赖它，并且需要 interrupt、fan-out、subgraph、streaming。

## 当前落地（Phase 21）

已完成：

- 删除 Main `create_deep_agent` 二次路由
- `WorkerRegistry` 按 `step_type` 直调 `langchain.agents.create_agent` Leaf
- 写文件 HITL：`interrupt()` 在副作用之前（PURE → HITL → SIDE EFFECT）
- Plan 带 `task_id` / `depends_on` / `plan_version`
- 幂等键升级为 `run_id + plan_version + task_id + action_id`，并兼容旧 `step_index` 键
- 薄 `StateGraph`（`app/research/runtime/graph.py`）已可编译；生产调度默认仍是 `AgentHarness` while（`graph_runtime_enabled: false`）

刻意保留：

- `IdempotencyRegistry`（checkpointer ≠ 外部副作用 exactly-once）
- ContextBuilder / MemoryPolicy / Citation / MCP Gateway / Validator / Eval
- `StepCheckpointStore`（Phase 3 切 graph 为唯一 workflow authority 后再删）

下一步：

1. `graph_runtime_enabled: true` 后用 `Send` 替换 `asyncio.gather + deepcopy`
2. 并行单元从数据源工人升级为 research task
3. 合成后加 claim / citation verifier 节点

## 几个 Agent？

只有 **N × Research Worker**（以及需要时的 DB/文档专家）是 Agent。Intent / Planner / Scheduler / Memory / Gateway 都不是 Agent。
