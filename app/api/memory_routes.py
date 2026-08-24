"""
【Phase 15】Memory API — 查询 / 召回 / 显式写入 / 删除 / 审计
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.agent.memory.models import MemoryType, WriteSource
from app.agent.memory.policy import resolve_memory_tenant_id, resolve_memory_user_id
from app.agent.memory.store import MemoryStore

router = APIRouter(prefix="/api/memory", tags=["memory"])


class MemoryWriteBody(BaseModel):
    fact: str = Field(..., min_length=1)
    memory_type: MemoryType = MemoryType.SEMANTIC
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    user_id: Optional[str] = None
    tenant_id: Optional[str] = None


@router.get("/records/{user_id}")
def list_memory_records(
    user_id: str,
    tenant_id: str = Query(default=""),
    include_deleted: bool = Query(default=False),
) -> dict[str, Any]:
    """列出用户有效记忆（TTL 过滤）。"""
    store = MemoryStore()
    uid = user_id if user_id != "me" else resolve_memory_user_id("session")
    tid = tenant_id or resolve_memory_tenant_id()
    records = store.list_records(uid, tenant_id=tid, include_deleted=include_deleted)
    return {
        "tenant_id": tid,
        "user_id": uid,
        "total": len(records),
        "records": [r.to_dict() for r in records],
    }


@router.get("/recall")
async def recall_memory(
    query: str = Query(..., min_length=1),
    user_id: str = Query(default="demo"),
    tenant_id: str = Query(default=""),
    top_k: int = Query(default=5, ge=1, le=20),
) -> dict[str, Any]:
    """调试 Hybrid Recall（与 Harness build_context 同口径）。"""
    store = MemoryStore()
    uid = resolve_memory_user_id(user_id)
    tid = tenant_id or resolve_memory_tenant_id()
    result = await store.recall_with_metrics(query, uid, tenant_id=tid, top_k=top_k)
    return {
        "tenant_id": tid,
        "user_id": uid,
        "query": query,
        "total": len(result.records),
        "recall_at_k": result.recall_at_k,
        "keyword_hits": result.keyword_hits,
        "embedding_used": result.embedding_used,
        "facts": [r.fact for r in result.records],
        "records": [r.to_dict() for r in result.records],
    }


@router.post("/facts")
async def remember_explicit(body: MemoryWriteBody) -> dict[str, Any]:
    """用户显式写入长期记忆（GDPR 友好入口之一）。"""
    store = MemoryStore()
    uid = resolve_memory_user_id(body.user_id or "session")
    tid = body.tenant_id or resolve_memory_tenant_id()
    from app.agent.memory.models import MemoryWriteRequest

    saved = await store.remember_writes(
        [
            MemoryWriteRequest(
                fact=body.fact,
                memory_type=body.memory_type,
                confidence=body.confidence,
                write_source=WriteSource.USER_EXPLICIT,
            )
        ],
        user_id=uid,
        tenant_id=tid,
    )
    return {"saved": saved, "user_id": uid, "tenant_id": tid}


@router.delete("/records/{user_id}/{record_id}")
async def delete_memory_record(
    user_id: str,
    record_id: str,
    tenant_id: str = Query(default=""),
) -> dict[str, Any]:
    """软删除单条记忆（合规遗忘）。"""
    store = MemoryStore()
    uid = resolve_memory_user_id(user_id)
    tid = tenant_id or resolve_memory_tenant_id()
    ok = await store.delete(record_id, uid, tenant_id=tid)
    if not ok:
        raise HTTPException(status_code=404, detail="record not found")
    return {"deleted": True, "record_id": record_id}


@router.delete("/records/{user_id}")
async def forget_user_memory(
    user_id: str,
    tenant_id: str = Query(default=""),
) -> dict[str, Any]:
    """软删除用户全部记忆。"""
    store = MemoryStore()
    uid = resolve_memory_user_id(user_id)
    tid = tenant_id or resolve_memory_tenant_id()
    count = await store.forget_user(uid, tenant_id=tid)
    return {"deleted_count": count, "user_id": uid, "tenant_id": tid}


@router.get("/audit")
def list_memory_audit(
    tenant_id: str = Query(default=""),
    user_id: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    store = MemoryStore()
    audit = store.get_audit_log()
    if audit is None:
        return {"entries": [], "message": "audit not available for current provider"}
    tid = tenant_id or resolve_memory_tenant_id()
    uid = user_id or None
    entries = audit.list_entries(tenant_id=tid, user_id=uid, limit=limit)
    return {"tenant_id": tid, "entries": entries}
