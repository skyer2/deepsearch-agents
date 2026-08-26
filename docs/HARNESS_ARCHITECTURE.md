# Harness 运行时架构（Phase 20）

> **权威模型**：while 外环（领域 Harness）+ 按步工人 + 一份落库的 `LoopState`。  
> LangGraph 只跑单步；DeepAgents 只组装工人，不再当第二导演。

对照：[教学版 deepsearch-agents](https://github.com/didilili/deepsearch-agents) 是一次 `create_deep_agent` 黑盒跑完全程。本仓库在其上加了显式 Loop 之后，Phase 20 把执行入口从「每步仍进全能主图」收成「计划指定谁就直调谁」。

---

## 整体架构

```text
┌─────────────────────────────────────────────────────────────────┐
│ 体验 / 服务                                                       │
│  React 时间线 · FastAPI · WebSocket · /health · golden eval       │
└───────────────────────────────┬─────────────────────────────────┘
                                │ run(task, session_id)
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│ 领域 Harness（唯一导演）  AgentHarness.while                       │
│  understand → plan → HITL → build_context                         │
│  → execute / parallel_execute → compress → validate → recover     │
│  → finalize / abort                                               │
│  计划绑定 · 引用 · Memory 策略 · Kill Switch · 评测轨迹            │
│  权威状态：LoopState  →  output/session_*/.harness/checkpoint.json │
└───────────────┬───────────────────────────┬─────────────────────┘
                │ invoke(本步工人)           │ invoke(写文件主图)
                ▼                           ▼
┌──────────────────────────┐   ┌──────────────────────────────────┐
│ 检索工人（按步直调）       │   │ 合成主图                          │
│ network_search            │   │ generate_markdown / PDF / 读附件 │
│   仅 internet_search      │   │ interrupt_on = 写文件 HITL       │
│ database_query            │   │ 没有检索子 Agent                 │
│   仅 SQL 三件套            │   └───────────────┬──────────────────┘
│ knowledge_base            │                   │
│   仅 RAGFlow              │                   ▼
└────────────┬─────────────┘   ┌──────────────────────────────────┐
             │                 │ 发动机 LangGraph                   │
             └────────────────►│ 单步 messages · 本步 thread_id     │
                               │ InMemorySaver（不作为任务进度）     │
                               └──────────────────────────────────┘
```

配置开关（默认开启）：

- `orchestration.direct_worker_invoke`：检索步直调工人
- `orchestration.persist_loop_state`：checkpoint 写入整份 LoopState

回退：`HARNESS_DIRECT_WORKER_INVOKE=false` 时主图重新挂上三个子 Agent，走旧的 `task` 路由（评测/对比用）。

---

## 一次任务怎么跑

以「查阿莫西林公开市场和库存，写 Markdown」为例：

1. **Harness 计划**：`network_search` → `database_query` → `generate_markdown`（可并行前两步）。
2. **步 1**：`resolve_execute_target` 返回网络搜索工人图，**不经过主 Agent**。工人没有写文件工具。
3. **步 2**：直调数据库工人。SQL 仍走 `ToolGateway`。
4. **Join**：facts/sources 进 evidence digest 与 `working_notes`。
5. **步 3**：才唤醒主图写 MD；`interrupt_on` 仍可用。
6. **落库**：每步成功后把 LoopState（计划、结果、笔记、证据、HITL 等待）写入 `checkpoint.json`。进程重启后 **跳过 understand/plan**，从 `next_step_index` 续跑。图内 HITL（写文件 interrupt）跨进程无法恢复，会从该步重跑——checkpoint 里会留下 `hitl_waiting.gate_type=interrupt_on` 以便区分。

---

## 和教学版的差别

| | [didilili/deepsearch-agents](https://github.com/didilili/deepsearch-agents) | 本仓库（Phase 20） |
|--|--------------------------------------------------------------------------|-------------------|
| 编排 | 主 Agent 自己决定调哪个子 Agent | Harness 外置计划，检索步直调工人 |
| 工具隔离 | 靠 prompt「请调用网络搜索助手」 | 工人图物理上没有写文件/跨源工具 |
| 失败 | 模型再试或任务失败 | validate 失败码 + recover / replan + Kill Switch |
| 引用 / 记忆 / 评测 | 无 | citation、分层 Memory、golden eval |
| 进度 | 无任务级 checkpoint | **LoopState 一份 JSON 为权威**；LangGraph Saver 只覆盖单步窗口 |
| HITL | 无或仅工具中断 | 计划审批 / 查库 step gate / 写文件 interrupt；等待态写入 LoopState |

教学版解决「DeepAgents 怎么把三个专家跑起来」。本仓库解决「一次研搜如何按剧本交付，并且执行入口与计划一致」。

---

## 面试怎么说

> 研搜要的是领域 Harness，不是再造一个 LangGraph。外环 while 管计划、校验、护栏、评测；检索步按计划直调工人图，主图只写文件。任务进度只认落库的 LoopState，不把 DeepAgents 当第二导演。

相关代码：`app/agent/harness/loop.py`、`worker_runtime.py`、`loop_state_store.py`、`app/agent/main_agent.py`。
