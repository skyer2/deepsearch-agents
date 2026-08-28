# DeepSearch Agents Frontend

React + Vite + Tailwind CSS + Ant Design frontend for the DeepSearch Agents FastAPI backend.

生产交互按任务状态机渲染，而不是「只要没结束就一直转圈」：

| 状态 | UI |
|------|----|
| `idle` | 待命 |
| `running` | 按 Harness Phase 显示**确定性**进度（0–100%） |
| `awaiting_approval` | HITL 暂停：进度条冻结、计时停止、无闪烁/扫光 |
| `cancelling` / `completed` / `failed` | 对应静态结果态 |

HITL 审批卡片会吸顶，输入框提示「任务已暂停」。原始 WebSocket 事件默认折进「原始事件日志」，主界面只保留阶段时间线。

开发时可用 `http://localhost:5173/?preview=run-states` 对照运行中 / 暂停 / 完成三种进度条。

## Run

```bash
pnpm install
pnpm dev
```

By default the app talks to `http://localhost:8000` and `ws://localhost:8000`.
Override with `.env.local`:

```bash
VITE_API_BASE_URL=http://localhost:8000
VITE_WS_BASE_URL=ws://localhost:8000
```

## Backend Contract

- `POST /api/task`
- `POST /api/upload`
- `GET /api/files`
- `GET /api/download`
- `POST /api/task/{thread_id}/resume`
- `WebSocket /ws/{thread_id}`（`phase` / `hitl_interrupt` / `task_result`）
