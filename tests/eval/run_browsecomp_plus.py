"""BrowseComp-Plus retrieval-only / Agent live 评测运行器。

输出既包含本项目成本、延迟、引用指标，也生成官方 evaluate_run.py
可直接消费的每 query JSON 文件。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.tools.browsecomp_plus import BrowseCompPlusRetriever, load_qrels, retrieval_metrics


def load_queries(path: Path, limit: int = 0) -> list[dict[str, str]]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return rows[:limit] if limit > 0 else rows


def _read_log_since(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], offset
    with path.open("rb") as handle:
        handle.seek(offset)
        records = [
            json.loads(line.decode("utf-8"))
            for line in handle
            if line.strip()
        ]
        return records, handle.tell()


def _unique_docids(records: list[dict[str, Any]]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for record in records:
        for docid in record.get("docids") or []:
            value = str(docid)
            if value not in seen:
                seen.add(value)
                ordered.append(value)
    return ordered


def _normalized(text: str) -> str:
    return re.sub(r"\W+", "", text, flags=re.UNICODE).casefold()


def answer_string_recall(output: str, gold_answer: str) -> float:
    """低成本 sanity metric；不能替代官方 Qwen3-32B Answer Accuracy judge。"""
    gold = _normalized(gold_answer)
    actual = _normalized(output)
    return float(bool(gold) and gold in actual)


def answer_surrogate_metrics(
    output: str,
    gold_answer: str,
) -> dict[str, float | None]:
    """明确标记为 offline surrogate 的 EM/F1，不冒充官方 Accuracy。"""
    answer_match = re.search(
        r"(?im)^\s*Exact Answer\s*:\s*(.+?)\s*$",
        output,
    )
    parsed_answer = answer_match.group(1).strip() if answer_match else output.strip()
    confidence_match = re.search(
        r"(?im)^\s*Confidence\s*:\s*(\d+(?:\.\d+)?)\s*%?\s*$",
        output,
    )
    confidence = (
        min(100.0, max(0.0, float(confidence_match.group(1)))) / 100.0
        if confidence_match
        else None
    )
    actual = _normalized(parsed_answer)
    gold = _normalized(gold_answer)
    actual_tokens = re.findall(
        r"[a-z0-9]+|[\u4e00-\u9fff]",
        parsed_answer.casefold(),
    )
    gold_tokens = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", gold_answer.casefold())
    actual_counts: dict[str, int] = {}
    gold_counts: dict[str, int] = {}
    for token in actual_tokens:
        actual_counts[token] = actual_counts.get(token, 0) + 1
    for token in gold_tokens:
        gold_counts[token] = gold_counts.get(token, 0) + 1
    overlap = sum(
        min(count, gold_counts.get(token, 0))
        for token, count in actual_counts.items()
    )
    precision = overlap / len(actual_tokens) if actual_tokens else 0.0
    recall = overlap / len(gold_tokens) if gold_tokens else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return {
        "answer_normalized_em": float(bool(gold) and actual == gold),
        "answer_token_f1": round(f1, 6),
        "answer_parse_rate": float(answer_match is not None),
        "answer_string_recall": float(bool(gold) and gold in actual),
        "confidence": confidence,
    }


def extract_cited_docids(output: str, allowed_docids: set[str]) -> list[str]:
    """提取官方数字 docid 引用，并过滤 Harness 自动生成的非语料顺序号。"""
    candidates: list[str] = []
    bracket_contents = re.findall(r"\[([^\]]+)\]|【([^】]+)】", output)
    for square, chinese in bracket_contents:
        content = square or chinese
        content = re.sub(r"(?i)docid\s*:", "", content)
        candidates.extend(re.findall(r"\d+", content))
    return list(
        dict.fromkeys(docid for docid in candidates if docid in allowed_docids)
    )


def citation_metrics(
    cited_docids: list[str],
    evidence_docids: set[str],
) -> dict[str, float]:
    cited = set(cited_docids)
    correct = cited.intersection(evidence_docids)
    return {
        "citation_precision": (
            round(len(correct) / len(cited), 6) if cited else 0.0
        ),
        "citation_recall": (
            round(len(correct) / len(evidence_docids), 6)
            if evidence_docids
            else 0.0
        ),
        "has_valid_docid_citation": float(bool(cited)),
        "citation_count": float(len(cited)),
    }


def write_trec_run(
    rankings: dict[str, list[str]],
    path: Path,
    *,
    tag: str,
) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for query_id, docids in rankings.items():
            for rank, docid in enumerate(docids, start=1):
                handle.write(
                    f"{query_id} Q0 {docid} {rank} {1.0 / rank:.8f} {tag}\n"
                )


def run_retrieval_only(
    queries: list[dict[str, str]],
    retriever: BrowseCompPlusRetriever,
    *,
    top_k: int,
) -> dict[str, list[str]]:
    rankings: dict[str, list[str]] = {}
    for index, item in enumerate(queries, start=1):
        results = retriever.search(item["query"], top_k=top_k)
        rankings[item["query_id"]] = [result["docid"] for result in results]
        print(f"[{index}/{len(queries)}] retrieved {len(results)} docs")
    return rankings


async def run_live(
    queries: list[dict[str, str]],
    output_dir: Path,
    retrieval_log: Path,
    evidence_qrels: dict[str, dict[str, int]],
) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    # 必须在导入 main_agent（以及常驻 MCP Server）之前设置。
    os.environ["BROWSECOMP_PLUS_ENABLED"] = "true"
    os.environ["BROWSECOMP_PLUS_RETRIEVAL_LOG"] = str(retrieval_log.resolve())
    from app.agent.main_agent import harness

    official_runs = output_dir / "official_runs"
    official_runs.mkdir(parents=True, exist_ok=True)
    rankings: dict[str, list[str]] = {}
    summaries: list[dict[str, Any]] = []
    log_offset = retrieval_log.stat().st_size if retrieval_log.exists() else 0

    for index, item in enumerate(queries, start=1):
        query_id = item["query_id"]
        session_id = f"browsecomp_{query_id}_{uuid.uuid4().hex[:8]}"
        benchmark_query = (
            "这是固定语料深度检索评测。请调用 network_search_agent，"
            "只依据搜索工具返回的文档回答。引用必须使用搜索结果的真实数字 "
            "docid，格式为 [docid]，禁止使用自增引用序号。"
            "严格按三段输出：Explanation: 带引用的解释；"
            "Exact Answer: 简洁答案；Confidence: 0-100%。\n"
            f"问题：{item['query']}"
        )
        started = time.perf_counter()
        result = await harness.run(benchmark_query, session_id)
        latency_ms = int((time.perf_counter() - started) * 1000)
        log_records, log_offset = _read_log_since(retrieval_log, log_offset)
        retrieved_docids = _unique_docids(log_records)
        rankings[query_id] = retrieved_docids
        usage = result.metadata.get("usage") or {}
        cited_docids = extract_cited_docids(
            result.content,
            set(retrieved_docids),
        )
        evidence_docids = {
            docid
            for docid, relevance in evidence_qrels.get(query_id, {}).items()
            if relevance > 0
        }
        summary = {
            "query_id": query_id,
            "status": result.status,
            "latency_ms": latency_ms,
            "retrieved_docids": retrieved_docids,
            "search_calls": len(log_records),
            "offline_surrogate": answer_surrogate_metrics(
                result.content, item.get("answer", "")
            ),
            "cited_docids": cited_docids,
            "browsecomp_citations": citation_metrics(
                cited_docids,
                evidence_docids,
            ),
            "citation_coverage_rate": result.metadata.get(
                "citation_coverage_rate"
            ),
            "hallucination_rate": result.metadata.get("hallucination_rate"),
            "usage": usage,
        }
        summaries.append(summary)

        official_payload = {
            "query_id": query_id,
            "tool_call_counts": {"search": len(log_records)},
            "status": "completed" if result.status == "success" else result.status,
            "retrieved_docids": retrieved_docids,
            "result": [{"type": "output_text", "output": result.content}],
        }
        (official_runs / f"{query_id}.json").write_text(
            json.dumps(official_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            f"[{index}/{len(queries)}] {query_id}: {result.status}, "
            f"search={len(log_records)}, docs={len(retrieved_docids)}, "
            f"latency={latency_ms}ms"
        )
    return rankings, summaries


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("retrieval", "live"), default="retrieval")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=ROOT / ".benchmark_data" / "browsecomp_plus",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "tests" / "eval" / "results" / "browsecomp_plus",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--top-k", type=int, default=1000)
    args = parser.parse_args()

    queries = load_queries(args.data_dir / "queries.jsonl", args.limit)
    database_path = args.data_dir / "corpus.sqlite3"
    os.environ["BROWSECOMP_PLUS_CORPUS_DB"] = str(database_path.resolve())
    args.output_dir.mkdir(parents=True, exist_ok=True)
    retriever = BrowseCompPlusRetriever(database_path)
    gold_qrels = load_qrels(args.data_dir / "qrels_gold.txt")
    evidence_qrels = load_qrels(args.data_dir / "qrels_evidence.txt")

    summaries: list[dict[str, Any]] = []
    if args.mode == "retrieval":
        rankings = run_retrieval_only(queries, retriever, top_k=args.top_k)
    else:
        rankings, summaries = asyncio.run(
            run_live(
                queries,
                args.output_dir,
                args.output_dir / "retrieval_log.jsonl",
                evidence_qrels,
            )
        )

    evaluated_ids = set(rankings)
    gold_qrels = {key: value for key, value in gold_qrels.items() if key in evaluated_ids}
    evidence_qrels = {
        key: value for key, value in evidence_qrels.items() if key in evaluated_ids
    }
    calibrated = [
        item["offline_surrogate"]
        for item in summaries
        if item["offline_surrogate"].get("confidence") is not None
    ]
    report = {
        "benchmark": "BrowseComp-Plus",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "queries": len(queries),
        "manifest": json.loads(
            (args.data_dir / "manifest.json").read_text(encoding="utf-8")
        ),
        "configuration": {
            "fixed_corpus": True,
            "top_k": args.top_k if args.mode == "retrieval" else 5,
            "corpus_database": str(database_path.resolve()),
        },
        "gold": retrieval_metrics(rankings, gold_qrels),
        "evidence": retrieval_metrics(rankings, evidence_qrels),
        "agent": summaries,
        "agent_aggregate": (
            {
                "success_rate": round(
                    sum(item["status"] == "success" for item in summaries)
                    / len(summaries),
                    6,
                ),
                "average_latency_ms": round(
                    sum(item["latency_ms"] for item in summaries) / len(summaries),
                    2,
                ),
                "average_cost_usd": round(
                    sum(
                        float(
                            ((item.get("usage") or {}).get("total") or {}).get(
                                "cost_usd", 0
                            )
                        )
                        for item in summaries
                    )
                    / len(summaries),
                    6,
                ),
                "missing_usage_calls": sum(
                    int(
                        ((item.get("usage") or {}).get("total") or {}).get(
                            "missing_usage_calls", 0
                        )
                    )
                    for item in summaries
                ),
                "offline_normalized_em": round(
                    sum(
                        item["offline_surrogate"]["answer_normalized_em"]
                        for item in summaries
                    )
                    / len(summaries),
                    6,
                ),
                "offline_token_f1": round(
                    sum(
                        item["offline_surrogate"]["answer_token_f1"]
                        for item in summaries
                    )
                    / len(summaries),
                    6,
                ),
                "offline_answer_parse_rate": round(
                    sum(
                        item["offline_surrogate"]["answer_parse_rate"]
                        for item in summaries
                    )
                    / len(summaries),
                    6,
                ),
                "offline_calibration_error": (
                    round(
                        sum(
                            abs(
                                float(item["confidence"])
                                - float(item["answer_normalized_em"])
                            )
                            for item in calibrated
                        )
                        / len(calibrated),
                        6,
                    )
                    if calibrated
                    else None
                ),
                "browsecomp_citation_precision": round(
                    sum(
                        item["browsecomp_citations"]["citation_precision"]
                        for item in summaries
                    )
                    / len(summaries),
                    6,
                ),
                "browsecomp_citation_recall": round(
                    sum(
                        item["browsecomp_citations"]["citation_recall"]
                        for item in summaries
                    )
                    / len(summaries),
                    6,
                ),
                "cited_ratio": round(
                    sum(
                        item["browsecomp_citations"][
                            "has_valid_docid_citation"
                        ]
                        for item in summaries
                    )
                    / len(summaries),
                    6,
                ),
            }
            if summaries
            else {}
        ),
        "note": (
            "offline_surrogate metrics are local sanity checks; run the official "
            "Qwen3-32B judge for Answer Accuracy. SQLite FTS5 is a custom portable "
            "retriever, not the official Lucene BM25 reproduction."
        ),
    }
    report_path = args.output_dir / f"{args.mode}_summary.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_trec_run(rankings, args.output_dir / f"{args.mode}.trec", tag=args.mode)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Saved: {report_path}")


if __name__ == "__main__":
    main()
