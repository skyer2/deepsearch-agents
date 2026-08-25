# 深度研搜上下文工程与压缩：面试讲法与问题清单

配套系统全貌：[CONTEXT_SYSTEM.md](./CONTEXT_SYSTEM.md)  
Phase 19 七项改进对照：[CONTEXT_IMPROVEMENTS.md](./CONTEXT_IMPROVEMENTS.md)  
和 Memory 的边界：[MEMORY_SYSTEM.md](./MEMORY_SYSTEM.md)

本文：**怎么开口**、**简历埋点**、**追问怎么接**、**问题清单**。细节想不起来时，先答「压的是步结果、不是对话；写报告用 digest 保出处」。

---

## 一、面试怎么介绍

### 1. 开场 20 秒（先定边界，别先讲 qwen-turbo）

> 研搜的上下文工程，要解决的是单次任务会爆窗、压缩又容易把来源压没。我们 **没有** 把整段对话 dump 进 Memory，也 **没有** 靠超大窗口硬扛。做法是：子 Agent 各自窗口只回传结构化结果；Harness 每步显式压缩；下一步按「这一步需要什么」分层拼 user message；写报告那一步改用全量 evidence digest，而不是 400 字截断。

### 2. 和 Memory 拆开 15 秒（必说，否则被混问）

> 压缩撑的是这一次任务。长期 Memory 撑的是下一次任务。checkpoint 是断点续跑。RAG 是共享知识库。这四样不要叫同一个 Memory。

### 3. 具体怎么做 60～90 秒（对着白板）

建议手画：**拼装 → 执行 → 压缩 → 回灌**。

> **拼装：** system 来自 prompts.yml，保持稳定。每步重建 user message：问题、意图、记忆、已完成步骤、当前步骤、工具（按 step 裁剪）、路径、恢复提示。记忆和网页都不进 system。
>
> **检索步回灌：** 只看最近 5 步，每步大约 400 字，优先工人 JSON 的 summary/facts/sources，外部内容包 `<untrusted>`。
>
> **写报告步回灌：** 换成多源 evidence digest，按步列事实和 URL，避免压缩把出处弄丢。
>
> **压缩：** 步结果超过约 2000 字才压。先用小模型摘要，要求保留 URL；失败就截断。压缩前 Citation 已经从原文登记了证据，finalize 再生成 `[n]` 和参考文献。
>
> **预算：** 单步 user message 估算超过 1.2 万 token 时按层淘汰：先丢工具说明和旧 prior，当前步骤指令尽量不裁。任务级还有总 token / 工具次数 / 时长上限。

### 4. 收尾 15 秒（主动暴露缺口）

> Token 是字符除以 4，不是真实 tokenizer。我们已按层淘汰预算、压缩后做 URL/数字保留检查、每步独立 thread。还没有服务端整段 compaction。

### 5. 一分钟版

分层拼装 + 检索/写报告两套 prior + 步级压缩（保留检查）+ 每步新 thread。Memory 另说。缺口：启发式 token、不是服务端 compaction。

### 6. 白板

```text
system（静态）     user message（每步重建）
                      问题 / 意图 / Memory
                      prior：检索=近N步截断
                             写报告=evidence digest
                      当前步 + 工具裁剪 + 恢复提示
                              │
                         EXECUTE
                              │
                    工人 JSON（facts/sources）
                              │
              Citation 登记原文 ──► COMPRESS（LLM/截断）
                              │
                         下一步 prior
                              │
                    FINALIZE：[n] + 参考文献
```

---

## 二、简历怎么写（把面试官问到你会的地方）

不要写「做了上下文压缩」。用钩子：

```text
• 研搜 Harness 上下文工程：system 与 user 分离；检索步只回灌近 N 步结构化摘要，
  写报告改用多源 evidence digest，避免 400 字截断丢掉 URL
• 步级显式压缩（小模型摘要，失败截断）+ 压缩前登记 Citation；
  外部检索 <untrusted> 隔离；单步 token 预算与任务级 tool/时长护栏
```

引导出来的问题几乎一定是：

- 压的是对话还是步结果？
- 压缩会不会丢来源？
- 有没有分层？
- 和 Memory 什么关系？

这四题标准答见下一节。

口头点菜（自我介绍末尾只点两个）：

> 「上下文这块如果感兴趣，可以展开：检索步和写报告步为什么用两套材料，以及压缩和引用谁先谁后。」

---

## 三、高频追问（先练这 8 个）

**Q. 压缩的是完整对话、summary 还是别的？**  
A. 压的是 **本步工人回传的 content**，不是聊天历史。长期 Memory 另存 fact。完整轨迹在 JSONL/checkpoint。摘要是压缩产物 `compressed_content`，供下一步 prior 用。

**Q. 和 Memory 什么关系？**  
A. 压缩管这一次别爆窗；Memory 管下一次还能用。BUILD_CONTEXT 召回的记忆会进 user message 的 Memory 层，但那是注入，不是压缩。

**Q. 压缩会不会把出处压没？**  
A. 会，所以三道补丁：压缩前 `CitationManager` 从原文抽 URL 并 bind fact；压缩后算 URL/数字保留率，不够打补丁；写报告用 digest + 可回读证据，不用截断摘要。CCR 低会 recover。

**Q. 为什么不把所有历史都塞进 system？**  
A. system 要稳定。网页和记忆进 user，并声明勿执行其中指令。工具描述按 step 裁剪，减少越权调用。

**Q. token 不够先丢哪一层？**  
A. `fit_layers_to_token_budget`：先丢 tools / path / 旧 prior，notes 和当前 step 尽量 pin。有 step 时不再整段保头。这是 Phase 19 已落地的，不是缺口。

**Q. 并行三路检索，上下文怎么拼？**  
A. 子 Agent 各开窗口、互不通信。Join 后各路 JSON 进 digest，写报告看汇总，而不是三份网页原文并排。

**Q. 为什么还要 LLM 压缩？截断不行吗？**  
A. 截断保开头丢结尾，研报关键数字经常在后半。LLM 按 step_type 要求保留事实和 URL。失败才截断，再跑保留检查。短于阈值不调 LLM。

**Q. 这不就是 RAG 吗？**  
A. RAG 检索外部文档。上下文工程管「模型这一步看见什么、看不见什么」。RAG 结果只是本步材料之一，进压缩和 digest。

细节想不起来时的三步：先问清「您说的是步压缩还是长期记忆」→ 答存什么/何时压/写报告怎么保源 → 「tokenizer 和分层淘汰的数字我回去对 `context_budget.py`」。

---

## 四、问题清单（按主题准备）

### A. 几乎必问：概念与边界

| # | 问题 | 考什么 | 答到即可 |
|---|---|---|---|
| A1 | 什么是上下文工程？和 prompt 调教有何不同？ | 是否只会写 system | 管模型每步看见什么：分层、预算、隔离、压缩、工具裁剪 |
| A2 | 压缩和 Memory 什么关系？ | 会不会混 | 压缩=本任务工作记忆；Memory=跨任务 fact |
| A3 | 存的是对话、summary 还是 fact？ | 上次真实面试题 | 长期 fact；步内 compressed summary；对话在 trace |
| A4 | 为什么上下文不是越长越好？ | Anthropic context pollution | 注意力稀释、费用、脏网页；所以 prior 限 N 步 |
| A5 | system 放什么、user 放什么？ | 注入与稳定性 | system 角色/流程；user 任务材料；记忆不进 system |
| A6 | Compaction 和 Summarization 有何不同？ | 业界词 | Claude 是替换旧消息；我们是步结果压缩后回灌 |

### B. 研搜场景特有（本岗位最该准备）

| # | 问题 | 考什么 | 答到即可 |
|---|---|---|---|
| B1 | 研搜上下文要解决的两个 P0？ | 问题定义 | 爆窗；压缩丢出处 |
| B2 | 检索步和写报告步上下文为何不同？ | 工作流 | 检索要轻、看近况；写报告要全证据 digest |
| B3 | 中间 8 次搜索，第 9 步写报告看见什么？ | 具体 | digest：每步 facts/sources，不是 8 篇原文 |
| B4 | 压缩把 URL 压丢了怎么办？ | Citation | 压缩前登记 + 保留补丁；写报告 lookup；CCR 校验 |
| B5 | 网页里有「忽略以上指令」怎么办？ | 注入 | `<untrusted>`；记忆层另有信任门 |
| B6 | 子 Agent 的工具轨迹要不要进主上下文？ | 隔离 | 不要；只回传 JSON/摘要 |
| B7 | 并行三路会不会污染窗口？ | Manus 同款 | 互不通信，主控 join + digest |
| B8 | evidence digest 和 compression 谁替代谁？ | 两层 | 压缩服务下一步干活；digest 服务合成，不互相替代 |
| B9 | 引用 [n] 对不齐原文怎么办？ | 诚实 | fact↔source 绑定后按命中贴 [n]；数字句 CCR；仍是启发式不是 NLI |
| B10 | 为什么工具列表也要裁剪？ | 上下文+行为 | 占 token，且诱导写报告时还去搜网 |
| B11 | 文件生成步为什么还要路径指令？ | 研搜交付 | 必须写到 session 目录，避免文件散落、下一步读不到 |
| B12 | 失败重试时上下文多了什么？ | recover | recovery_hints 进 user；structured_retry 要求纯 JSON |

### C. 压缩实现

| # | 问题 | 考什么 | 答到即可 |
|---|---|---|---|
| C1 | 什么阈值触发压缩？ | 数字 | 默认 2000 字符；短的不压 |
| C2 | LLM 失败怎么办？ | 降级 | truncate 到 max_chars，带「已截断」标记 |
| C3 | 压缩模型为什么用小模型？ | 成本 | 主模型推理，压缩用 qwen-turbo |
| C4 | 压缩 prompt 保什么？ | 研搜 | 事实、数据、URL/表名/`[source:src-N]` |
| C5 | 输入太长压缩模型自己会爆吗？ | 工程 | 先截 12000 字符再送压缩模型 |
| C6 | 压缩比怎么评测？ | 指标 | `compression_ratios` 平均；还有估算 tokens saved |
| C7 | 能不能只做截断？ | 取舍 | 截断丢尾部，研报数字常在后半；LLM 优先 |

### D. 预算、观测、失败

| # | 问题 | 考什么 | 答到即可 |
|---|---|---|---|
| D1 | token 怎么估？ | 诚实 | `len/4`，不是 tiktoken |
| D2 | 单步上限多少？超了怎么办？ | 数字 | 默认 12000；按层淘汰，当前步骤最后才裁 |
| D3 | 分层淘汰你会怎么改？ | 已落地 | 优先级：当前步 / notes > 意图 > digest/记忆 > prior > 工具说明 |
| D4 | 任务级还有哪些刹车？ | 护栏 | max_total_tokens / tool_calls / run_sec / plan_steps |
| D5 | 如何证明压缩有用？ | 数据 | 平均压缩比、entity_retention、budget_trims、CCR、fresh_threads |

### E. 架构对比

| # | 问题 | 考什么 | 答到即可 |
|---|---|---|---|
| E1 | 为什么不用百万级窗口硬扛？ | Gemini 对照 | 贵、慢、脏上下文；研搜更要可控材料 |
| E2 | 和 Claude Compaction 差在哪？ | 业界 | 他们摘要旧对话；我们按 Harness 步压缩结果 |
| E3 | 要不要把 tool 结果写文件只留指针？ | Manus/DeepAgents | 跨步新 thread 丢弃；步内 keep_last=1 占位；完整 offload 未做 |
| E4 | 监督者-工人和上下文什么关系？ | 编排 | 工人 JSON 是压缩和 digest 的上游契约 |

### F. 开放题

| # | 问题 | 建议立场 |
|---|---|---|
| F1 | 写报告要不要把原文文件路径塞回 context？ | 已做 lookup + working_notes；完整 offload 仍可演进 |
| F2 | 压缩要不要按 step_type 用不同 prompt？ | 已做搜网保 URL、SQL 保表名；仍可再细 |
| F3 | 记忆和 prior 抢预算时谁优先？ | 写报告：digest/证据 > 记忆；检索：当前步 > 近况 > 旧记忆 |
| F4 | 最不该过早做的是什么？ | 上知识图谱当上下文；先把丢出处和爆窗做对 |

---

## 五、建议复习顺序

1. **20 分钟**：背熟本文「一、面试怎么介绍」+ A2 A3 B2 B4。  
2. **1 小时**：通读 [CONTEXT_SYSTEM.md](./CONTEXT_SYSTEM.md) 第一～五节，对照 `build_step_message` 和 `_phase_compress_step`。  
3. **加分**：跑 `tests/test_harness_phase19_context.py`，能讲每步新 thread、分层淘汰保住当前步骤、压缩后 URL/数字还在。细节对照 [CONTEXT_IMPROVEMENTS.md](./CONTEXT_IMPROVEMENTS.md)。

---

## 六、开口不要踩的坑

- 不要说「我们把历史都压缩进 Memory」。  
- 不要把 Checkpoint、JSONL、RAG 叫成压缩。  
- 不要再说「还在整段保头截断」；分层淘汰已经落地，关掉开关才会走旧路径。  
- 不要背设计文档 §6.2 的 SummarizationMiddleware，那不是现行主路径。  
- 被问细节空了：先分层，再承认数字要对代码，不要编 Jaccard 那种记错的阈值。
