"""
【Phase 6】Citation-First Research — 证据链与引用管理

每个 step 注册 EvidenceSource，finalize 生成参考文献块并计算 CCR / 幻觉率。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

URL_PATTERN = re.compile(r"https?://[^\s\]\)\"'<>]+", re.IGNORECASE)
CITATION_MARKER_PATTERN = re.compile(r"\[(\d+)\]")
SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[。！？.!?])\s+")


@dataclass
class EvidenceSource:
    """单条可追溯证据。"""

    source_id: str
    step_index: int
    step_type: str
    source_kind: str  # url | sql | file | text | kb
    locator: str
    excerpt: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class Claim:
    """带引用的断言（可选，用于细粒度校验）。"""

    text: str
    source_ids: list[str] = field(default_factory=list)


class CitationManager:
    """管理证据注册、参考文献生成与引用指标。"""

    def __init__(self) -> None:
        self.sources: list[EvidenceSource] = []
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"src-{self._counter}"

    def register_from_step(
        self,
        step_index: int,
        step_type: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[EvidenceSource]:
        """从 step 产出中提取并注册证据源。"""
        if not content or not content.strip():
            return []

        registered: list[EvidenceSource] = []
        meta = metadata or {}

        for url in URL_PATTERN.findall(content)[:5]:
            src = EvidenceSource(
                source_id=self._next_id(),
                step_index=step_index,
                step_type=step_type,
                source_kind="url",
                locator=url.rstrip(".,;"),
                excerpt=content[:240].replace("\n", " "),
            )
            self.sources.append(src)
            registered.append(src)

        if step_type == "database_query":
            sql_hint = meta.get("sql_query") or self._extract_sql_hint(content)
            src = EvidenceSource(
                source_id=self._next_id(),
                step_index=step_index,
                step_type=step_type,
                source_kind="sql",
                locator=sql_hint or "mysql://structured_query",
                excerpt=content[:300].replace("\n", " "),
            )
            self.sources.append(src)
            registered.append(src)

        if step_type == "file_read":
            src = EvidenceSource(
                source_id=self._next_id(),
                step_index=step_index,
                step_type=step_type,
                source_kind="file",
                locator=meta.get("filename", "uploaded_file"),
                excerpt=content[:300].replace("\n", " "),
            )
            self.sources.append(src)
            registered.append(src)

        if step_type == "knowledge_base":
            src = EvidenceSource(
                source_id=self._next_id(),
                step_index=step_index,
                step_type=step_type,
                source_kind="kb",
                locator="ragflow://internal_kb",
                excerpt=content[:300].replace("\n", " "),
            )
            self.sources.append(src)
            registered.append(src)

        if not registered and len(content.strip()) >= 80:
            src = EvidenceSource(
                source_id=self._next_id(),
                step_index=step_index,
                step_type=step_type,
                source_kind="text",
                locator=f"step:{step_index}:{step_type}",
                excerpt=content[:300].replace("\n", " "),
            )
            self.sources.append(src)
            registered.append(src)

        return registered

    def _extract_sql_hint(self, content: str) -> str:
        for line in content.splitlines():
            lower = line.lower()
            if "select" in lower or "from" in lower or "表" in line:
                return line.strip()[:120]
        return ""

    def source_number_map(self) -> dict[str, int]:
        """source_id → 引用编号 [1][2]…"""
        return {src.source_id: idx + 1 for idx, src in enumerate(self.sources)}

    def build_references_block(self) -> str:
        if not self.sources:
            return ""
        lines = ["", "## 参考文献", ""]
        id_to_num = self.source_number_map()
        for src in self.sources:
            num = id_to_num[src.source_id]
            kind_label = {
                "url": "网络",
                "sql": "数据库",
                "file": "文件",
                "kb": "知识库",
                "text": "步骤产出",
            }.get(src.source_kind, src.source_kind)
            excerpt = src.excerpt[:120] + ("…" if len(src.excerpt) > 120 else "")
            lines.append(
                f"[{num}] ({kind_label}) {src.locator} — "
                f"Step {src.step_index + 1}/{src.step_type}: {excerpt}"
            )
        return "\n".join(lines)

    def inject_inline_citation_hints(self, content: str) -> str:
        """在正文段落末追加可用引用编号提示（轻量 Citation-First）。"""
        if not self.sources or not content.strip():
            return content

        id_to_num = self.source_number_map()
        step_nums: dict[int, list[int]] = {}
        for src in self.sources:
            step_nums.setdefault(src.step_index, []).append(id_to_num[src.source_id])

        header = (
            "> **Evidence-First 报告**：正文关键结论应标注 [n] 引用；"
            "完整来源见文末参考文献。\n\n"
        )
        if content.startswith("> **Evidence-First"):
            header = ""

        paragraphs = [p for p in content.split("\n\n") if p.strip()]
        if len(paragraphs) <= 1:
            nums = sorted({id_to_num[s.source_id] for s in self.sources})
            hint = "".join(f"[{n}]" for n in nums[:3])
            if hint and hint not in content:
                return header + content.rstrip() + f" {hint}\n"
            return header + content

        enriched: list[str] = []
        for idx, para in enumerate(paragraphs):
            if para.startswith("##") or para.startswith("> **Evidence"):
                enriched.append(para)
                continue
            if CITATION_MARKER_PATTERN.search(para):
                enriched.append(para)
                continue
            step_idx = min(idx, len(self.sources) - 1)
            src = self.sources[step_idx]
            num = id_to_num[src.source_id]
            enriched.append(para.rstrip() + f" [{num}]")
        return header + "\n\n".join(enriched)

    def build_cited_report(self, raw_content: str) -> str:
        """生成带引用提示与参考文献块的最终报告。"""
        body = self.inject_inline_citation_hints(raw_content)
        refs = self.build_references_block()
        if refs and refs.strip() not in body:
            return body.rstrip() + "\n" + refs + "\n"
        return body

    def compute_metrics(self, final_content: str) -> dict[str, float]:
        """计算 Citation Coverage Rate 与 Hallucination Rate（启发式）。"""
        if not self.sources:
            return {
                "citation_coverage_rate": 0.0,
                "hallucination_rate": 1.0,
                "cited_markers": 0,
                "registered_sources": 0,
            }

        cited_nums = set(int(m) for m in CITATION_MARKER_PATTERN.findall(final_content))
        registered = len(self.sources)
        coverage = min(1.0, len(cited_nums) / registered) if registered else 0.0

        sentences = [
            s.strip()
            for s in SENTENCE_SPLIT_PATTERN.split(final_content)
            if len(s.strip()) >= 12
            and not s.strip().startswith("#")
            and not s.strip().startswith(">")
            and "参考文献" not in s
        ]
        uncited = 0
        for sentence in sentences:
            if not CITATION_MARKER_PATTERN.search(sentence):
                uncited += 1
        hallucination = uncited / len(sentences) if sentences else (1.0 - coverage)

        return {
            "citation_coverage_rate": round(coverage, 3),
            "hallucination_rate": round(min(1.0, hallucination), 3),
            "cited_markers": len(cited_nums),
            "registered_sources": registered,
        }

    def validate_citations(self, final_content: str, min_coverage: float = 0.2) -> tuple[bool, str]:
        metrics = self.compute_metrics(final_content)
        if metrics["registered_sources"] == 0:
            return True, ""
        if "## 参考文献" not in final_content and metrics["citation_coverage_rate"] < min_coverage:
            return False, "citation_coverage_low"
        if metrics["citation_coverage_rate"] < min_coverage:
            return False, "citation_coverage_low"
        return True, ""

    def to_dict_list(self) -> list[dict[str, Any]]:
        return [asdict(src) for src in self.sources]

    def save_evidence_json(self, session_dir: Path) -> Path | None:
        if not self.sources:
            return None
        path = session_dir / "evidence.json"
        payload = {
            "sources": self.to_dict_list(),
            "generated_at": datetime.now().isoformat(),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
