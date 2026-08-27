"""
JIT 回读工具：read_artifact / read_evidence。

模型窗口只留 ref；需要原文 span 时按 id 取回。权限仍走 ToolGateway。
"""

from __future__ import annotations

import json

from langchain_core.tools import tool

from app.agent.harness.artifacts import get_artifact_store
from app.agent.harness.evidence_store import get_evidence_store


@tool
def read_artifact(
    artifact_id: str,
    start: int = 0,
    end: int = 0,
    query: str = "",
    max_chars: int = 4000,
) -> str:
    """按 artifact_id 回读已外置的原始工具结果（网页/SQL/文件/KB）。

    优先用 query 检索相关片段；否则按 start/end 字符偏移切片。
    """
    store = get_artifact_store()
    end_arg = None if not end else end
    payload = store.read(
        artifact_id,
        start=start or 0,
        end=end_arg,
        query=query or "",
        max_chars=max(200, min(int(max_chars or 4000), 12000)),
    )
    return json.dumps(payload, ensure_ascii=False)


@tool
def read_evidence(
    evidence_id: str,
    include_artifact: bool = False,
    max_chars: int = 2000,
) -> str:
    """按 evidence_id 回读证据 span（claim 对应的原文片段）。"""
    store = get_evidence_store()
    payload = store.read(
        evidence_id,
        include_artifact=bool(include_artifact),
        max_chars=max(200, min(int(max_chars or 2000), 8000)),
    )
    return json.dumps(payload, ensure_ascii=False)
