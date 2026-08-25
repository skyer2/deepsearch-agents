# 上下文工程 Phase 19：七项改进对照

> 本文回答四件事：改了哪些代码、七个问题改前/改后差在哪、决策后得到什么、怎么验证、面试怎么讲。  
> 系统全貌仍见 [CONTEXT_SYSTEM.md](./CONTEXT_SYSTEM.md)。面试题库见 [CONTEXT_INTERVIEW.md](./CONTEXT_INTERVIEW.md)。

---

## 0. 为什么要改（决策背景）

业界做长任务上下文，通常是四刀：

1. **隔离**：子 Agent / 子任务各开窗口，只回传结构化结果。  
2. **清 tool_result**：可再取的工具原文不要带进下一步。  
3. **compaction**：旧对话有损压缩，换预算。  
4. **外置笔记**：压缩可以丢细节，关键事实写在窗口外再 pin 回来。

研搜还要多两刀：**citation（claim↔source）** 和 **写报告时按需展开原文**。

对照本仓旧实现，缺口正好是七个：精选 user message 仍和旧 graph 叠窗口、写报告看不到能核对的原文、压缩丢 URL/数字、超预算保头可能裁掉当前步骤、引用按段落盲贴、没有工作笔记 / tool 清除、观测和 system 与 Harness 不一致。下面按这七项落地。

默认全部开启，可在 `app/config/harness.yml` 关掉回退。

---

## 1. 改了什么（代码清单）

| 文件 | 作用 |
|---|---|
| `app/agent/harness/window_hygiene.py` | 每步独立 LangGraph `thread_id`；过长 `tool_result` 占位清除；按 id 回写 checkpoint |
| `app/agent/harness/working_notes.py` | 抗压缩工作笔记，写入会话目录 `working_notes.md` |
| `app/agent/harness/retention.py` | 从原文抽 URL/数字，压缩后算保留率，不足则打补丁 |
| `app/agent/harness/context_budget.py` | `fit_layers_to_token_budget`：按层淘汰，当前步骤指令最后才动 |
| `app/agent/harness/context_builder.py` | 增加【工作笔记】【可回读证据】层；超预算走分层淘汰 |
| `app/agent/harness/compressor.py` | 按 `step_type` 分压缩模板 + 保留检查 |
| `app/agent/harness/citations.py` | 工人 fact↔source 绑定；写报告 lookup；数字句 CCR；切句时 `[n]` 并回上一句 |
| `app/agent/harness/loop.py` | 逐步新 thread；压缩后绑定证据；刷新笔记；HITL resume 前 hygiene；观测字段 |
| `app/agent/harness/state.py` / `observability.py` | `fresh_threads`、`entity_retention`、`retention_patches`、`tool_results_cleared` |
| `app/agent/main_agent.py` | 压缩器接入保留参数；system 追加 Harness 约束 |
| `app/config/harness.yml` / `loader.py` / `app/api/health.py` | 开关与健康检查暴露 |
| `tests/test_harness_phase19_context.py` | 无需 LLM 的回归 |
| `.github/workflows/eval-regression.yml` | CI 跑 Phase 19 |

---

## 2. 七个问题：改前 / 改后 / 决策好处

### 问题 1 — 精选 prior 和旧 tool 原文叠在同一窗口

| | |
|---|---|
| **改前** | 主 Agent 全程共用 `session_id` 作为 LangGraph `thread_id`。Harness 每步重建瘦 user message，但 checkpointer 里前几步的 messages / tool_result 还在，等于付两份上下文。并行步已经用 `session:parallel:i`，串行步没有。 |
| **改后** | 默认 `fresh_thread_per_step=true`。串行步 `{session}:step:{i}`，并行步 `{session}:parallel:{i}`。下一步从干净窗口开始，只靠 user message 里的 digest / 笔记 / 记忆。同一步 HITL resume 仍用该步 thread，interrupt 可恢复。 |
| **好处** | 对齐 Anthropic「清掉可再取的工具结果 / compaction 后新开窗口」。长任务费用和 context rot 下降。观测字段 `fresh_threads` / `graph_thread_ids` 可证明。 |
| **开关** | `context.fresh_thread_per_step` |
| **实现要点** | `AgentHarness._graph_thread_id` → `build_run_config(graph_thread, …)`。HITL 不得换 thread，否则 interrupt 丢。 |

### 问题 2 — 写报告看不到能核对的原文

| | |
|---|---|
| **改前** | 合成步只有 evidence digest（依赖工人 JSON）或 400 字截断。压缩丢数字后模型只能编。`evidence.json` 要到 finalize 才写。 |
| **改后** | 每步登记更长 excerpt（800 字），工人 `facts/sources` 绑成 `bound_fact`。合成步注入【可回读证据】`[n] + locator + 摘录`。每步成功即写 `evidence.json` / `working_notes.md`（不必等到 finalize）。路径指令允许读这两个文件核对。检索步不注入 lookup，避免窗口膨胀。 |
| **好处** | digest 当目录，原文按需在上下文里，对齐「渐进式读取」。减少终稿幻觉数字。 |
| **开关** | `context.evidence_lookup_enabled` |
| **实现要点** | `CitationManager.build_lookup_block` + `ContextBuilder.build_evidence_lookup_context`（仅 `SYNTHESIS_STEP_TYPES`）。 |

### 问题 3 — 压缩切错对象、固定模板、摘要丢锚点

| | |
|---|---|
| **改前** | 所有 step 同一压缩 prompt；超 2000 字 LLM 摘要，失败截断。不管 URL/数字还在不在。主线程多轮 tool 也不归 compressor 管。 |
| **改后** | ① 搜网 / SQL / KB / 文件分模板，强调保留 URL、表名、数字。② 压缩后算 URL/数字保留率，低于 `0.8` / `0.5` 则前置【压缩保留补丁】。③ 问题 1 的新 thread 让「步内 tool 原文」不再带入下一步。 |
| **好处** | 压缩可以有损，但研搜最贵的锚点（出处和数字）有硬检查，而不是只靠 prompt「请保留」。 |
| **开关** | `compression.retention_check`、`retention_min_url`、`retention_min_number` |
| **实现要点** | `COMPRESS_PROMPT_BY_STEP` + `apply_retention_patch`。观测：`entity_retention_avg`、`retention_patches`。 |

### 问题 4 — 超预算整段保头，可能裁掉当前步骤

| | |
|---|---|
| **改前** | `trim_text_to_token_budget` 对整段 user message `[:max_chars]`。记忆靠前安全，当前步骤、工人 JSON、恢复提示在后部可能被切掉。模型不知道这一步要干什么。 |
| **改后** | `fit_layers_to_token_budget`：先 trim/drop `tools → resources → path → prior → memory → evidence → task_query`；`step/binding/worker_json/recovery/notes` 尽量 pin；实在超了才最后动 `step`。**只要还剩 step 层，禁止整段保头截尾。** |
| **好处** | 窗口不够时模型至少还知道「这一步要干什么」，避免裁掉指令却留下一堆旧记忆。 |
| **开关** | `context.layer_priority_eviction` |
| **实现要点** | `LAYER_SHRINK_ORDER` / `LAYER_PINNED`。回退路径：开关关掉时仍走旧的整段保头。 |

### 问题 5 — Citation 按段落序号盲贴 [n]，CCR 定义偏弱

| | |
|---|---|
| **改前** | regex 抽 URL；段落 i 贴 source i；CCR = 文中出现的 `[n]` 个数 / 注册源数；无引用句子都算 hallucination（标题也被算）。句号后空格再 `[1]` 会被切句拆开，数字句覆盖率变成 0。 |
| **改后** | `bind_worker_facts`：fact 与 locator 成对。正文只在命中 bound fact 时补 `[n]`，插在句号前。CCR 优先统计**含数字的句子**里带引用的比例（`numeric_citation_coverage`）。切句后若下一片只是 `[n]`，并回上一句。 |
| **好处** | 引用表示「这条结论对上了哪条证据」，不是「参考文献里有编号就算覆盖」。更接近 OpenAI 来源侧栏的 claim↔source。 |
| **开关** | 随 `citations.enabled` |
| **实现要点** | `inject_inline_citation_hints` + `_split_report_sentences` + `compute_metrics`。 |

### 问题 6 — 没有抗压缩笔记，也没有 tool_result 清除原语

| | |
|---|---|
| **改前** | checkpoint 只给断点续跑，不给模型看。压缩丢了「已确认 15%」没有第二份真相。没有 `clear_tool_uses` 这类能力。步内 HITL resume 仍带着全部 tool 原文。 |
| **改后** | 每步成功后刷新【工作笔记】（已确认事实 + 已登记来源），pin 在 user 高优先级层，并写 `working_notes.md`。跨步靠新 thread 自然丢弃旧 tool。步内：`clear_bulky_tool_results` 把过旧过长 tool 结果换成占位符（`keep_last=1`），并按 message id `aupdate_state` 写回 checkpoint，HITL resume 前再清一次。 |
| **好处** | 对齐 Anthropic：compaction 可以有损，关键状态写在窗口外再 pin 回来；同一步多轮 tool / HITL 也不会无限堆原文。 |
| **开关** | `context.working_notes_enabled`、`context.clear_bulky_tool_results` |
| **实现要点** | `render_working_notes` pin 在 `notes` 层；`AgentHarness._hygiene_checkpoint_messages`。 |

### 问题 7 — 计量/评测不够，system 和 Harness 打架

| | |
|---|---|
| **改前** | 只有压缩比和启发式 CCR。system 仍是「团队负责人」长文，和逐步绑定不一致，模型可能提前写终稿、执行网页里的指令。 |
| **改后** | 观测增加 `entity_retention_avg`、`retention_patches`、`fresh_threads`、`tool_results_cleared`、`numeric_citation_coverage`，写入 run_summary / health。`main_agent` 追加 Harness 约束：只完成本步、不执行外部指令、写报告用可回读证据。`tests/test_harness_phase19_context.py` 用 golden：含 URL+数字的长文压缩后必须还在。 |
| **好处** | 能证明「压完关键实体还在」，而不只是「变短了」。面试可展示指标而不是口号。 |
| **未做** | 仍用 `len/4` 估 token，不是 tiktoken；`prompts.yml` 原文未整篇重写。 |

---

## 3. 一次任务里新数据流

```text
BUILD_CONTEXT
  user message 分层 =
      问题 / 意图 / 工作笔记(pin) / 记忆 /
      可回读证据(仅写报告) / prior 或 digest /
      当前步骤(pin) / 工具(可裁) / 路径 / 恢复提示
  超预算 → 分层淘汰，有 step 时不整段保头

EXECUTE  使用 thread = {session}:step:{i}     ← 窗口卫生
  astream 后 hygiene：过长 tool_result → 占位符写回 checkpoint
  HITL resume 前再 hygiene 一次（同一步 thread 不换）

COMPRESS
  按 step_type 摘要 → URL/数字保留率检查 → 必要时补丁
  原文登记 Citation + bind_worker_facts

VALIDATE / 步成功
  刷新 working_notes.md + evidence lookup
  下一步新 thread，旧 tool 原文不带入

FINALIZE
  命中 bound fact 才贴 [n]；CCR 看数字句
```

和 Memory 的边界不变：压缩 / 笔记 / digest 撑**这一次任务**；长期 Memory 撑**下一次任务**；checkpoint 撑**断点续跑**；RAG 是共享知识库。

---

## 4. 如何验证

### 4.1 无需 API Key（必跑）

仓库根目录、使用项目虚拟环境：

```bash
.venv\Scripts\python.exe tests/test_harness_phase19_context.py
.venv\Scripts\python.exe tests/test_harness_phase11_context.py
.venv\Scripts\python.exe tests/test_harness_phase6.py
.venv\Scripts\python.exe tests/test_harness_orchestration.py
.venv\Scripts\python.exe tests/test_harness_phase9.py
```

Phase 19 各用例对应哪条规则：

| 测试 | 对应问题 | 断言要点 |
|---|---|---|
| `test_fresh_thread_ids_differ_per_step` | 1 窗口卫生 | `sess:step:0` ≠ `sess:step:1` |
| `test_clear_bulky_tool_results` | 6 tool 清除 | 旧长 tool 变占位符，最近一条保留 |
| `test_hygiene_writes_placeholder_back_to_graph` | 6 HITL 路径 | dummy `aupdate_state` 收到占位符且带原 id |
| `test_retention_patch_keeps_url_and_number` | 3 保留检查 | 摘要丢锚点后补丁含 URL 和数字 |
| `test_compressor_retention_without_llm` | 3 截断后仍补 | 无 LLM 走 truncate+patch |
| `test_layer_priority_keeps_current_step` | 4 分层预算 | 超预算仍含 `KEEP_STEP_INSTRUCTION` |
| `test_working_notes_and_file` | 6 笔记 | 笔记含事实/URL，并写出文件 |
| `test_refresh_writes_evidence_json` | 2 写报告回读 | 步成功即写 `evidence.json`，lookup 进 state |
| `test_fact_source_binding_and_lookup` | 5 绑定 + 数字句 CCR | 命中 fact 才有 `[1]`；句号后 `[1]` 仍计入覆盖 |
| `test_synthesis_injects_evidence_lookup` | 2 写报告回读 | 合成步 user message 含证据块和笔记 |
| `test_retrieval_skips_evidence_lookup` | 2 窗口控制 | 检索步不注入 lookup |
| `test_config_phase19` | 开关默认开 | 含 `clear_bulky_tool_results` |

### 4.2 Live（有 LLM）时额外看

- 会话目录出现 `working_notes.md`、`evidence.json`
- JSONL `context_built.graph_thread_id` 形如 `*:step:0`、`*:step:1` 且不相同
- `run_summary` 含 `fresh_threads`、`entity_retention_avg`、`retention_patches`、`tool_results_cleared`、`numeric_citation_coverage`
- 写报告步 prompt 含【可回读证据】【工作笔记】
- `/health` 的 `context` 段暴露上述开关

### 4.3 关掉对比（确认可回退）

```yaml
context:
  fresh_thread_per_step: false
  layer_priority_eviction: false
  working_notes_enabled: false
  evidence_lookup_enabled: false
  clear_bulky_tool_results: false
compression:
  retention_check: false
```

预期：thread 退回 `session_id`；超预算再走整段保头；压缩不再打补丁。单测 `test_config_phase19` 会失败（它断言默认开），这是开关生效的信号，不是回归。

### 4.4 手工对照实验（面试可讲）

同一长任务开/关 `fresh_thread_per_step`：

- 开：第 2 步 `context_built` 的 thread 带 `:step:1`，user message 长度接近「本步材料」，不随 tool 轮次线性涨。  
- 关：第 2 步仍用同一 `thread_id`，checkpointer messages 含第 1 步 tool 原文。

同一超预算构造（巨大 tools 层 + 短 step 指令）：

- 开分层淘汰：输出含当前步骤目标。  
- 关：可能只剩任务开头，step 指令消失。

---

## 5. 面试怎么介绍

### 5.1 开场 20 秒（先定边界）

> 研搜的上下文工程，要解决的是单次任务会爆窗、压缩又容易把来源压没。我们没有把整段对话 dump 进 Memory，也没有靠超大窗口硬扛。做法是：子 Agent 各自窗口只回传结构化结果；Harness 每步显式压缩；下一步按「这一步需要什么」分层拼 user message；写报告那一步用 digest + 可回读证据，而不是 400 字截断。

### 5.2 和 Memory 拆开 15 秒（必说）

> 压缩和工作笔记撑的是这一次任务。长期 Memory 撑的是下一次任务。checkpoint 是断点续跑。RAG 是共享知识库。这四样不要叫同一个 Memory。

### 5.3 七项改进 60～90 秒（对着白板）

> 我们对照业界做了一次上下文评审，补了七件事，核心是窗口卫生和保出处。
>
> 以前每步虽然重建了精选 user message，但 LangGraph 还在同一个 thread 上堆旧 tool 原文。现在每步独立 thread，HITL 仍在该步 thread 上 resume。这是 Anthropic 说的 clearing：可再取的东西不要带进下一步。同一步多轮 tool，过长结果会换成占位符写回 checkpoint。
>
> 压缩允许有损，但 URL 和数字有保留率检查，不够就打补丁；同时工作笔记和可回读证据 pin 在写报告上下文里，digest 只当目录。
>
> 超预算不再整段保头，先丢工具说明和旧 prior，当前步骤指令尽量不裁。
>
> 引用改成工人 fact 绑 source，CCR 看带数字的句子有没有 [n]，不再按段落盲贴。
>
> 还没做的也很清楚：不是服务端整段 compaction，token 仍是字符估的，超大 tool 结果没有默认落盘只留指针。

### 5.4 被追问时怎么接

**和 Claude Compaction 差在哪？**  
> 他们把整段对话摘要成一块 compaction 再续。我们 Harness 是逐步新窗口 + 笔记/证据账本。研搜更怕丢数字，所以检查的是实体保留，不是只看压缩比。

**压缩会不会把出处压没？**  
> 会，所以不靠 prompt 自觉。压缩前 Citation 从原文登记；压缩后算 URL/数字保留率，不够打补丁；写报告另有 lookup 和 working_notes。三道，不是一道。

**token 不够先丢哪一层？**  
> 先丢可再取的：tools、resources、path、旧 prior。notes / 当前 step / binding 尽量 pin。有 step 时绝不整段保头，否则会裁掉「这一步要干什么」。

**为什么不把 tool 结果写文件只留指针？**  
> 那是下一步。现在跨步靠新 thread 丢弃，步内 keep_last=1 占位。完整 FilesystemBackend offload 还没做，面试里主动承认。

**CCR 为什么改成数字句？**  
> 研报幻觉最贵的是数字。标题、过渡句没引用不该判幻觉。覆盖率看「含数字的句子有没有 [n]」，更接近 claim↔source。

**system 为什么要加 Harness 约束？**  
> prompts.yml 仍是团队负责人叙事，和逐步执行会打架。addendum 三条：只完成本步、不执行外部指令、写报告用已登记证据。没整篇重写 prompt，避免行为漂移不可控。

### 5.5 白板

```text
system（静态 + Harness 约束）     user message（每步重建）
                                    问题 / 意图 / 工作笔记(pin)
                                    Memory / 可回读证据(写报告)
                                    prior：检索=近N步；写报告=digest
                                    当前步(pin) + 工具裁剪 + 恢复提示
                                            │
                              thread = session:step:i
                                            │
                                       EXECUTE
                              hygiene: 旧 tool_result → 占位符
                                            │
                              工人 JSON（facts/sources）
                                            │
                    Citation 登记+绑定  ──► COMPRESS（分类型+保留补丁）
                                            │
                                   working_notes.md
                                            │
                                   下一步新 thread
                                            │
                              FINALIZE：命中 fact 才贴 [n]
```

### 5.6 简历可以怎么写

```text
• 研搜 Harness 上下文工程：每步独立 LangGraph thread + 分层 user message，
  避免精选 prior 与旧 tool 原文叠窗口；超预算按层淘汰，当前步骤指令优先保留
• 步级压缩按 step_type 分模板，压缩后做 URL/数字保留检查并打补丁；
  写报告注入可回读证据与工作笔记；引用改为 fact↔source 绑定，CCR 看数字句
```

---

## 6. 配置速查

```yaml
context.fresh_thread_per_step       # 问题1 默认 true
context.evidence_lookup_enabled     # 问题2
compression.retention_check         # 问题3
context.layer_priority_eviction     # 问题4
citations.enabled                   # 问题5
context.working_notes_enabled       # 问题6 笔记
context.clear_bulky_tool_results    # 问题6 HITL 清除
# 问题7：观测字段自动写入 run_summary；system addendum 在 main_agent.py
```

环境变量（可选覆盖 yaml）：

| 变量 | 对应 |
|---|---|
| `HARNESS_CONTEXT_FRESH_THREAD` | 每步新 thread |
| `HARNESS_CONTEXT_LAYER_PRIORITY` | 分层淘汰 |
| `HARNESS_CONTEXT_WORKING_NOTES` | 工作笔记 |
| `HARNESS_CONTEXT_EVIDENCE_LOOKUP` | 写报告回读 |
| `HARNESS_CONTEXT_CLEAR_TOOL_RESULTS` | tool 占位清除 |
| `HARNESS_COMPRESSION_RETENTION` | 压缩保留检查 |

---

## 7. 仍然没做的（面试主动说）

- Token 仍是 `len/4` 启发式，不是模型真实 tokenizer / API usage。  
- 没有 Anthropic 那种服务端整段 `compaction` block；我们是每步新 thread + 步级摘要。  
- 超大 tool 结果没有默认写文件只留指针；跨步靠新 thread，步内 keep_last=1 占位。  
- digest 仍依赖工人 JSON；散文回传时写报告会退回截断摘要，但可回读证据层会尽量补。  
- `prompts.yml` 原文仍偏「团队负责人」叙事，已在 `main_agent.py` 追加约束，未整篇重写。  
- CCR 仍是启发式字符串匹配，不是 NLI / 事实验证模型。
