"""
记忆时效 — valid_time / as_of / 按类型 TTL。

写入时间 ≠ 事实发生时间。冲突检测必须先对齐 valid_time，TTL 也必须
按 memory type 和波动性区分，而不是全局 90 天一刀切。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.agent.memory.models import MemoryRecord, MemoryType

_YEAR = re.compile(r"(?:19|20)\d{2}")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?%?")

# 默认按类型生命周期；0 = 直到用户改/删，不因 TTL 过期
DEFAULT_TTL_BY_TYPE: dict[str, int] = {
    MemoryType.PREFERENCE.value: 0,
    MemoryType.PROCEDURAL.value: 0,
    MemoryType.SEMANTIC.value: 90,
    MemoryType.EPISODIC.value: 14,
    MemoryType.SOURCE.value: 180,
}

VOLATILE_MARKERS = (
    "营收",
    "产量",
    "股价",
    "市值",
    "guidance",
    "预计",
    "市场份额",
    "增速",
    "价格",
    "产能",
    "库存",
    "revenue",
    "guidance",
    "market share",
    "stock",
)


@dataclass
class FactFrame:
    """结构化 fact 槽位，供冲突检测使用。"""

    entity: str = ""
    attribute: str = ""
    value: str = ""
    unit: str = ""
    valid_time: str = ""
    tokens: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "attribute": self.attribute,
            "value": self.value,
            "unit": self.unit,
            "valid_time": self.valid_time,
            "tokens": list(self.tokens),
        }

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "FactFrame":
        if not data:
            return cls()
        return cls(
            entity=str(data.get("entity") or ""),
            attribute=str(data.get("attribute") or ""),
            value=str(data.get("value") or ""),
            unit=str(data.get("unit") or ""),
            valid_time=str(data.get("valid_time") or ""),
            tokens=[str(t) for t in (data.get("tokens") or [])],
        )


def extract_fact_frame(text: str) -> FactFrame:
    """启发式抽取 entity / value / valid_time。不做 NER，只服务冲突对齐。"""
    raw = (text or "").strip()
    years = _YEAR.findall(raw)
    nums = [n for n in _NUMBER.findall(raw) if not _YEAR.fullmatch(n)]
    valid_time = years[0] if years else ""
    value = nums[0] if nums else ""
    unit = ""
    if "%" in raw and value and not str(value).endswith("%"):
        unit = "%"
    elif "亿" in raw:
        unit = "亿"
    elif "万" in raw:
        unit = "万"
    compact = re.sub(r"[\s,，。；;：:、]", "", raw.lower())
    entity = compact[:18]
    return FactFrame(
        entity=entity,
        attribute="value" if value else "",
        value=value,
        unit=unit,
        valid_time=valid_time,
        tokens=years + nums,
    )


def is_volatile_fact(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker.lower() in lowered for marker in VOLATILE_MARKERS)


def type_ttl_map(policy: Any) -> dict[str, int]:
    mapping = dict(DEFAULT_TTL_BY_TYPE)
    extra = getattr(policy, "ttl_by_type", None) or {}
    for key, value in dict(extra).items():
        try:
            mapping[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return mapping


def effective_ttl_days(record: MemoryRecord, policy: Any) -> int:
    mapping = type_ttl_map(policy)
    ttl = mapping.get(record.type_label(), int(getattr(policy, "ttl_days", 90) or 90))
    if record.memory_type == MemoryType.SEMANTIC and (
        record.metadata.get("volatile") or is_volatile_fact(record.fact)
    ):
        volatile_ttl = int(getattr(policy, "volatile_semantic_ttl_days", 7) or 7)
        if ttl <= 0:
            ttl = volatile_ttl
        else:
            ttl = min(ttl, volatile_ttl)
    return ttl


def _parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def record_is_expired(record: MemoryRecord, policy: Any) -> bool:
    """valid_to 优先；否则按类型 TTL。ttl<=0 表示不过期。"""
    until = _parse_iso(getattr(record, "valid_to", "") or "")
    if until is not None and datetime.now(timezone.utc) > until:
        return True
    ttl = effective_ttl_days(record, policy)
    if ttl <= 0:
        return False
    return record.age_days() > ttl


def source_needs_refresh(entry: Any, *, freshness_days: int) -> bool:
    """来源台账：刚查过可复用，过期或内容指纹变化则应 revisit。"""
    if freshness_days <= 0:
        return False
    checked = str(getattr(entry, "last_checked_at", "") or getattr(entry, "last_used_at", "") or "")
    parsed = _parse_iso(checked)
    if parsed is None:
        return True
    age = (datetime.now(timezone.utc) - parsed).days
    return age >= freshness_days
