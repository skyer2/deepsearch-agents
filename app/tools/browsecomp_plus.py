"""BrowseComp-Plus 固定语料检索适配。

使用 SQLite FTS5 保存约 10 万篇官方语料，避免 Benchmark 期间访问实时网络。
生产工具与评测运行器共用本模块，保证检索口径一致。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")


@dataclass(frozen=True)
class BrowseCompDocument:
    docid: str
    text: str
    url: str = ""


def create_corpus_database(
    documents: Iterable[BrowseCompDocument],
    output_path: Path,
    *,
    batch_size: int = 500,
) -> int:
    """流式创建 FTS5 索引；返回写入文档数。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".building")
    if temporary_path.exists():
        temporary_path.unlink()
    connection = sqlite3.connect(temporary_path)
    count = 0
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE corpus USING fts5("
            "docid UNINDEXED, url UNINDEXED, text, tokenize='unicode61')"
        )
        batch: list[tuple[str, str, str]] = []
        for document in documents:
            batch.append((document.docid, document.url, document.text))
            if len(batch) >= batch_size:
                connection.executemany(
                    "INSERT INTO corpus(docid, url, text) VALUES (?, ?, ?)",
                    batch,
                )
                count += len(batch)
                batch.clear()
        if batch:
            connection.executemany(
                "INSERT INTO corpus(docid, url, text) VALUES (?, ?, ?)",
                batch,
            )
            count += len(batch)
        connection.commit()
    finally:
        connection.close()
    temporary_path.replace(output_path)
    return count


def _fts_query(query: str) -> str:
    terms = TOKEN_RE.findall(query.lower())
    unique_terms = list(dict.fromkeys(terms))[:32]
    if not unique_terms:
        raise ValueError("BrowseComp-Plus search query contains no searchable terms")
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in unique_terms)


class BrowseCompPlusRetriever:
    """线程安全的只读 SQLite FTS5 Retriever。"""

    def __init__(self, database_path: str | Path, *, max_context_chars: int = 2048):
        self.database_path = Path(database_path).resolve()
        if not self.database_path.is_file():
            raise FileNotFoundError(
                f"BrowseComp-Plus corpus database not found: {self.database_path}"
            )
        self.max_context_chars = max(256, int(max_context_chars))
        self._local = threading.local()

    def _connection(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(
                f"file:{self.database_path.as_posix()}?mode=ro",
                uri=True,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            self._local.connection = connection
        return connection

    def search(self, query: str, *, top_k: int = 5) -> list[dict[str, Any]]:
        rows = self._connection().execute(
            "SELECT docid, url, "
            "snippet(corpus, 2, '', '', ' … ', 80) AS snippet, "
            "bm25(corpus) AS rank "
            "FROM corpus WHERE corpus MATCH ? ORDER BY rank LIMIT ?",
            (_fts_query(query), max(1, int(top_k))),
        ).fetchall()
        return [
            {
                "docid": str(row["docid"]),
                "url": str(row["url"] or f"browsecomp://doc/{row['docid']}"),
                "text": str(row["snippet"] or "")[: self.max_context_chars],
                # SQLite FTS5 的 bm25 排名通常为负值，绝对值越大越相关。
                "score": round(-float(row["rank"]), 6),
            }
            for row in rows
        ]


def append_retrieval_log(path: str | Path, query: str, results: list[dict[str, Any]]) -> None:
    """追加一次工具检索结果，供 live runner 汇总 agent 全轨迹 docid。"""
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        # Benchmark 查询可能属于受保护明文，日志仅保留不可逆指纹。
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "docids": [str(item["docid"]) for item in results],
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def load_qrels(path: Path) -> dict[str, dict[str, int]]:
    qrels: dict[str, dict[str, int]] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) == 4:
            query_id, _iteration, docid, relevance = parts
        elif len(parts) == 3:
            query_id, docid, relevance = parts
        else:
            raise ValueError(f"Invalid qrels line: {raw_line}")
        qrels.setdefault(query_id, {})[docid] = int(relevance)
    return qrels


def retrieval_metrics(
    rankings: dict[str, list[str]],
    qrels: dict[str, dict[str, int]],
    *,
    recall_ks: tuple[int, ...] = (5, 100, 1000),
    ndcg_k: int = 10,
) -> dict[str, float]:
    """计算 macro Recall@k 与 nDCG@k，口径与 TREC 常用指标一致。"""
    if not qrels:
        return {}
    sums = {f"recall@{k}": 0.0 for k in recall_ks}
    ndcg_sum = 0.0
    evaluated = 0
    for query_id, judgments in qrels.items():
        relevant = {docid for docid, relevance in judgments.items() if relevance > 0}
        if not relevant:
            continue
        ranked = rankings.get(query_id, [])
        evaluated += 1
        for k in recall_ks:
            sums[f"recall@{k}"] += len(relevant.intersection(ranked[:k])) / len(relevant)

        dcg = sum(
            (2 ** judgments.get(docid, 0) - 1) / math.log2(rank + 2)
            for rank, docid in enumerate(ranked[:ndcg_k])
        )
        ideal_relevances = sorted(judgments.values(), reverse=True)[:ndcg_k]
        ideal_dcg = sum(
            (2**relevance - 1) / math.log2(rank + 2)
            for rank, relevance in enumerate(ideal_relevances)
        )
        ndcg_sum += dcg / ideal_dcg if ideal_dcg else 0.0

    if not evaluated:
        return {}
    return {
        **{name: round(value / evaluated, 6) for name, value in sums.items()},
        f"ndcg@{ndcg_k}": round(ndcg_sum / evaluated, 6),
        "evaluated_queries": float(evaluated),
    }
