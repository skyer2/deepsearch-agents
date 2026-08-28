# 深度研搜 MCP / Tool Capability Plane

> 对照 `app/mcp/`、`app/tools/db_core.py`、`app/agent/harness/worker_profiles.py`、`tool_contract.py`、`loop.py`。
> Harness 总架构见 [HARNESS_ARCHITECTURE.md](./HARNESS_ARCHITECTURE.md)。
> 上下文外置见 [CONTEXT_SYSTEM.md](./CONTEXT_SYSTEM.md)。

---

## 1. 一句话定位

MCP **不是**「Agent 调工具的一种新 API」，而是 **Agent Runtime 与外部 Capability Provider 之间的标准协议边界**。

Harness 仍然负责权限、预算、调度、安全、上下文和恢复。MCP 只标准化 capability 的发现与调用。

```text
Research Worker
      │ tool intent
      ▼
Worker Profile（最小、稳定的 model-visible surface）
      ▼
PolicyEngine + ToolGateway（fail-closed）
      ▼
MCP Gateway（identity / retry / breaker / audit）
      ▼
stdio pool 或 stateless HTTP
      ▼
Tavily / MySQL / RAGFlow / Files MCP
      ▼
Result Normalizer + Tool Output Contract
      ▼
短卡进 LLM；原文进 Artifact Store
```

默认 `mcp.enabled: false`：上层 Worker 走 LangChain 直连。打开 MCP 后按 **Server 粒度**替换为 MCP provider。工人不需要知道底层是 `langchain-tool` 还是 `mcp-pool`。

> **MCP 是 pluggable transport/provider boundary，不污染 Research Domain Model。**

---

## 2. 和 Function Calling 的边界

| | Function Calling | MCP |
|--|------------------|-----|
| 层级 | 模型 API | Client/Server capability 协议 |
| 谁消费 | 当前这一个 Python Agent | 可被不同 Runtime / 模型 / 语言客户端共享 |
| 本仓做法 | 本地/单进程足够时保留 `@tool` | 外部、可复用、可独立部署的 capability 走 MCP |

本仓 **没有强制全部 MCP 化**。Registry 隔离 domain layer；底层可以是 local tool 或 MCP provider。LangChain 路径和 MCP 路径共用同一份 `db_core` + `ToolGateway`，MCP 不能绕过安全检查。

---

## 3. Tools / Resources / Prompts

MCP 官方把三类 primitive 分开：

| Primitive | 谁主导 | 深度研搜里做什么 |
|-----------|--------|------------------|
| Tool | Model-controlled | 搜索、SQL、API、写文件、触发长任务 |
| Resource | Application-controlled | 会话文件、Artifact、Evidence、Schema |
| Prompt | User-controlled | 模板 / 工作流入口（本仓未作为主路径） |

`files-mcp` 既有 `read_file_content` / `generate_markdown` / `convert_md_to_pdf_async` 这些 Tool，也有 `session://{session_id}/{filename}` Resource。读 PDF 不应一次把 50KB 塞进 tool result，而应走 Resource / Artifact ref，再 JIT 读相关段落。

---

## 4. 现行控制面（按调用顺序）

### 4.1 Worker Profile — 最小 tool surface

文件：`app/agent/harness/worker_profiles.py`

稳定 Profile：`web_researcher` / `db_researcher` / `kb_researcher` / `file_researcher` / `mixed_researcher` / `synthesis_editor`。

Tesla 公开研究只看到 `internet_search` + `read_artifact` + `read_evidence`，看不到 SQL / RAG / PDF。schema 稳定也更利于 prefix / KV cache。

### 4.2 Registry — Server 只 describe，Host 决定 policy

文件：`app/mcp/registry.py`（工具 catalog）、`registry_sync.py`（启动 `list_tools`）、`server_registry.py`（Trusted Server 白名单）

启动时可 `list_tools()` 同步 description / inputSchema。但 `step_types`、`permissions`、server ownership 由 Harness 的 `TOOL_STEP_POLICY` 管。未批准的 module / URL 在 `server_registry.py` 就被拒绝，不会自动连上。

> Tool description / annotation 来自 Server，不能当可信安全声明。

未知第三方 tool 可以编进 catalog，但 `step_types=[]`，Gateway fail-closed，不会自动暴露给模型。

### 4.3 PolicyContext — 不再只认 step_type

文件：`app/mcp/policy_context.py`、`app/mcp/auth.py`

`AgentHarness` 在 run 启动时签发 MCP access token，绑定：

```text
tenant_id / user_id / project_id / session_id
run_id / task_id
granted_scopes / allowed_tools
access_token
```

授权交集：

```text
User Scopes ∩ Task Policy ∩ Tool Permissions ∩ Step Policy ∩ Resource ACL
```

本地默认 `mcp.require_auth: false`。生产打开后必须带有效 token（issuer / audience / expiry / tenant）。**禁止**用进程自己的 env 和自己比对——那不是 caller identity。

MCP token **不得** passthrough 给下游 MySQL / Tavily。下游凭证由各 Server 自己的 env allowlist 注入。

### 4.4 ToolGateway — 双路径同一 choke point

文件：`app/mcp/tool_gateway.py`

```text
fail_closed=true
sql_select_only=true
enforce_step_policy=true
```

```text
LangChain mode  ──┐
                  ├→ db_core + ToolGateway
MCP mode       ──┘
```

### 4.5 MCP Gateway — 治理而不是转发

文件：`app/mcp/mcp_gateway.py`

- Trusted Server Registry（`server_registry.py`）：未批准的 module / URL 拒绝
- 按副作用分类重试：只读可 backoff；`create_ask_delete` 等非幂等禁止盲重试
- per-server circuit breaker
- 租户维度内存限流（进程级；多实例需外置 Redis）
- 耐久 SQLite audit（`mcp_data/audit.db`），不再是内存 500 条 deque

### 4.6 Transport

| 环境 | 实现 |
|------|------|
| local / trusted | stdio Session Pool，`pool_size=3` round-robin，crash 后重建，queue backpressure |
| production | `transport: streamable-http`，JSON-RPC `POST {endpoint}/mcp`，请求自包含 |

stdio 子进程 **不再** `os.environ.copy()`。每个 server 只拿自己的 credentials（`app/mcp/server_env.py`）。

### 4.7 Tasks

PDF 长任务走 durable SQLite（`HARNESS_MCP_TASK_STORE`），stdio 子进程与 Agent 主进程共享同一文件库。`files-mcp` 暴露 `tasks_get` / `tasks_cancel`。这是协议风格的 task handle，不是两边各持一份内存 dict。

### 4.8 Result Normalizer + Tool Output Contract

- `result_normalizer.py`：消费 `structuredContent`、多 content block、resource link
- `schema_adapter.py`：MCP JSON Schema → LangChain StructuredTool
- `tool_contract.py`：工具先把原文写入 Artifact Store，模型只看到 snippet + `artifact_id`

Tool Output Contract 管的是 **模型可见体积**。数据库资源耗尽要在数据源层截断（见下一节）。

---

## 5. DB 生产护栏

文件：`app/mcp/sql_guard.py`、`app/tools/db_core.py`

SELECT-only **不等于** 数据访问安全。现行护栏：

| 层 | 做什么 |
|----|--------|
| 语法 | 只允许 SELECT / WITH；禁多语句、禁 DDL/DML 关键字 |
| 账号 | 优先 `MYSQL_READ_HOST` 读副本 |
| 表白名单 | `tools.sql_table_allowlist`（空 = 开发态全表） |
| 行 / 字节 | 服务端封顶 LIMIT + `fetchmany` + `sql_max_bytes` |
| 时间 | `SET SESSION MAX_EXECUTION_TIME` |

`get_table_data` 带 LIMIT；`execute_sql_query` 不再 `fetchall()` 无上限。

---

## 6. Resources 与 ACL

- 会话文件：`session://{session_id}/{filename}`，Gateway / client 校验当前 principal 的 `session_id`
- Opaque ref：`res_*`，按 tenant / user 解析
- 路径穿越仍在 files-mcp 内拦截；多租户时 **知道 session id 不等于有权读**

---

## 7. 配置与开关

`app/config/harness.yml`：

```yaml
mcp:
  enabled: false                 # true = 四个 Server 都走 Gateway
  tavily_enabled / mysql_enabled / ragflow_enabled / files_enabled
  transport: stdio               # 或 streamable-http
  pool_size: 3
  require_auth: false            # 生产打开
  oauth_audience: https://mcp.local/gateway
tools:
  fail_closed: true
  sql_select_only: true
  sql_max_rows: 200
  sql_table_allowlist: []        # 生产必须显式配置
```

环境变量覆盖：`HARNESS_MCP_ENABLED`、`HARNESS_MCP_TAVILY`、`HARNESS_MCP_REQUIRE_AUTH`、`HARNESS_MCP_TASK_STORE` 等。

---

## 8. 面试怎么说

> MCP 标准化的是 capability discovery/invocation，不替 Host 决定谁可以调用什么。tenant ACL、task allowlist、budget、approval、rate-limit、idempotency、audit 都属于 Harness control plane。Server 自己标的 `readOnlyHint` 也不能直接当安全事实。

> 如果工具只给当前 Python Agent 用，FunctionTool 足够。引入 MCP 的价值出现在 capability 要被不同 Runtime 共享时。所以本仓没有为了 MCP 而 MCP。

> 当前最不成熟、面试要主动说的：分布式限流仍是进程内存；OIDC 是 HMAC token scaffold（audience/scope 已按 Resource Server 校验，但还不是完整企业 IdP）；HTTP 传输已具备客户端，远程 replica 部署仍需运维侧。

相关代码：`app/mcp/mcp_gateway.py`、`policy_context.py`、`auth.py`、`server_registry.py`、`session_pool.py`、`server_env.py`、`task_store.py`、`sql_guard.py`。

运维面：`GET /api/tools/mcp`、`GET /api/tools/mcp/gateway/audit`、`GET /api/tools/mcp/tasks/{task_id}`。

---

## 9. 验证

```bash
python3 tests/test_harness_phase10_tools.py
python3 tests/test_harness_phase16_mcp_production.py
python3 tests/test_harness_phase25_mcp.py
```
