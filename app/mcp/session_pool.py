"""
MCP Session Pool — 常驻 stdio 连接，避免每次 call 起子进程。

每个 MCP Server 维护一组 Worker（默认 3），round-robin 并发；crash 后丢弃并重建。
生产远程路径走 stateless HTTP，stdio 仅 local/dev。
"""

from __future__ import annotations

import asyncio
import json
import queue
import sys
import threading
from dataclasses import dataclass
from typing import Any, Literal, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.mcp.result_normalizer import normalize_mcp_result
from app.mcp.server_env import build_server_env, server_id_for_module

RequestKind = Literal[
    "call_tool",
    "list_tools",
    "list_resources",
    "read_resource",
    "get_task",
]


@dataclass
class _PoolRequest:
    kind: RequestKind
    tool_name: str = ""
    arguments: dict[str, Any] | None = None
    uri: str = ""
    timeout_sec: float = 30.0
    response_queue: queue.Queue = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.response_queue is None:
            self.response_queue = queue.Queue(maxsize=1)


def _pool_size() -> int:
    try:
        from app.config.loader import get_harness_config

        return max(1, int(getattr(get_harness_config(), "mcp_pool_size", 3) or 3))
    except Exception:
        return 3


def _queue_limit() -> int:
    try:
        from app.config.loader import get_harness_config

        return max(1, int(getattr(get_harness_config(), "mcp_queue_limit", 32) or 32))
    except Exception:
        return 32


class MCPSessionError(RuntimeError):
    pass


class _PersistentMCPWorker:
    """单 MCP Server 的一条常驻 stdio 连接。"""

    def __init__(
        self,
        server_module: str,
        python_executable: Optional[str] = None,
        *,
        worker_id: int = 0,
        queue_limit: Optional[int] = None,
    ):
        self.server_module = server_module
        self.worker_id = worker_id
        self._python = python_executable or sys.executable
        self._requests: queue.Queue[_PoolRequest] = queue.Queue(maxsize=queue_limit or _queue_limit())
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"mcp-worker-{server_module.split('.')[-1]}-{worker_id}",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=45):
            self._stopped.set()
            raise TimeoutError(f"MCP worker failed to start: {server_module}")

    @property
    def is_stopped(self) -> bool:
        return self._stopped.is_set()

    def _server_params(self) -> StdioServerParameters:
        server_id = server_id_for_module(self.server_module)
        env = build_server_env(server_id)
        return StdioServerParameters(
            command=self._python,
            args=["-m", self.server_module],
            env=env,
        )

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run_session_loop())
        except Exception as exc:
            print(f"[MCPSessionPool] worker crashed {self.server_module}#{self.worker_id}: {exc}")
        finally:
            loop.close()
            self._stopped.set()

    async def _run_session_loop(self) -> None:
        async with stdio_client(self._server_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                self._ready.set()
                while not self._stopped.is_set():
                    try:
                        req = self._requests.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.02)
                        continue
                    try:
                        result = await asyncio.wait_for(
                            self._dispatch(session, req),
                            timeout=req.timeout_sec,
                        )
                        req.response_queue.put((True, result))
                    except Exception as exc:
                        req.response_queue.put((False, exc))

    async def _dispatch(self, session: ClientSession, req: _PoolRequest) -> Any:
        if req.kind == "list_tools":
            tools = await session.list_tools()
            return [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "input_schema": getattr(t, "inputSchema", None) or {},
                    "output_schema": getattr(t, "outputSchema", None) or {},
                }
                for t in tools.tools
            ]
        if req.kind == "list_resources":
            resources = await session.list_resources()
            return [
                {"uri": r.uri, "name": r.name or "", "description": r.description or ""}
                for r in resources.resources
            ]
        if req.kind == "read_resource":
            content = await session.read_resource(req.uri)
            blocks = content.contents or []
            texts: list[str] = []
            for block in blocks:
                texts.append(getattr(block, "text", None) or str(block))
            return "\n".join(texts) if texts else ""
        if req.kind == "get_task":
            result = await session.call_tool("tasks_get", {"task_id": req.tool_name})
            return normalize_mcp_result(result).model_visible()
        result = await session.call_tool(req.tool_name, req.arguments or {})
        visible = normalize_mcp_result(result).model_visible()
        if isinstance(visible, str):
            try:
                return json.loads(visible)
            except json.JSONDecodeError:
                return visible
        return visible

    def submit(self, req: _PoolRequest) -> Any:
        if self._stopped.is_set():
            raise MCPSessionError(f"MCP worker stopped: {self.server_module}")
        try:
            self._requests.put(req, timeout=min(5.0, req.timeout_sec))
        except queue.Full as exc:
            raise MCPSessionError(f"MCP queue full: {self.server_module}") from exc
        try:
            ok, payload = req.response_queue.get(timeout=req.timeout_sec + 5)
        except queue.Empty as exc:
            raise MCPSessionError(f"MCP call timeout: {self.server_module}.{req.kind}") from exc
        if ok:
            return payload
        raise payload  # type: ignore[misc]


class MCPSessionPool:
    """进程级 MCP 连接池（按 server_module 复用一组 Worker）。"""

    _workers: dict[str, list[_PersistentMCPWorker]] = {}
    _cursor: dict[str, int] = {}
    _lock = threading.Lock()

    @classmethod
    def get_worker(cls, server_module: str) -> _PersistentMCPWorker:
        size = _pool_size()
        with cls._lock:
            alive = [w for w in cls._workers.get(server_module, []) if not w.is_stopped]
            next_id = len(cls._workers.get(server_module, []))
            while len(alive) < size:
                worker = _PersistentMCPWorker(server_module, worker_id=next_id)
                alive.append(worker)
                next_id += 1
            cls._workers[server_module] = alive
            idx = cls._cursor.get(server_module, 0) % len(alive)
            cls._cursor[server_module] = idx + 1
            return alive[idx]

    @classmethod
    def call_tool_sync(
        cls,
        server_module: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_sec: float = 30.0,
    ) -> Any:
        worker = cls.get_worker(server_module)
        req = _PoolRequest(
            kind="call_tool",
            tool_name=tool_name,
            arguments=arguments,
            timeout_sec=timeout_sec,
        )
        return worker.submit(req)

    @classmethod
    def list_tools_sync(cls, server_module: str, *, timeout_sec: float = 30.0) -> list[dict]:
        worker = cls.get_worker(server_module)
        req = _PoolRequest(kind="list_tools", timeout_sec=timeout_sec)
        result = worker.submit(req)
        return list(result or [])

    @classmethod
    def list_resources_sync(cls, server_module: str, *, timeout_sec: float = 15.0) -> list[dict]:
        worker = cls.get_worker(server_module)
        req = _PoolRequest(kind="list_resources", timeout_sec=timeout_sec)
        result = worker.submit(req)
        return list(result or [])

    @classmethod
    def read_resource_sync(cls, server_module: str, uri: str, *, timeout_sec: float = 30.0) -> str:
        worker = cls.get_worker(server_module)
        req = _PoolRequest(kind="read_resource", uri=uri, timeout_sec=timeout_sec)
        result = worker.submit(req)
        return str(result or "")

    @classmethod
    def get_task_sync(cls, server_module: str, task_id: str, *, timeout_sec: float = 15.0) -> Any:
        worker = cls.get_worker(server_module)
        req = _PoolRequest(kind="get_task", tool_name=task_id, timeout_sec=timeout_sec)
        return worker.submit(req)

    @classmethod
    def shutdown_all(cls) -> None:
        with cls._lock:
            for workers in cls._workers.values():
                for worker in workers:
                    worker._stopped.set()
            cls._workers.clear()
            cls._cursor.clear()


def use_session_pool() -> bool:
    from app.config.loader import get_harness_config

    cfg = get_harness_config()
    return cfg.mcp_pool_enabled


def get_mcp_session_pool() -> type[MCPSessionPool]:
    return MCPSessionPool


def reset_mcp_session_pool() -> None:
    MCPSessionPool.shutdown_all()
