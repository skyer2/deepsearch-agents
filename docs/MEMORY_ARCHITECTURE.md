# 深度研搜记忆层架构（Phase 18）

> 本文回答三件事：业界怎么做、本仓改前缺什么、改完后怎么讲。

## 1. 总判断

用户给出的业界分层判断是对的：**没有一家把 Memory 做成一个向量库就完事**。深度研搜的记忆必须：

1. **工作记忆可压**（Compaction）——单次任务别爆窗、别丢来源
2. **会话可 replay**（Session / Checkpoint / Event Log）
3. **长期层 curated**——抽取 fact，不是 dump 对话或网页全文
4. **写入可溯源、可删、可过期、可冲突合并**

但对照 **本仓 2026 年代码**，原先第六节的对照表已经过时：Phase 12/15 已经有类型化 fact、SQLite、Hybrid Recall、Jaccard 合并、PII、审计。真正的生产缺口不在「有没有 fact 抽取」，而在：

| 缺口 | 风险 | Phase 18 对策 |
|---|---|---|
| `user_id` 默认 = `session_id`，且走进程级 env | FastAPI 多用户串写 | 请求级 `MemoryIdentity` + ContextVar |
| 网页原文可进长期记忆 | 持久化提示注入 | `TrustTier` + 无出处不写 + 合成步准入门 |
| 没有项目域 | 同专题重复搜同一批源 | `project_id` + 来源台账 |
| 只有 UPDATE、没有 SUPERSEDE | 过时结论覆盖不清 | ADD/UPDATE/SUPERSEDE/NOOP |
| HITL 编辑/拒绝不沉淀 | Agent 不会「越用越会」 | `WriteSource.HITL` 程序性记忆 |
| embedding 在 SQLite 事务内 await | 锁等待、事件循环卡顿 | 事务外 embed + WAL + `to_thread` |
| finalize 覆盖 `obs_memory_saved_count` | 观测丢步内写入 | 累加而非赋值 |

## 2. 五层对照（业界 → 本仓）

```text
工作记忆   Compaction + 子 Agent 隔离
           → ContextCompressor + prior 摘要 + evidence digest + token 预算

会话记忆   Session / Checkpoint / Event Log
           → LoopState + StepCheckpointStore + JSONL trace
           → LangGraph InMemorySaver（DeepAgent 图内，进程级）

情节记忆   「上次调研的结论」
           → MemoryStore semantic/episodic + 项目加权召回

用户记忆   偏好 / 领域 / 交付格式
           → preference + user_explicit（TRUSTED）

程序性记忆 怎么查、用户改过什么
           → procedural + HITL 写入；Skills/AGENTS.md 仍属提示词而非 Memory

RAG        外部共享知识库
           → RAGFlow 工具（不是 MemoryStore）
```

长期层再拆 **信任三级**，这是研搜相对聊天助手的特化：

```text
trusted   用户亲口说的、HITL 批准的、系统种子     → 写报告可引用
derived   内部库 / 知识库 / 带引用的报告结论         → 写报告可引用
untrusted 外部网页原文，即便带 URL                   → 只作线索，合成步默认不注入
```

## 3. 写入与召回路径

```text
BUILD_CONTEXT
  recall(task, identity) ──► 四类知识记忆 + 项目来源台账注入 prompt

EXECUTE 检索步成功
  有出处？──否──► 网页步丢弃（不写长期）
           ──是──► STEP_INCREMENTAL（untrusted 或 derived）
                 └─ URL 记入 source_ledger

EXECUTE 合成步（写报告）
  二次 recall(min_trust=derived) ──► 丢掉脏网页结论

HITL reject/edit
  procedural + trusted ──► 「用户改过什么」

FINALIZE
  抽取 curated findings ──► ADD/UPDATE/SUPERSEDE
  异步 consolidation ──► 衰减 / 晋升 / 硬清理
```

冲突动作对齐 Mem0，但研搜多了 SUPERSEDE：旧记录软删并留下取代链，方便审计「结论何时被推翻」。低信任不能覆盖高信任（网页改不了用户偏好）。

## 4. 身份模型

```text
tenant_id  → 企业隔离
user_id    → 真实用户；缺失则退化为 session 并标 ephemeral
project_id → 研究专题（「别重复搜」靠它）
session_id → 仅溯源，不参与隔离
```

解析优先级：请求参数 > ContextVar > 环境变量 > session 退化。

生产建议：

- API 调用 `/api/task` 必须带 `user_id`（以及可选 `tenant_id` / `project_id`）
- `HARNESS_MEMORY_REQUIRE_IDENTITY=true`，拒绝匿名写入
- 不要把 `HARNESS_MEMORY_USER_ID` 当多用户方案（那是单进程默认用户）

## 5. 和 RAG 的边界（保持不变，说清楚）

- **RAGFlow**：多人共享、相对静态的文档。结果进本次 `step_results`，是证据，不是记忆。
- **Memory**：关于这个用户 / 这个项目 / 这个 Agent 自己的状态与结论。
- 网页抓取全文 **永远不要** 当长期 memory；只抽 fact，且必须带 provenance。

## 6. 面试 30 秒 / 2 分钟

**30 秒**

> 深度研搜的记忆不是单一向量库。我们分三层：Session 管原始轨迹和 replay；Compaction 管单次任务的工作记忆；长期层存 curated findings。长期层按用户和项目隔离，写入要过信任分级——网页无出处不落库，写报告只召回 derived 以上的结论。冲突走 ADD/UPDATE/SUPERSEDE，HITL 的拒绝和修改沉淀成程序性记忆。RAG 走工具，不和 Memory 混用。

**被追问「和 Mem0 / Perplexity Brain 的关系」**

> 我们没有把 Mem0 当内核。Mem0 的 ADD/UPDATE/DELETE 语义我们自研实现了，并加了研搜特有的 SUPERSEDE 和信任准入。Perplexity 把用户 Memory 和 Agent Brain 分开，我们对应的是 preference/semantic vs source_ledger + HITL procedural。框架侧 Compaction 已经是一等能力，这比先上复杂 Memory OS 更划算。

**被追问「最大的坑」**

> 持久化注入。把不可信网页写进长期记忆，等于一次注入变成每次召回都中招。所以我们把「无出处不写」和「合成步过滤 untrusted」做成硬门，而不是靠 prompt 说「请谨慎」。
