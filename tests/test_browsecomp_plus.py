"""BrowseComp-Plus 适配离线测试（仅使用微型固定语料）。"""

from pathlib import Path

from app.tools.browsecomp_plus import (
    BrowseCompDocument,
    BrowseCompPlusRetriever,
    create_corpus_database,
    retrieval_metrics,
)
from tests.eval.run_browsecomp_plus import (
    answer_surrogate_metrics,
    citation_metrics,
    extract_cited_docids,
)


def test_sqlite_fixed_corpus_retrieval(tmp_path: Path):
    database = tmp_path / "corpus.sqlite3"
    count = create_corpus_database(
        [
            BrowseCompDocument(
                docid="gold-1",
                text="Ada Lovelace wrote notes on the Analytical Engine.",
                url="https://example.test/ada",
            ),
            BrowseCompDocument(
                docid="negative-1",
                text="Charles Babbage designed mechanical computing machines.",
            ),
        ],
        database,
    )
    assert count == 2

    results = BrowseCompPlusRetriever(database).search(
        "Who wrote notes about the Analytical Engine?",
        top_k=2,
    )
    assert results[0]["docid"] == "gold-1"
    assert results[0]["url"] == "https://example.test/ada"


def test_retrieval_metrics():
    rankings = {"q1": ["negative", "gold-a", "gold-b"]}
    qrels = {"q1": {"gold-a": 1, "gold-b": 1}}
    metrics = retrieval_metrics(
        rankings,
        qrels,
        recall_ks=(1, 3),
        ndcg_k=3,
    )
    assert metrics["recall@1"] == 0.0
    assert metrics["recall@3"] == 1.0
    assert 0.0 < metrics["ndcg@3"] < 1.0


def test_official_docid_citations_ignore_harness_sequence_numbers():
    output = "结论来自 [docid:18639] 和【41759】，本地参考号 [1]。"
    cited = extract_cited_docids(output, {"18639", "41759", "99999"})
    assert cited == ["18639", "41759"]
    metrics = citation_metrics(cited, {"18639", "99999"})
    assert metrics["citation_precision"] == 0.5
    assert metrics["citation_recall"] == 0.5


def test_answer_metrics_are_explicit_surrogates():
    metrics = answer_surrogate_metrics(
        "Explanation: evidence [18639]\n"
        "Exact Answer: Ada Lovelace\n"
        "Confidence: 90%",
        "Ada Lovelace",
    )
    assert metrics["answer_normalized_em"] == 1.0
    assert metrics["answer_string_recall"] == 1.0
    assert 0.0 < metrics["answer_token_f1"] <= 1.0
    assert metrics["answer_parse_rate"] == 1.0
    assert metrics["confidence"] == 0.9
