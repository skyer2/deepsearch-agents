"""Opaque Resource refs + session ACL。"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Optional

from app.mcp.policy_context import ToolCallContext


def default_resource_store_path() -> Path:
    raw = os.getenv("HARNESS_MCP_RESOURCE_STORE", "").strip()
    if raw:
        return Path(raw)
    return Path("mcp_data") / "resources.db"


class ResourceRegistry:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path or default_resource_store_path())
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS resource_refs (
                    id TEXT PRIMARY KEY,
                    uri TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()

    def mint(self, uri: str, ctx: ToolCallContext) -> str:
        digest = hashlib.sha1(
            f"{ctx.tenant_id}:{ctx.user_id}:{ctx.session_id}:{uri}".encode("utf-8")
        ).hexdigest()[:16]
        rid = f"res_{digest}"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO resource_refs (id, uri, tenant_id, user_id, session_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (rid, uri, ctx.tenant_id, ctx.user_id, ctx.session_id),
            )
            conn.commit()
        return rid

    def resolve(self, ref_or_uri: str, ctx: ToolCallContext) -> str:
        raw = (ref_or_uri or "").strip()
        if raw.startswith("session://"):
            authorize_session_uri(raw, ctx)
            return raw
        if not raw.startswith("res_"):
            return raw
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT uri, tenant_id, user_id FROM resource_refs WHERE id=?",
                (raw,),
            ).fetchone()
        if not row:
            raise PermissionError("unknown_resource_ref")
        if row[1] != ctx.tenant_id or (ctx.user_id and row[2] != ctx.user_id):
            raise PermissionError("resource_acl_denied")
        return str(row[0])


def authorize_session_uri(uri: str, ctx: ToolCallContext) -> None:
    rest = uri.removeprefix("session://")
    session_id = rest.split("/", 1)[0]
    if ctx.session_id and session_id and session_id != ctx.session_id:
        raise PermissionError("resource_acl_denied")


_resources: ResourceRegistry | None = None


def get_resource_registry() -> ResourceRegistry:
    global _resources
    if _resources is None:
        _resources = ResourceRegistry()
    return _resources
