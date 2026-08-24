"""
【Phase 16】MCP Session Pool — 常驻 stdio 连接，避免每次 call 起子进程。

每个 MCP Server 模块对应一个后台 Worker 线程 + 持久 ClientSession。
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import sys
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Literal, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

RequestKind = Literal["call_tool", "list_tools", "list_resources", "read_resource"]


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


class _PersistentMCPWorker:
    """单 MCP Server 模块的后台常驻连接。"""

    def __init__(self, server_module: str, python_executable: Optional[str] = None):
        self.server_module = server_module
        self._python = python_executable or sys.executable
        self._requests: queue.Queue[_PoolRequest] = queue.Queue()
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"mcp-worker-{server_module.split('.')[-1]}",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=45):
            raise TimeoutError(f"MCP worker failed to start: {server_module}")

    def _server_params(self) -> StdioServerParameters:
        return StdioServerParameters(
            command=self._python,
            args=["-m", self.server_module],
            env=os.environ.copy(),
        )

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run_session_loop())
        except Exception as exc:
            print(f"[MCPSessionPool] worker crashed {self.server_module}: {exc}")
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
                        await asyncio.sleep(0.05)
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
            if not blocks:
                return ""
            block = blocks[0]
            return getattr(block, "text", None) or str(block)
        result = await session.call_tool(req.tool_name, req.arguments or {})
        if not result.content:
            return ""
        block = result.content[0]
        text = getattr(block, "text", None) or str(block)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def submit(self, req: _PoolRequest) -> Any:
        if self._stopped.is_set():
            raise RuntimeError(f"MCP worker stopped: {self.server_module}")
        self._requests.put(req)
        ok, payload = req.response_queue.get(timeout=req.timeout_sec + 5)
        if ok:
            return payload
        raise payload  # type: ignore[misc]


class MCPSessionPool:
    """进程级 MCP 连接池（按 server_module 复用 Worker）。"""

    _workers: dict[str, _PersistentMCPWorker] = {}
    _lock = threading.Lock()

    @classmethod
    def get_worker(cls, server_module: str) -> _PersistentMCPWorker:
        with cls._lock:
            worker = cls._workers.get(server_module)
            if worker is None:
                worker = _PersistentMCPWorker(server_module)
                cls._workers[server_module] = worker
            return worker

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
    def shutdown_all(cls) -> None:
        with cls._lock:
            for worker in cls._workers.values():
                worker._stopped.set()
            cls._workers.clear()


def use_session_pool() -> bool:
    from app.config.loader import get_harness_config

    cfg = get_harness_config()
    return cfg.mcp_pool_enabled
