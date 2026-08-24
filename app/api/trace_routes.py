"""
Trace API：JSONL 本地 trace + Langfuse 代理，供前端 Trace 查看器使用。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests
from fastapi import APIRouter, HTTPException

from app.api.tracing import is_langfuse_enabled
from app.config.loader import get_harness_config

ROOT = Path(__file__).resolve().parents[1]

router = APIRouter(prefix="/api/traces", tags=["traces"])


def _jsonl_path(session_id: str) -> Path:
    config = get_harness_config()
    log_dir = ROOT / config.jsonl_log_dir
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
    return log_dir / f"{safe_id}.jsonl"


@router.get("/langfuse/config")
def langfuse_viewer_config() -> dict[str, Any]:
    host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
    enabled = is_langfuse_enabled() and get_harness_config().langfuse_enabled
    return {
        "enabled": enabled,
        "host": host,
        "session_filter_hint": "按 session_id (= thread_id) 过滤",
        "ui_url": f"{host}/" if enabled else None,
    }


@router.get("/jsonl/{session_id}")
def get_jsonl_trace(session_id: str) -> dict[str, Any]:
    path = _jsonl_path(session_id)
    if not path.exists():
        return {
            "session_id": session_id,
            "events": [],
            "total": 0,
            "source": "jsonl",
            "path": str(path),
            "message": "暂无 JSONL trace，请先完成一次 Harness run",
        }

    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))

    return {
        "session_id": session_id,
        "events": events,
        "total": len(events),
        "source": "jsonl",
        "path": str(path),
    }


@router.get("/langfuse/{session_id}")
def get_langfuse_traces(session_id: str) -> dict[str, Any]:
    if not is_langfuse_enabled() or not get_harness_config().langfuse_enabled:
        return {
            "session_id": session_id,
            "enabled": False,
            "traces": [],
            "message": "Langfuse 未配置，请设置 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY",
        }

    host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")

    try:
        resp = requests.get(
            f"{host}/api/public/traces",
            params={"sessionId": session_id, "limit": 20},
            auth=(public_key, secret_key),
            timeout=8,
        )
        if resp.status_code == 404:
            return {
                "session_id": session_id,
                "enabled": True,
                "traces": [],
                "message": "Langfuse 中暂无该 session 的 trace",
            }
        resp.raise_for_status()
        payload = resp.json()
        traces = payload.get("data", payload if isinstance(payload, list) else [])
        return {
            "session_id": session_id,
            "enabled": True,
            "traces": traces,
            "total": len(traces),
            "ui_url": f"{host}/",
        }
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Langfuse API 请求失败: {exc}",
        ) from exc


@router.get("/citations/{session_id}")
def get_citations(session_id: str) -> dict[str, Any]:
    """【Phase 6】读取 session 证据链 evidence.json。"""
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
    path = ROOT / "output" / f"session_{safe_id}" / "evidence.json"
    if not path.exists():
        return {
            "session_id": session_id,
            "sources": [],
            "total": 0,
            "message": "暂无证据链，请完成带 Citation-First 的 Harness run",
        }
    data = json.loads(path.read_text(encoding="utf-8"))
    sources = data.get("sources", [])
    return {
        "session_id": session_id,
        "sources": sources,
        "total": len(sources),
        "generated_at": data.get("generated_at"),
    }
