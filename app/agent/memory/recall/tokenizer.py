"""
中英混合词法切分 — Hybrid Recall 的 keyword 通道。

``str.split()`` 对「人形机器人市场规模」只会得到一个 token，导致
``recall_keyword_weight`` 在中文查询下几乎失效。这里对 CJK 连续串
使用字 unigram + bigram，对拉丁片段仍按空白/标点切分。
"""

from __future__ import annotations

import re

_SPLIT = re.compile(r"[\s,，。．.；;：:、！!？?（）()\[\]【】\"'“”‘’/\\|]+")
_CJK_CHAR = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")


def _is_cjk_char(ch: str) -> bool:
    return bool(_CJK_CHAR.match(ch))


def lexical_tokens(text: str) -> list[str]:
    """返回用于 keyword / BM25 粗召回的 token 列表（可重复）。"""
    raw = (text or "").strip().lower()
    if not raw:
        return []
    tokens: list[str] = []
    for part in _SPLIT.split(raw):
        if not part:
            continue
        if any(_is_cjk_char(ch) for ch in part):
            chars = [ch for ch in part if not ch.isspace()]
            tokens.extend(chars)
            tokens.extend("".join(chars[i : i + 2]) for i in range(len(chars) - 1))
            if 2 <= len(part) <= 12:
                tokens.append(part)
        else:
            if len(part) >= 2:
                tokens.append(part)
            elif part.isalnum():
                tokens.append(part)
    return tokens


def lexical_token_set(text: str) -> set[str]:
    return {t for t in lexical_tokens(text) if t}


def keyword_overlap(query: str, fact: str) -> float:
    """Query token 覆盖率：命中数 / 查询 unique token 数。"""
    q = lexical_token_set(query)
    if not q:
        return 0.0
    f = lexical_token_set(fact)
    if not f:
        return 0.0
    return len(q & f) / len(q)
