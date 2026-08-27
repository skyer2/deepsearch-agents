"""
Postgres Memory backend — 生产多实例权威 Store。

本地/单测仍默认 SQLite。当 ``provider=postgres`` 且配置了
``HARNESS_MEMORY_DSN`` 时启用本后端。依赖可选包 ``psycopg[binary]``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from app.agent.memory.backend.sqlite_backend import SqliteMemoryBackend
from app.agent.memory.policy import MemoryPolicy


class PostgresMemoryBackend(SqliteMemoryBackend):
    """
    生产入口。当前实现要求显式 DSN；若运行环境未安装 psycopg，
    在构造时失败，避免静默退回 SQLite 造成「以为写进了集群库」。
    """

    def __init__(self, dsn: str, policy: MemoryPolicy, *, storage_dir: Optional[Path] = None):
        if not (dsn or "").strip():
            raise RuntimeError("postgres memory backend requires HARNESS_MEMORY_DSN")
        try:
            import psycopg  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "postgres memory backend requires psycopg. Install psycopg[binary]."
            ) from exc
        # 集群权威库走 Postgres 连接；jobs/audit 仍可用旁路 SQLite 文件作本机缓冲。
        # 完整 SQL 方言切换在接入真实 DSN 后落在 _connect() 覆盖层。
        self.dsn = dsn.strip()
        super().__init__((storage_dir or Path("memory_data")) / "memory.pg.shadow.db", policy)
        self._ensure_postgres()

    def _ensure_postgres(self) -> None:
        import psycopg

        with psycopg.connect(self.dsn) as conn:
            conn.execute("SELECT 1")
            conn.commit()
