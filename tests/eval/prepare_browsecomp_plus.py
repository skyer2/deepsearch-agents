"""准备 BrowseComp-Plus 固定 50 条子集与 SQLite FTS5 语料索引。

先按官方仓库 README 解密 query 数据，再运行本脚本。解密后的问题/答案以及
3GB 语料均只保存在被 gitignore 的本地目录，避免 benchmark 明文泄露。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.tools.browsecomp_plus import BrowseCompDocument, create_corpus_database

CORPUS_REVISION = "b27b02bc3e45511b8b82a13e6f90ce761df726f6"
QUERY_REVISION = "144cff8e35b5eaef7e526346aa60774a9deb941f"
UPSTREAM_GIT_COMMIT = "046949032b0328319cc9a02663a759ec601d9402"


def _read_jsonl(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def select_fixed_subset(
    records: Iterable[dict],
    *,
    limit: int,
    seed: str,
) -> list[dict]:
    """按 query_id 稳定散列抽样，不受源文件顺序变化影响。"""
    scored = []
    for record in records:
        query_id = str(record.get("query_id") or "")
        if not query_id:
            continue
        digest = hashlib.sha256(f"{seed}:{query_id}".encode("utf-8")).hexdigest()
        # 不在内存中保留每题数 MB 的正文，只保留评测所需字段。
        compact = {
            "query_id": query_id,
            "query": record.get("query") or "",
            "answer": record.get("answer") or "",
            "gold_docs": [
                {"docid": str(item["docid"])}
                for item in (record.get("gold_docs") or [])
            ],
            "evidence_docs": [
                {"docid": str(item["docid"])}
                for item in (record.get("evidence_docs") or [])
            ],
        }
        scored.append((digest, compact))
    return [record for _digest, record in sorted(scored)[:limit]]


def write_subset(records: list[dict], output_dir: Path, seed: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    query_path = output_dir / "queries.jsonl"
    gold_path = output_dir / "qrels_gold.txt"
    evidence_path = output_dir / "qrels_evidence.txt"

    with (
        query_path.open("w", encoding="utf-8") as queries,
        gold_path.open("w", encoding="utf-8") as gold,
        evidence_path.open("w", encoding="utf-8") as evidence,
    ):
        for record in records:
            query_id = str(record["query_id"])
            queries.write(
                json.dumps(
                    {
                        "query_id": query_id,
                        "query": str(record.get("query") or ""),
                        "answer": str(record.get("answer") or ""),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            for document in record.get("gold_docs") or []:
                gold.write(f"{query_id} 0 {document['docid']} 1\n")
            for document in record.get("evidence_docs") or []:
                evidence.write(f"{query_id} 0 {document['docid']} 1\n")

    manifest = {
        "schema_version": 1,
        "benchmark": "BrowseComp-Plus",
        "upstream_git_commit": UPSTREAM_GIT_COMMIT,
        "query_dataset": "Tevatron/browsecomp-plus",
        "expected_query_dataset_revision": QUERY_REVISION,
        "corpus_dataset": "Tevatron/browsecomp-plus-corpus",
        "corpus_dataset_revision": CORPUS_REVISION,
        "seed": seed,
        "size": len(records),
        "query_ids": [str(record["query_id"]) for record in records],
        "selection": "sha256(seed:query_id), ascending",
        "retrieval": {
            "backend": "sqlite-fts5-portable-custom",
            "trec_depth": 1000,
            "agent_top_k": 5,
            "snippet_chars": 2048,
            "official_bm25_reproduction": False,
        },
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _corpus_documents_from_jsonl(path: Path) -> Iterable[BrowseCompDocument]:
    for record in _read_jsonl(path):
        yield BrowseCompDocument(
            docid=str(record["docid"]),
            text=str(record.get("text") or ""),
            url=str(record.get("url") or ""),
        )


def _corpus_documents_from_huggingface() -> Iterable[BrowseCompDocument]:
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise RuntimeError(
            "Preparing the official corpus requires: pip install datasets"
        ) from exc
    dataset = load_dataset(
        "Tevatron/browsecomp-plus-corpus",
        split="train",
        streaming=True,
        revision=CORPUS_REVISION,
    )
    for record in dataset:
        yield BrowseCompDocument(
            docid=str(record["docid"]),
            text=str(record.get("text") or ""),
            url=str(record.get("url") or ""),
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--decrypted", type=Path, required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / ".benchmark_data" / "browsecomp_plus",
    )
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--seed", default="deepsearch-agents-v1")
    parser.add_argument(
        "--corpus-jsonl",
        type=Path,
        help="可选：使用已下载的 corpus JSONL；默认从 Hugging Face 流式下载",
    )
    parser.add_argument(
        "--skip-corpus",
        action="store_true",
        help="仅生成固定 query/qrels 子集",
    )
    args = parser.parse_args()

    selected = select_fixed_subset(
        _read_jsonl(args.decrypted),
        limit=max(1, args.limit),
        seed=args.seed,
    )
    write_subset(selected, args.output_dir, args.seed)

    if not args.skip_corpus:
        documents = (
            _corpus_documents_from_jsonl(args.corpus_jsonl)
            if args.corpus_jsonl
            else _corpus_documents_from_huggingface()
        )
        count = create_corpus_database(
            documents,
            args.output_dir / "corpus.sqlite3",
        )
        print(f"Indexed {count} corpus documents")
    print(f"Prepared {len(selected)} fixed queries in {args.output_dir}")


if __name__ == "__main__":
    main()
