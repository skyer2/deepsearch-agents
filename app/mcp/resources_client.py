"""
【Phase 16】MCP Resources 客户端 — 为 ContextBuilder 提供会话文件摘要。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from app.mcp.registry_sync import SERVER_MODULES_BY_ID
from app.mcp.session_pool import MCPSessionPool, use_session_pool


def list_session_resource_uris(session_id: str, session_dir: Optional[str] = None) -> list[str]:
    root = Path(session_dir) if session_dir else None
    if root is None or not root.exists():
        return []
    uris: list[str] = []
    for path in sorted(root.iterdir()):
        if path.is_file() and path.suffix.lower() in {".md", ".txt", ".pdf"}:
            uris.append(f"session://{session_id}/{path.name}")
    return uris


def build_resources_context(session_id: str, session_dir: Optional[str] = None) -> str:
    """构建 MCP Resources 层上下文（pool 开启时尝试 list_resources）。"""
    uris = list_session_resource_uris(session_id, session_dir)
    if not uris:
        return ""

    lines = ["【MCP Resources — 会话产物索引】"]
    if use_session_pool():
        try:
            listed = MCPSessionPool.list_resources_sync(SERVER_MODULES_BY_ID["files-mcp"])
            if listed:
                for item in listed[:5]:
                    lines.append(f"  - {item.get('uri')}: {item.get('description','')}")
        except Exception:
            pass

    for uri in uris[:8]:
        lines.append(f"  - {uri}")
    lines.append("  （可通过 resources/read 或 read_file_content 读取，勿重复粘贴全文）")
    return "\n".join(lines)
