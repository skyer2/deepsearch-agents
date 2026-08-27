"""
真正的 Memory IR 指标。

运行时 ``mean_recall_score`` 只是召回候选的平均打分，不是 Recall@K。
本模块在有 labeled relevant set 时计算 Recall@K / MRR / nDCG / Precision@K。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass
class RetrievalMetrics:
    recall_at_k: float
    precision_at_k: float
    mrr: float
    ndcg_at_k: float
    k: int
    retrieved: int
    relevant: int

    def to_dict(self) -> dict[str, float | int]:
        return {
            "recall_at_k": round(self.recall_at_k, 4),
            "precision_at_k": round(self.precision_at_k, 4),
            "mrr": round(self.mrr, 4),
            "ndcg_at_k": round(self.ndcg_at_k, 4),
            "k": self.k,
            "retrieved": self.retrieved,
            "relevant": self.relevant,
        }


def _ids(items: Iterable[str]) -> list[str]:
    return [str(item) for item in items if str(item)]


def _dcg(relevances: Sequence[float]) -> float:
    score = 0.0
    for idx, rel in enumerate(relevances, start=1):
        score += rel / math.log2(idx + 1)
    return score


def evaluate_retrieval(
    retrieved_ids: Sequence[str],
    relevant_ids: Sequence[str],
    *,
    k: int | None = None,
) -> RetrievalMetrics:
    ranked = _ids(retrieved_ids)
    gold = set(_ids(relevant_ids))
    cutoff = k if k is not None else len(ranked)
    cutoff = max(1, cutoff)
    top = ranked[:cutoff]
    hits = [item for item in top if item in gold]
    relevant_n = len(gold)
    recall = (len(hits) / relevant_n) if relevant_n else 0.0
    precision = (len(hits) / len(top)) if top else 0.0

    mrr = 0.0
    for idx, item in enumerate(top, start=1):
        if item in gold:
            mrr = 1.0 / idx
            break

    rels = [1.0 if item in gold else 0.0 for item in top]
    ideal = sorted(rels, reverse=True)
    dcg = _dcg(rels)
    idcg = _dcg(ideal)
    ndcg = (dcg / idcg) if idcg else 0.0

    return RetrievalMetrics(
        recall_at_k=recall,
        precision_at_k=precision,
        mrr=mrr,
        ndcg_at_k=ndcg,
        k=cutoff,
        retrieved=len(top),
        relevant=relevant_n,
    )
