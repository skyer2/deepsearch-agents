"""
MCP Access Token — 真实 caller identity，而不是进程用自己的 env 校验自己。

Token 不出现在 URI，也不 passthrough 给下游 MySQL/Tavily。
Gateway 校验 issuer / audience / expiry / tenant / scopes。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional


DEFAULT_AUDIENCE = "https://mcp.local/gateway"
DEFAULT_ISSUER = "deepsearch-harness"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def token_secret() -> str:
    return (
        os.getenv("HARNESS_MCP_TOKEN_SECRET")
        or os.getenv("HARNESS_MCP_GATEWAY_TOKEN")
        or "dev-mcp-token-secret"
    ).strip()


@dataclass
class MCPPrincipal:
    tenant_id: str = "default"
    user_id: str = ""
    roles: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)
    audience: str = DEFAULT_AUDIENCE
    issuer: str = DEFAULT_ISSUER
    expires_at: float = 0.0
    token_id: str = ""
    ephemeral: bool = False

    @property
    def is_identified(self) -> bool:
        return bool(self.user_id) and not self.ephemeral

    def to_claims(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "roles": list(self.roles),
            "scopes": list(self.scopes),
            "aud": self.audience,
            "iss": self.issuer,
            "exp": int(self.expires_at),
            "jti": self.token_id,
            "ephemeral": self.ephemeral,
        }


class TokenError(PermissionError):
    """Access token 校验失败。"""


MCPAuthError = TokenError


class MCPTokenValidator:
    """Gateway 侧 Resource Server：校验 caller 提交的 token，禁止自校验 env。"""

    def validate(
        self,
        token: str,
        *,
        expected_audience: str = DEFAULT_AUDIENCE,
        expected_issuer: str = DEFAULT_ISSUER,
    ) -> MCPPrincipal:
        return validate_access_token(
            token,
            expected_audience=expected_audience,
            expected_issuer=expected_issuer,
        )


_validator: MCPTokenValidator | None = None


def get_mcp_token_validator() -> MCPTokenValidator:
    global _validator
    if _validator is None:
        _validator = MCPTokenValidator()
    return _validator


def issue_access_token(
    principal: MCPPrincipal,
    *,
    secret: Optional[str] = None,
    ttl_sec: int = 3600,
    audience: str = DEFAULT_AUDIENCE,
) -> str:
    """签发 Gateway 专用 access token。不得把该 token 转给下游 DB/SaaS。"""
    payload = principal.to_claims()
    payload["aud"] = audience
    payload["exp"] = int(time.time()) + max(60, ttl_sec)
    if not payload.get("jti"):
        payload["jti"] = _b64url(os.urandom(8))
    blob = _b64url(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = hmac.new((secret or token_secret()).encode("utf-8"), blob.encode("ascii"), hashlib.sha256).hexdigest()
    return f"mcp.{blob}.{sig}"


def validate_access_token(
    token: str,
    *,
    expected_audience: str = DEFAULT_AUDIENCE,
    expected_issuer: str = DEFAULT_ISSUER,
    secret: Optional[str] = None,
    now: Optional[float] = None,
) -> MCPPrincipal:
    raw = (token or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    if not raw:
        raise TokenError("missing_access_token")
    parts = raw.split(".")
    if len(parts) != 3 or parts[0] != "mcp":
        raise TokenError("malformed_access_token")
    blob, sig = parts[1], parts[2]
    expected = hmac.new((secret or token_secret()).encode("utf-8"), blob.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise TokenError("invalid_token_signature")
    try:
        claims = json.loads(_b64url_decode(blob).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TokenError("invalid_token_payload") from exc
    if str(claims.get("aud") or "") != expected_audience:
        raise TokenError("invalid_audience")
    if expected_issuer and str(claims.get("iss") or "") != expected_issuer:
        raise TokenError("invalid_issuer")
    exp = float(claims.get("exp") or 0)
    if exp and exp < (now if now is not None else time.time()):
        raise TokenError("token_expired")
    return MCPPrincipal(
        tenant_id=str(claims.get("tenant_id") or "default"),
        user_id=str(claims.get("user_id") or ""),
        roles=[str(r) for r in (claims.get("roles") or [])],
        scopes=[str(s) for s in (claims.get("scopes") or [])],
        audience=str(claims.get("aud") or expected_audience),
        issuer=str(claims.get("iss") or expected_issuer),
        expires_at=exp,
        token_id=str(claims.get("jti") or ""),
        ephemeral=bool(claims.get("ephemeral", False)),
    )
