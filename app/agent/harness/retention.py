"""
压缩后关键锚点保留检查：URL 与数字必须能从摘要中找回，否则打补丁。
"""

from __future__ import annotations

import re
from typing import Iterable

from app.agent.harness.citations import URL_PATTERN

NUMBER_PATTERN = re.compile(
    r"(?<![\w./])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?(?![\w./])"
)


def _normalize_url(url: str) -> str:
    return url.rstrip(".,;)]}>\"'").lower()


def extract_urls(text: str, limit: int = 12) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in URL_PATTERN.findall(text or ""):
        url = raw.rstrip(".,;)]}>\"'")
        key = _normalize_url(url)
        if key and key not in seen:
            seen.add(key)
            out.append(url)
        if len(out) >= limit:
            break
    return out


def extract_numbers(text: str, limit: int = 20) -> list[str]:
    """抽取值得保留的数字（跳过孤立个位数，保留百分数与小数）。"""
    seen: set[str] = set()
    out: list[str] = []
    for match in NUMBER_PATTERN.findall(text or ""):
        token = match.strip()
        if not token:
            continue
        digits = token.replace(",", "").replace("%", "")
        if digits.isdigit() and len(digits) == 1:
            continue
        if token not in seen:
            seen.add(token)
            out.append(token)
        if len(out) >= limit:
            break
    return out


def _present(token: str, haystack: str) -> bool:
    return token.lower() in haystack.lower() if token else False


def retention_stats(original: str, compressed: str) -> dict[str, float | int | list[str]]:
    urls = extract_urls(original)
    numbers = extract_numbers(original)
    missing_urls = [u for u in urls if not _present(u, compressed)]
    missing_numbers = [n for n in numbers if not _present(n, compressed)]
    url_rate = 1.0 if not urls else round((len(urls) - len(missing_urls)) / len(urls), 3)
    number_rate = (
        1.0 if not numbers else round((len(numbers) - len(missing_numbers)) / len(numbers), 3)
    )
    units = len(urls) + len(numbers)
    kept = (len(urls) - len(missing_urls)) + (len(numbers) - len(missing_numbers))
    overall = 1.0 if units == 0 else round(kept / units, 3)
    return {
        "url_count": len(urls),
        "number_count": len(numbers),
        "url_retention": url_rate,
        "number_retention": number_rate,
        "entity_retention": overall,
        "missing_urls": missing_urls,
        "missing_numbers": missing_numbers,
    }


def apply_retention_patch(
    original: str,
    compressed: str,
    *,
    min_url_retention: float = 0.8,
    min_number_retention: float = 0.5,
) -> tuple[str, dict]:
    """若摘要丢掉过多 URL/数字，把缺失锚点前置补回。"""
    stats = retention_stats(original, compressed)
    missing_urls: list[str] = list(stats["missing_urls"])  # type: ignore[arg-type]
    missing_numbers: list[str] = list(stats["missing_numbers"])  # type: ignore[arg-type]
    url_ok = float(stats["url_retention"]) >= min_url_retention
    num_ok = float(stats["number_retention"]) >= min_number_retention
    meta = {
        **{k: v for k, v in stats.items() if k not in {"missing_urls", "missing_numbers"}},
        "retention_patched": False,
        "retention_passed": bool(url_ok and num_ok),
    }
    if url_ok and num_ok:
        return compressed, meta

    lines = ["【压缩保留补丁 — 摘要可能丢失原文锚点，写报告须优先采信以下条目】"]
    if missing_urls:
        lines.append("URL: " + " | ".join(missing_urls[:8]))
    if missing_numbers:
        lines.append("数字: " + "、".join(missing_numbers[:12]))
    lines.append("【摘要】")
    lines.append(compressed)
    meta["retention_patched"] = True
    meta["retention_passed"] = False
    patched = "\n".join(lines)
    # 补丁后重新计算
    after = retention_stats(original, patched)
    meta["entity_retention_after_patch"] = after["entity_retention"]
    return patched, meta


def format_missing(items: Iterable[str], limit: int = 8) -> str:
    values = [str(x) for x in items][:limit]
    return " | ".join(values)
