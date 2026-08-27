"""
MCP Result Normalizer — 消费 structuredContent / 多 block / resource link。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class MCPToolResult:
    structured_content: Any = None
    text_blocks: list[str] = field(default_factory=list)
    resource_links: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: Any = None

    def model_visible(self) -> Any:
        if self.structured_content is not None:
            return self.structured_content
        if self.resource_links and not self.text_blocks:
            return {"resource_links": self.resource_links, "artifacts": self.artifacts}
        if len(self.text_blocks) == 1:
            text = self.text_blocks[0]
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        if self.text_blocks:
            return {
                "text": self.text_blocks,
                "resource_links": self.resource_links,
                "artifacts": self.artifacts,
            }
        return self.raw if self.raw not in (None, "") else ""


def _block_text(block: Any) -> str:
    if block is None:
        return ""
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        return str(block.get("text") or block.get("uri") or "")
    return str(getattr(block, "text", None) or "")


def _block_uri(block: Any) -> str:
    if isinstance(block, dict):
        return str(block.get("uri") or block.get("resource") or "")
    return str(getattr(block, "uri", None) or "")


def _block_type(block: Any) -> str:
    if isinstance(block, dict):
        return str(block.get("type") or "")
    return str(getattr(block, "type", None) or "")


def normalize_mcp_result(result: Any) -> MCPToolResult:
    if result is None or result == "":
        return MCPToolResult(raw=result)
    if isinstance(result, MCPToolResult):
        return result
    if isinstance(result, (str, int, float, bool, list)):
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
                return normalize_mcp_result(parsed)
            except json.JSONDecodeError:
                return MCPToolResult(text_blocks=[result], raw=result)
        return MCPToolResult(structured_content=result, raw=result)

    if isinstance(result, dict) and "content" not in result and "structuredContent" not in result:
        return MCPToolResult(structured_content=result, raw=result)

    structured = getattr(result, "structuredContent", None)
    if structured is None and isinstance(result, dict):
        structured = result.get("structuredContent")
    is_error = bool(getattr(result, "isError", False))
    if isinstance(result, dict):
        is_error = bool(result.get("isError", is_error))
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")
    blocks = list(content or [])
    text_blocks: list[str] = []
    links: list[str] = []
    artifacts: list[str] = []
    for block in blocks:
        btype = _block_type(block).lower()
        uri = _block_uri(block)
        text = _block_text(block)
        if btype in {"resource", "resource_link"} or uri.startswith(("artifact://", "evidence://", "resource://")):
            if uri:
                links.append(uri)
            if uri.startswith("artifact://"):
                artifacts.append(uri)
            continue
        if text:
            text_blocks.append(text)
    meta: dict[str, Any] = {}
    if hasattr(result, "meta") and getattr(result, "meta"):
        meta = dict(getattr(result, "meta") or {})
    elif isinstance(result, dict) and result.get("meta"):
        meta = dict(result.get("meta") or {})
    return MCPToolResult(
        structured_content=structured,
        text_blocks=text_blocks,
        resource_links=links,
        artifacts=artifacts,
        is_error=is_error,
        metadata=meta,
        raw=result,
    )


def model_visible_result(result: Any) -> Any:
    return normalize_mcp_result(result).model_visible()
