"""
2026-07-28 风格 stateless Streamable HTTP MCP 客户端。

每个请求自包含，走普通 HTTP；本地开发仍用 stdio。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Optional


class MCPHTTPError(RuntimeError):
    pass


MCPHttpError = MCPHTTPError


def call_http_tool(
    endpoint: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    access_token: str = "",
    timeout_sec: float = 30.0,
    opener: Optional[Any] = None,
) -> Any:
    """POST JSON-RPC tools/call。测试可注入 opener。"""
    if not endpoint:
        raise MCPHTTPError("missing_http_endpoint")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments or {}},
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(endpoint.rstrip("/") + "/mcp", data=body, headers=headers, method="POST")
    try:
        if opener is not None:
            resp = opener(req, timeout=timeout_sec)
            raw = resp.read() if hasattr(resp, "read") else resp
        else:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                raw = resp.read()
    except urllib.error.URLError as exc:
        raise MCPHTTPError(str(exc)) from exc
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(data, dict) and data.get("error"):
        raise MCPHTTPError(str(data["error"]))
    if isinstance(data, dict) and "result" in data:
        return data["result"]
    return data


def list_http_tools(endpoint: str, *, access_token: str = "", timeout_sec: float = 15.0) -> list[dict[str, Any]]:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"Content-Type": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(endpoint.rstrip("/") + "/mcp", data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    result = data.get("result") if isinstance(data, dict) else data
    tools = result.get("tools") if isinstance(result, dict) else result
    return list(tools or [])
