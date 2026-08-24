"""
【Phase 18】记忆身份 — 请求级 tenant / user / project / session 四元组。

Phase 15 用进程级环境变量 HARNESS_MEMORY_USER_ID 解析身份，在单进程多用户的
FastAPI 服务下所有并发请求会共享同一身份，导致跨用户记忆串写。本模块把身份
下沉为 ContextVar，由 API 层在每次请求开始时绑定，Harness 全程透传。
"""

from __future__ import annotations

import os
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Optional

DEFAULT_TENANT_ID = "default"
DEFAULT_PROJECT_ID = "default"


@dataclass(frozen=True)
class MemoryIdentity:
    """长期记忆的隔离边界。

    - tenant_id: 企业/组织，最外层隔离，任何查询都必须带
    - user_id: 真实用户；缺失时退化为 session_id 且标记 ephemeral
    - project_id: 研究项目/专题，深度研搜的「同项目别重复搜」靠它
    - session_id: 本次运行，只做溯源用，不参与隔离
    """

    tenant_id: str = DEFAULT_TENANT_ID
    user_id: str = ""
    project_id: str = DEFAULT_PROJECT_ID
    session_id: str = ""
    ephemeral: bool = False

    @property
    def is_identified(self) -> bool:
        """是否为可信的真实身份（而非 session 退化身份）。"""
        return bool(self.user_id) and not self.ephemeral

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "ephemeral": self.ephemeral,
        }

    def scope_label(self) -> str:
        return f"{self.tenant_id}/{self.user_id}/{self.project_id}"


_identity_var: ContextVar[Optional[MemoryIdentity]] = ContextVar(
    "memory_identity", default=None
)


def _clean(value: Optional[str]) -> str:
    return (value or "").strip()


def _env(name: str) -> str:
    return _clean(os.getenv(name))


def resolve_memory_identity(
    session_id: str,
    *,
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> MemoryIdentity:
    """按 显式参数 > ContextVar > 环境变量 > session 退化 的优先级解析身份。"""
    bound = _identity_var.get()

    tid = (
        _clean(tenant_id)
        or (bound.tenant_id if bound else "")
        or _env("HARNESS_MEMORY_TENANT_ID")
        or DEFAULT_TENANT_ID
    )
    pid = (
        _clean(project_id)
        or (bound.project_id if bound and bound.project_id else "")
        or _env("HARNESS_MEMORY_PROJECT_ID")
        or DEFAULT_PROJECT_ID
    )
    uid = (
        _clean(user_id)
        or (bound.user_id if bound and bound.is_identified else "")
        or _env("HARNESS_MEMORY_USER_ID")
    )

    ephemeral = False
    if not uid:
        # 无任何真实身份：退化为 session 级私有记忆，并标记 ephemeral，
        # 便于 policy.require_explicit_identity 在生产环境拒绝写入。
        uid = _clean(session_id) or "anonymous"
        ephemeral = True

    return MemoryIdentity(
        tenant_id=tid,
        user_id=uid,
        project_id=pid,
        session_id=_clean(session_id),
        ephemeral=ephemeral,
    )


def set_memory_identity(identity: MemoryIdentity) -> Token:
    """绑定当前上下文身份，返回 Token 供 reset 使用。"""
    return _identity_var.set(identity)


def reset_memory_identity(token: Token) -> None:
    _identity_var.reset(token)


def get_memory_identity() -> Optional[MemoryIdentity]:
    return _identity_var.get()
