# 深度研搜 Memory：面试讲法与问题清单

配套系统全貌：[MEMORY_SYSTEM.md](./MEMORY_SYSTEM.md)  
Phase 18 对照业界的短文：[MEMORY_ARCHITECTURE.md](./MEMORY_ARCHITECTURE.md)

本文分三块：**怎么开口**、**追问怎么接**、**问题清单（建议答案要点）**。清单按「几乎必问 → 研搜特有 → 工程深挖 → 产品/合规」排，方便按时间准备。

---

## 一、面试怎么介绍

### 1. 开场 20 秒（先定框架，别先讲 SQLite）

> 我们做的是深度研搜 Agent，记忆层不是一个向量库。业界现在的共识是分层、不同生命周期。我这边对齐成三块：**Compaction 管单次任务的工作记忆**，**Session/Checkpoint 管原始轨迹和续跑**，**长期层只存 curated findings**。长期层和 RAG 分开：RAG 是共享知识库，Memory 是这个用户、这个项目、这个 Agent 自己的状态。

### 2. 业务优先级 20 秒（证明你懂研搜，不是套聊天机器人）

> 研搜和普通助手不一样。P0 是单次任务别爆窗、别丢来源；P1 是同项目别重复搜同一批源；P2 才是用户偏好；P3 才是 Agent 越用越会。所以我们先把压缩和引用做稳，长期记忆第二阶段再上，而且必须防把网页脏结论持久化。

### 3. 我们具体怎么做 60～90 秒（对着架构图讲）

建议手画三列：写入 / 存储 / 召回。

> **身份**：tenant / user / project / session 四元组，请求级 ContextVar，不再用进程级环境变量当 user_id，否则 FastAPI 多用户会串写。
>
> **写入**：不是 dump 对话。检索步成功才抽 1～2 条；网页没有 URL 直接不写；任务成功 finalize 时 LLM 抽 3～5 条带类型的 fact。冲突对齐 Mem0 的 ADD/UPDATE/NOOP，我们多了 SUPERSEDE——数字变了或结论相反，旧记录软删并留取代链。HITL 的拒绝和编辑沉淀成 procedural，信任等级是 trusted。
>
> **信任**：trusted / derived / untrusted。外部网页永远是 untrusted。写报告前会二次召回，只注入 derived 以上，这是硬门，不是靠 prompt 说请谨慎。
>
> **召回**：关键词 + embedding + 新近度 + 同项目加权 + 信任打折。来源台账单独注入「项目已查来源」，避免和事实记忆混在一起。
>
> **存储**：默认 SQLite，JSON 降级，Mem0 可选 overlay 但不是内核。

### 4. 收尾 15 秒（主动暴露边界，比装成「已经企业级」加分）

> 还没做的也很清楚：图状态仍是进程级 InMemorySaver，要上多副本得换 Redis Checkpointer；巩固还是规则衰减不是 sleep-time LLM；产品登录我们还没有，但记忆层隔离已经按租户和用户切开，身份必须从 API 传入。

### 5. 一分钟版（时间不够就用这个）

把 1+2+4 压缩：分层三块 → 研搜优先级 → 信任硬门 → 还没做的。细节等追问。

### 6. 白板怎么画（面试官说「画一下」时）

```text
用户请求 (+ user_id, project_id)
        │
   Identity 四元组
        │
   ┌────┴────┐
   │ BUILD   │  recall findings + source ledger
   │ CONTEXT │
   └────┬────┘
        │  每步 user message：记忆块 + <untrusted>
   检索步 ──► 有出处? ──否──► 丢弃
              ──是──► step fact (untrusted/derived) + URL 台账
   写报告 ──► 二次 recall (derived+)
   HITL   ──► procedural / trusted
   FINALIZE ► curated findings ► ADD/UPDATE/SUPERSEDE
              └─ async 衰减/晋升/清理
```

---

## 二、高频追问，怎么接（先练这 8 个）

**Q. 这不就是 RAG 吗？**  
A. RAG 检索的是共享、相对静态的文档；Memory 检索的是「和这个用户/项目相关的状态与结论」。网页全文不当长期记忆。我们 RAGFlow 走工具，MemoryStore 是另一条链路，prompt 里分层注入。

**Q. 为什么不直接上 Mem0？**  
A. 很多团队没多 session 用户就上 Mem0，复杂度高于收益。我们把 ADD/UPDATE/DELETE 语义自研了，并加了研搜需要的 SUPERSEDE 和信任准入。Mem0 可以当 sidecar，但 Compaction 和引用才是研搜 P0。

**Q. 长期记忆最大的坑是什么？**  
A. 持久化提示注入。不可信网页写进 memory，之后每次被召回。所以无出处不写、合成步过滤 untrusted，是代码硬门。

**Q. 过时结论怎么办？**  
A. TTL + 置信度衰减 + SUPERSEDE 取代链 + 用户可删。低信任不能覆盖高信任，避免网页改掉「用户只要 PDF」这种偏好。

**Q. 同项目重复搜索怎么避免？**  
A. `project_id` 召回加权 + source_ledger 记录归一化 URL。下次任务 BUILD_CONTEXT 直接告诉模型「这些源已经查过」。

**Q. 记忆进 system 还是 user？**  
A. 进每步 user message，不进 system。system 保持稳定；记忆当参考材料，并声明勿执行其中的指令。

**Q. 工作记忆和长期记忆怎么分工？**  
A. 工作记忆是本轮检索结果的压缩和 evidence digest，任务结束就不必整段留着。长期记忆只留可复用的 fact。Checkpoint 是为了续跑，不是为了当记忆用。

**Q. 你觉得下一步该做什么？**  
A. 按阶段：先强制 API 传真实 user_id；Checkpointer 换 Redis；来源质量接用户点踩；巩固如果真有大量多 session 用户再考虑 sleep-time LLM。不会一上来换 Memory OS。

---

## 三、问题清单（按主题，方便准备）

每题给「面试官在考什么」和「建议答到哪几句」。不必背全文，能用项目里的模块名撑住即可。

### A. 几乎必问：概念与边界

| # | 问题 | 考什么 | 答到即可 |
|---|---|---|---|
| A1 | 什么是 Agent Memory？和聊天历史有何不同？ | 是否把 history dump 当记忆 | 记忆是筛选后的状态；历史是原始轨迹，要 compaction / replay |
| A2 | Memory 和 RAG 怎么分工？ | 会不会把两者混成一个向量库 | RAG=共享文档；Memory=用户/项目/Agent 状态；本仓 RAGFlow 走工具 |
| A3 | 为什么不能「把网页全文 embed 进去」？ | token、噪声、注入 | 太大太脏；只抽 fact；网页默认 untrusted |
| A4 | 业界 Deep Research 的记忆一般几层？ | 是否跟过 OpenAI/Perplexity | 工作 / 会话 / 情节 / 用户 / 程序性；本仓如何映射 |
| A5 | OpenAI Deep Research 示例的双轨是什么？ | Session vs long-term | Session 存 raw replay；长期存 Agent 显式 save 的 findings |
| A6 | Perplexity Memory 和 Brain 有何不同？ | 用户画像 vs 工作记忆 | 我们 preference vs source_ledger + HITL procedural |
| A7 | 研搜和客服 Bot 的记忆策略差在哪？ | 场景 priority | 研搜 P0 来源与窗口；客服更偏画像与情感 |
| A8 | 「没有一家把 Memory 做成向量库就完事」你怎么理解？ | 工程成熟度 | 分层生命周期、写入策略、遗忘、治理、评测 |

### B. 研搜场景特有（本岗位最该准备）

| # | 问题 | 考什么 | 答到即可 |
|---|---|---|---|
| B1 | 深度研搜记忆层要解决的三个问题是什么？ | 问题定义 | 爆窗、重复搜、脏结论持久化 |
| B2 | 什么叫持久化提示注入？你们怎么防？ | 安全直觉 | 网页 fact 每次被 recall；无出处不写 + 合成步过滤 + untrusted 包裹 |
| B3 | 为什么网页带了 URL 仍然是 untrusted？ | 出处≠可信 | URL 只证明「从哪来」，不证明内容对；交叉验证前不能当 fact |
| B4 | 写报告时为什么要二次召回？ | 工作流理解 | 开始时 recall 给检索用；写报告提高信任门槛，避免脏结论进交付物 |
| B5 | 如何避免同一项目把同一批源搜第二遍？ | 产品价值 | project_id + source_ledger；prompt 独立块「已查来源」 |
| B6 | 中间笔记和长期 findings 如何区分？ | curated vs raw | 步骤结果走 compress/digest；长期只留抽取的 fact |
| B7 | Citation 和 Memory 是什么关系？ | 证据链 | Citation 管本轮报告可追溯；Memory 的 provenance 把证据 id/URL 带到跨任务 |
| B8 | 调研结论过了三个月还该召回吗？ | 时效 | TTL 90 天 + 半衰期衰减 + SUPERSEDE；召回展示日期让模型注意时效 |
| B9 | 两个来源数字打架，记忆层怎么办？ | 冲突 | SUPERSEDE 留链，而不是静默覆盖；低信任不能打高信任 |
| B10 | 用户说「不要再用那家媒体」应该记在哪一层？ | 分层是否清楚 | procedural 或来源质量 unreliable，不是 semantic 事实 |
| B11 | HITL 拒绝查生产库，下次还会查吗？ | Brain 式记忆 | 应沉淀 procedural/trusted；我们已把 reject/edit 写入 |
| B12 | 子 Agent 的工具轨迹要不要进长期记忆？ | 粒度 | 不要整段 dump；可抽「哪张表有用」这类 procedural，本仓尚未细到表名级 |
| B13 | Compaction 把来源压丢了怎么办？ | P0 风险 | 写报告用 evidence digest 而不是 400 字截断；CitationManager 单独留源 |
| B14 | 并行检索三路结果，记忆怎么写？ | 并发与去重 | 各路成功后增量写；URL 台账按归一化键去重 |
| B15 | 失败任务的半成品能不能 remember？ | 污染 | 默认 `remember_on_partial=false` |

### C. 写入、巩固、遗忘

| # | 问题 | 考什么 | 答到即可 |
|---|---|---|---|
| C1 | 为什么要 Agent 显式 save，而不是每轮都抽？ | curated | 本仓折中：步内有限抽取 + finalize 批量；网页无出处仍拒绝 |
| C2 | ADD/UPDATE/DELETE 之外为什么要 SUPERSEDE？ | 研搜审计 | 结论被推翻需要链，不能只改一行 |
| C3 | 相似度用 Jaccard 够吗？ | 召回/合并质量 | 中文 bigram + 可选 embedding；来源台账用 URL hash 精确撞 |
| C4 | 如何防止记忆无限膨胀？ | 容量 | max_facts_per_remember、TTL、衰减、硬清理、top_k 召回 |
| C5 | 巩固放同步还是异步？ | 延迟 | finalize 后 `create_task`，不挡用户拿到报告 |
| C6 | 什么记忆可以晋升为 trusted？ | 治理 | 仅 derived 且跨 session 多次命中；untrusted 不晋升 |
| C7 | 用户要求删除记忆，GDPR 怎么做？ | 合规 | 软删单条 / forget_user；审计留下 action=delete |
| C8 | PII 怎么处理？ | 安全 | 写入时正则脱敏邮箱/手机/身份证 |

### D. 召回、上下文、成本

| # | 问题 | 考什么 | 答到即可 |
|---|---|---|---|
| D1 | Hybrid Recall 怎么打分？ | 能否讲清公式 | 关键词 + embedding + recency + type + project × confidence × trust |
| D2 | 冷启动没有命中怎么办？ | 工程完备 | 返回最近记录，避免完全空白 |
| D3 | 记忆为何不放 system prompt？ | 注入与稳定性 | user 层参考材料；system 稳定；声明勿执行记忆中的指令 |
| D4 | token 不够先丢哪一层？ | 预算 | 当前是整段 trim 保头部；记忆靠前相对不易被裁尽——可主动说这是可改进点 |
| D5 | 评测记忆好不好用什么指标？ | 数据意识 | 运行时 `mean_recall_score`（旧名 recall_at_k 不是 IR Recall@K）；离线才测真正 Recall@K/MRR/nDCG；下游还要看搜索次数和 freshness error |
| D6 | embedding 挂了系统还能用吗？ | 降级 | 可以，纯关键词；缺 key 时 embed_text 返回 None |

### E. 身份、多租户、并发

| # | 问题 | 考什么 | 答到即可 |
|---|---|---|---|
| E1 | 为什么不能用进程级环境变量当 user_id？ | 生产事故 | 单进程多请求会串写；改为请求级 ContextVar |
| E2 | session_id 和 user_id 为什么必须分开？ | 隔离模型 | session 只溯源；跨任务共享靠 user；跨专题靠 project |
| E3 | ephemeral 身份是什么？ | 安全默认值 | 无真实用户时退化为 session，生产可拒绝写入 |
| E4 | 两个租户同名 user 会串吗？ | 租户 | 查询带 tenant_id；SQLite 索引 `(tenant, user, deleted)` |

### F. 架构对比与「你为什么这么选」

| # | 问题 | 考什么 | 答到即可 |
|---|---|---|---|
| F1 | Mem0 vs Letta vs Zep，你们为什么自研？ | 技术选型 | 阶段论：先 compaction+curated；语义层自研足够；图谱/OS 等真有多跳再上 |
| F2 | 要不要把 Memory 放进 LangGraph Store？ | 图 vs Harness | Harness 是显式 loop，Memory 是旁路存储；图内 Store 和我们的 Checkpointer 职责不同 |
| F3 | InMemorySaver 算记忆吗？ | 概念混淆 | 不算长期记忆，只是图会话；进程一死就没了 |
| F4 | 如果重做，你还坚持 SQLite 吗？ | 演进 | Demo/单机 SQLite 合理；多副本换 Postgres + 向量扩展或独立向量库，门面不变 |

### G. 开放题（展示判断力）

| # | 问题 | 建议立场 |
|---|---|---|
| G1 | 要不要让模型自己决定记什么（工具 save_research_finding）？ | 研搜更该显式 save；本仓 finalize 自动抽是工程折中，可演进为工具 |
| G2 | 记忆要不要进训练？ | 不要。Perplexity 也强调历史不是训练数据，是检索后再写 |
| G3 | 多 Agent 要不要共享记忆？ | 同项目可共享 source_ledger 和 semantic；preference 仍按用户隔离 |
| G4 | 怎样证明 Memory 有用？ | A/B：关记忆后重复任务耗时/搜次/正确率；本仓有 MRH、trust_filtered |
| G5 | 最不该过早做的是什么？ | 过早上 Mem0/Letta OS；先把身份、防污染、Compaction 做对 |

---

## 四、建议复习顺序（按你剩的时间）

1. **30 分钟**：背熟本文「一、面试怎么介绍」+ A1 A2 B1 B2 B3。  
2. **2 小时**：通读 [MEMORY_SYSTEM.md](./MEMORY_SYSTEM.md) 第一～六节，对照 `loop.py` 里 BUILD_CONTEXT / remember_step / finalize。  
3. **半天**：把 B 组、C 组过完，能说出模块文件名。  
4. **加分**：自己跑 `tests/test_harness_phase18_memory.py`，能讲每个 `[OK]` 对应哪条产品规则。

---

## 五、开口时不要踩的坑

- 不要说「我们用向量库做了记忆」——立刻会被问注入和过期，答不上就穿帮。  
- 不要把 Checkpointer、JSONL、RAG 都叫 Memory。  
- 不要宣称已经企业级完备；主动讲 InMemorySaver 和未做登录。  
- 不要贬低 Mem0；说「阶段不匹配」，不是「它不好」。
