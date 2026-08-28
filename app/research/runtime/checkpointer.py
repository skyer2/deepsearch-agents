"""Durable ResearchState checkpointer：默认 SQLite 文件，失败回退内存。"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SQLITE_SAVER: Any = None
_SQLITE_CONN: sqlite3.Connection | None = None
_SQLITE_PATH: str | None = None

DEFAULT_CHECKPOINT_PATH = "output/.harness/graph_checkpoints.sqlite"


def memory_checkpointer():
    try:
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()
    except ImportError:
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()


def default_research_checkpointer(
    *,
    backend: str | None = None,
    path: str | Path | None = None,
):
    """生产默认 SQLite；测试可显式传入 InMemorySaver。"""
    chosen = (
        backend
        or os.getenv("HARNESS_GRAPH_CHECKPOINT_BACKEND")
        or "sqlite"
    ).strip().lower()
    if chosen in {"memory", "inmemory", "mem", "none"}:
        return memory_checkpointer()
    try:
        return sqlite_checkpointer(path)
    except Exception as exc:
        logger.warning("sqlite checkpointer unavailable (%s); falling back to InMemorySaver", exc)
        return memory_checkpointer()


def sqlite_checkpointer(path: str | Path | None = None):
    global _SQLITE_SAVER, _SQLITE_CONN, _SQLITE_PATH
    from langgraph.checkpoint.sqlite import SqliteSaver

    resolved = str(
        path
        or os.getenv("HARNESS_GRAPH_CHECKPOINT")
        or DEFAULT_CHECKPOINT_PATH
    )
    if _SQLITE_SAVER is not None and _SQLITE_PATH == resolved and _SQLITE_CONN is not None:
        return _SQLITE_SAVER
    target = Path(resolved)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    saver = SqliteSaver(conn)
    if hasattr(saver, "setup"):
        saver.setup()
    _SQLITE_CONN = conn
    _SQLITE_SAVER = saver
    _SQLITE_PATH = resolved
    return saver


def reset_checkpointer_cache() -> None:
    global _SQLITE_SAVER, _SQLITE_CONN, _SQLITE_PATH
    if _SQLITE_CONN is not None:
        try:
            _SQLITE_CONN.close()
        except Exception:
            pass
    _SQLITE_SAVER = None
    _SQLITE_CONN = None
    _SQLITE_PATH = None
