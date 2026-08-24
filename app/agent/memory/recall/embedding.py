"""
【Phase 15】Embedding 客户端 — OpenAI 兼容接口，失败时降级。
"""

from __future__ import annotations

import os
from typing import Optional

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

_client: Optional[object] = None
_disabled_reason: Optional[str] = None


def embedding_enabled() -> bool:
    return os.getenv("HARNESS_MEMORY_EMBEDDING_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _get_client():
    global _client, _disabled_reason
    if _client is not None:
        return _client
    if not embedding_enabled():
        _disabled_reason = "embedding_disabled"
        return None
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        _disabled_reason = "missing_openai_api_key"
        return None
    try:
        from openai import OpenAI

        base_url = os.getenv("OPENAI_BASE_URL") or None
        _client = OpenAI(api_key=api_key, base_url=base_url)
        return _client
    except Exception as exc:
        _disabled_reason = str(exc)
        return None


def embedding_model_name() -> str:
    return os.getenv("HARNESS_MEMORY_EMBEDDING_MODEL", "text-embedding-v3")


async def embed_text(text: str) -> Optional[list[float]]:
    client = _get_client()
    if client is None:
        return None
    try:
        response = client.embeddings.create(
            model=embedding_model_name(),
            input=text[:8000],
        )
        data = response.data[0].embedding
        return [float(x) for x in data]
    except Exception as exc:
        global _disabled_reason
        _disabled_reason = str(exc)
        return None


def embed_text_sync(text: str) -> Optional[list[float]]:
    import asyncio

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return None
    except RuntimeError:
        pass
    return asyncio.run(embed_text(text))


def get_embedding_status() -> dict[str, str | bool]:
    return {
        "enabled": embedding_enabled(),
        "model": embedding_model_name(),
        "available": _get_client() is not None,
        "disabled_reason": _disabled_reason or "",
    }
