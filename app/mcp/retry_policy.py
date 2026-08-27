"""Retry taxonomy：按副作用分类，禁止对非幂等写操作 blind retry。"""

from __future__ import annotations

import random
from enum import Enum
from typing import Optional


class SideEffectClass(str, Enum):
    READ_ONLY = "read_only"
    IDEMPOTENT_WRITE = "idempotent_write"
    NON_IDEMPOTENT = "non_idempotent"
    DESTRUCTIVE = "destructive"


TOOL_SIDE_EFFECTS: dict[str, SideEffectClass] = {
    "internet_search": SideEffectClass.READ_ONLY,
    "list_sql_tables": SideEffectClass.READ_ONLY,
    "get_table_data": SideEffectClass.READ_ONLY,
    "execute_sql_query": SideEffectClass.READ_ONLY,
    "get_assistant_list": SideEffectClass.READ_ONLY,
    "read_file_content": SideEffectClass.READ_ONLY,
    "read_artifact": SideEffectClass.READ_ONLY,
    "read_evidence": SideEffectClass.READ_ONLY,
    "generate_markdown": SideEffectClass.IDEMPOTENT_WRITE,
    "convert_md_to_pdf": SideEffectClass.IDEMPOTENT_WRITE,
    "convert_md_to_pdf_async": SideEffectClass.IDEMPOTENT_WRITE,
    "create_ask_delete": SideEffectClass.NON_IDEMPOTENT,
}


_RETRYABLE_EXC = (
    TimeoutError,
    ConnectionError,
    ConnectionResetError,
    BrokenPipeError,
    OSError,
)


def side_effect_for_tool(tool_name: str) -> SideEffectClass:
    return TOOL_SIDE_EFFECTS.get(tool_name, SideEffectClass.NON_IDEMPOTENT)


classification_for_tool = side_effect_for_tool


def sleep_seconds(attempt: int, *, base: float = 0.2, cap: float = 2.0) -> float:
    exp = min(cap, base * (2 ** max(0, attempt)))
    jitter = random.uniform(0, exp * 0.3)
    return exp + jitter


def is_retryable_error(exc: Optional[BaseException]) -> bool:
    if exc is None:
        return True
    if isinstance(exc, _RETRYABLE_EXC):
        return True
    text = str(exc).lower()
    return any(token in text for token in ("timeout", "timed out", "connection", "unavailable", "reset"))


def should_retry(
    tool_name: str,
    *,
    attempt: int,
    max_retries: int,
    exc: Optional[BaseException] = None,
) -> bool:
    if attempt >= max_retries:
        return False
    kind = side_effect_for_tool(tool_name)
    if kind in {SideEffectClass.NON_IDEMPOTENT, SideEffectClass.DESTRUCTIVE}:
        return False
    if kind == SideEffectClass.READ_ONLY:
        return is_retryable_error(exc)
    if kind == SideEffectClass.IDEMPOTENT_WRITE:
        return True
    return False
