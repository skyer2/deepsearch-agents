"""
【Phase 15】Memory API — 查询 / 召回 / 显式写入 / 删除 / 审计
【Phase 18】请求级身份、来源台账、巩固入口。
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.agent.memory.identity import resolve_memory_identity
from app.agent.memory.models import MemoryType, MemoryWriteRequest, WriteSource
from app.agent.memory.store import get_memory_store

router = APIRouter(prefix="/api/memory", tags=["memory"])


class MemoryWriteBody(BaseModel):
    fact: str = Field(..., min_length=1)
    memory_type: MemoryType = MemoryType.SEMANTIC
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    user_id: Optional[str] = None
    tenant_id: Optional[str] = None
    project_id: Optional[str] = None
    session_id: Optional[str] = None


class MemoryIdentityQuery(BaseModel):
    user_id: str
    tenant_id: Optional[str] = None
    project_id: Optional[str] = None
    session_id: Optional[str] = None


def _identity(
    *,
    user_id: str,
    tenant_id: str = "",
    project_id: str = "",
    session_id: str = "",
):
    uid = user_id if user_id and user_id != "me" else ""
    return resolve_memory_identity(
        session_id or uid or "session",
        user_id=uid or None,
        tenant_id=tenant_id or None,
        project_id=project_id or None,
    )


@router.get("/records/{user_id}")
def list_memory_records(
    user_id: str,
    tenant_id: str = Query(default=""),
    project_id: str = Query(default=""),
    include_deleted: bool = Query(default=False),
) -> dict[str, Any]:
    """列出用户有效记忆（TTL 过滤）。"""
    store = get_memory_store()
    ident = _identity(user_id=user_id, tenant_id=tenant_id, project_id=project_id)
    records = store.list_records(
        ident.user_id,
        tenant_id=ident.tenant_id,
        include_deleted=include_deleted,
        project_id=ident.project_id,
        identity=ident,
    )
    return {
        "tenant_id": ident.tenant_id,
        "user_id": ident.user_id,
        "project_id": ident.project_id,
        "total": len(records),
        "records": [r.to_dict() for r in records],
    }


@router.get("/recall")
async def recall_memory(
    query: str = Query(..., min_length=1),
    user_id: str = Query(default="demo"),
    tenant_id: str = Query(default=""),
    project_id: str = Query(default=""),
    top_k: int = Query(default=5, ge=1, le=20),
    target_step_type: str = Query(default=""),
) -> dict[str, Any]:
    """调试 Hybrid Recall（与 Harness build_context 同口径）。"""
    store = get_memory_store()
    ident = _identity(user_id=user_id, tenant_id=tenant_id, project_id=project_id)
    result = await store.recall_with_metrics(
        query,
        ident.user_id,
        identity=ident,
        top_k=top_k,
        target_step_type=target_step_type,
    )
    return {
        "tenant_id": ident.tenant_id,
        "user_id": ident.user_id,
        "project_id": ident.project_id,
        "query": query,
        "total": len(result.records),
        "mean_recall_score": result.mean_recall_score,
        "recall_at_k": result.mean_recall_score,
        "keyword_hits": result.keyword_hits,
        "embedding_used": result.embedding_used,
        "trust_filtered": result.trust_filtered,
        "by_trust": result.by_trust,
        "facts": [r.fact for r in result.records],
        "records": [r.to_dict() for r in result.records],
    }


@router.post("/facts")
async def remember_explicit(body: MemoryWriteBody) -> dict[str, Any]:
    """用户显式写入长期记忆（GDPR 友好入口之一）。"""
    store = get_memory_store()
    ident = _identity(
        user_id=body.user_id or "session",
        tenant_id=body.tenant_id or "",
        project_id=body.project_id or "",
        session_id=body.session_id or "",
    )
    saved = await store.remember_writes(
        [
            MemoryWriteRequest(
                fact=body.fact,
                memory_type=body.memory_type,
                confidence=body.confidence,
                write_source=WriteSource.USER_EXPLICIT,
                project_id=ident.project_id,
                session_id=ident.session_id,
            )
        ],
        user_id=ident.user_id,
        identity=ident,
    )
    return {
        "saved": saved,
        "user_id": ident.user_id,
        "tenant_id": ident.tenant_id,
        "project_id": ident.project_id,
    }


@router.delete("/records/{user_id}/{record_id}")
async def delete_memory_record(
    user_id: str,
    record_id: str,
    tenant_id: str = Query(default=""),
) -> dict[str, Any]:
    """软删除单条记忆（合规遗忘）。"""
    store = get_memory_store()
    ident = _identity(user_id=user_id, tenant_id=tenant_id)
    ok = await store.delete(record_id, ident.user_id, tenant_id=ident.tenant_id)
    if not ok:
        raise HTTPException(status_code=404, detail="record not found")
    return {"deleted": True, "record_id": record_id}


@router.delete("/records/{user_id}")
async def forget_user_memory(
    user_id: str,
    tenant_id: str = Query(default=""),
) -> dict[str, Any]:
    """软删除用户全部记忆。"""
    store = get_memory_store()
    ident = _identity(user_id=user_id, tenant_id=tenant_id)
    count = await store.forget_user(ident.user_id, tenant_id=ident.tenant_id)
    return {"deleted_count": count, "user_id": ident.user_id, "tenant_id": ident.tenant_id}


@router.get("/sources")
def list_source_ledger(
    user_id: str = Query(default="demo"),
    tenant_id: str = Query(default=""),
    project_id: str = Query(default="default"),
    limit: int = Query(default=8, ge=1, le=50),
) -> dict[str, Any]:
    store = get_memory_store()
    ident = _identity(user_id=user_id, tenant_id=tenant_id, project_id=project_id)
    entries = store.list_sources(identity=ident, limit=limit)
    return {
        "tenant_id": ident.tenant_id,
        "user_id": ident.user_id,
        "project_id": ident.project_id,
        "total": len(entries),
        "sources": [e.to_dict() for e in entries],
    }


@router.post("/consolidate")
async def consolidate_memory(body: MemoryIdentityQuery) -> dict[str, Any]:
    """手动触发衰减 / 晋升 / 硬清理。"""
    store = get_memory_store()
    ident = _identity(
        user_id=body.user_id,
        tenant_id=body.tenant_id or "",
        project_id=body.project_id or "",
        session_id=body.session_id or "",
    )
    report = await store.consolidate(user_id=ident.user_id, identity=ident)
    return {"identity": ident.to_dict(), "report": report}


@router.get("/audit")
def list_memory_audit(
    tenant_id: str = Query(default=""),
    user_id: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    store = get_memory_store()
    audit = store.get_audit_log()
    if audit is None:
        return {"entries": [], "message": "audit not available for current provider"}
    ident = _identity(user_id=user_id or "demo", tenant_id=tenant_id)
    uid = user_id or None
    entries = audit.list_entries(tenant_id=ident.tenant_id, user_id=uid, limit=limit)
    return {"tenant_id": ident.tenant_id, "entries": entries}
