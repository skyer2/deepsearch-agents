"""
上下文压缩

Phase 2：优先使用 LLM 摘要压缩，失败或未配置模型时降级为规则截断。
Phase 19：按 step_type 分模板 + 压缩后 URL/数字保留检查，失败则打补丁。
"""

from __future__ import annotations

from typing import Any, Optional

from app.agent.harness.retention import apply_retention_patch

COMPRESS_PROMPT = """请将以下 Agent 执行结果压缩为结构化摘要。

要求：
1. 保留关键事实、数据、来源信息（URL、表名、文件名必须保留）
2. 删除重复和无关内容
3. 使用中文，不超过 {max_chars} 字
4. 步骤类型：{step_type}
5. 【重要】保留原始 source 标记，如 [source:src-N] 或 URL 链接

原始内容：
{content}
"""

COMPRESS_PROMPT_BY_STEP = {
    "network_search": """请将以下网络检索结果压缩为结构化摘要。

要求：
1. 所有 URL 必须原样保留
2. 所有百分比、金额、年份等数字必须原样保留
3. 删除广告、导航、重复段落
4. 使用中文，不超过 {max_chars} 字
5. 步骤类型：{step_type}

原始内容：
{content}
""",
    "database_query": """请将以下数据库查询结果压缩为结构化摘要。

要求：
1. 表名、列名、SQL 关键字必须保留
2. 行数、聚合数字必须原样保留
3. 删除重复空行
4. 使用中文，不超过 {max_chars} 字
5. 步骤类型：{step_type}

原始内容：
{content}
""",
    "knowledge_base": """请将以下知识库检索结果压缩为结构化摘要。

要求：
1. 文档名 / 切片来源必须保留
2. 关键定义与数字必须原样保留
3. 删除重复内容
4. 使用中文，不超过 {max_chars} 字
5. 步骤类型：{step_type}

原始内容：
{content}
""",
    "file_read": """请将以下文件读取结果压缩为结构化摘要。

要求：
1. 文件名必须保留
2. 关键数字、条款标题必须保留
3. 使用中文，不超过 {max_chars} 字
4. 步骤类型：{step_type}

原始内容：
{content}
""",
}

COMPRESS_THRESHOLD_CHARS = 2000
MAX_OUTPUT_CHARS = 2000


class ContextCompressor:
    def __init__(
        self,
        model: Any = None,
        max_output_chars: int = MAX_OUTPUT_CHARS,
        enabled: bool = True,
        threshold_chars: int = COMPRESS_THRESHOLD_CHARS,
        retention_check: bool = True,
        min_url_retention: float = 0.8,
        min_number_retention: float = 0.5,
    ):
        self.model = model if enabled else None
        self.max_output_chars = max_output_chars
        self.threshold_chars = max(200, threshold_chars)
        self.retention_check = retention_check
        self.min_url_retention = min_url_retention
        self.min_number_retention = min_number_retention

    def estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def _prompt_for(self, step_type: str) -> str:
        return COMPRESS_PROMPT_BY_STEP.get(step_type, COMPRESS_PROMPT)

    async def compress(
        self,
        raw_result: str,
        step_type: str = "generic",
        source_metadata: dict | None = None,
    ) -> tuple[str, dict]:
        """返回 (压缩后文本, 元数据)。【Phase 6】source_metadata 写入压缩元数据。"""
        original_len = len(raw_result)
        meta: dict[str, Any] = {
            "method": "none",
            "step_type": step_type,
            "original_chars": original_len,
            "compressed_chars": original_len,
            "compression_ratio": 1.0,
            "entity_retention": 1.0,
            "retention_patched": False,
        }
        if source_metadata:
            meta["source_metadata"] = source_metadata

        if original_len <= self.threshold_chars:
            return raw_result, meta

        compressed = raw_result
        if self.model is not None:
            llm_result = await self._compress_with_llm(raw_result, step_type)
            if llm_result:
                compressed = llm_result
                meta["method"] = "llm"
            else:
                compressed = self._truncate(raw_result)
                meta["method"] = "truncate"
        else:
            compressed = self._truncate(raw_result)
            meta["method"] = "truncate"

        if self.retention_check and meta["method"] != "none":
            compressed, retention_meta = apply_retention_patch(
                raw_result,
                compressed,
                min_url_retention=self.min_url_retention,
                min_number_retention=self.min_number_retention,
            )
            meta.update(retention_meta)
            if retention_meta.get("retention_patched"):
                meta["method"] = f"{meta['method']}+retention_patch"

        meta["compressed_chars"] = len(compressed)
        meta["compression_ratio"] = round(len(compressed) / original_len, 3) if original_len else 1.0
        return compressed, meta

    async def _compress_with_llm(self, raw_result: str, step_type: str) -> Optional[str]:
        prompt = self._prompt_for(step_type).format(
            max_chars=self.max_output_chars,
            step_type=step_type,
            content=raw_result[:12000],
        )
        try:
            from app.agent.harness.usage_tracker import tracked_ainvoke

            response = await tracked_ainvoke(
                self.model,
                prompt,
                phase="compress",
            )
            content = getattr(response, "content", response)
            if isinstance(content, list):
                content = "".join(
                    block.get("text", "") if isinstance(block, dict) else str(block)
                    for block in content
                )
            text = str(content).strip()
            if text:
                return text
        except Exception as exc:
            print(f"[Compressor] LLM compress failed, fallback to truncate: {exc}")
        return None

    def _truncate(self, raw_result: str) -> str:
        truncated = raw_result[: self.max_output_chars]
        return (
            f"{truncated}\n\n[已截断压缩: 原始 {len(raw_result)} 字符 "
            f"→ {len(truncated)} 字符]"
        )

    def compress_sync(self, raw_result: str, step_type: str = "generic") -> str:
        """同步兼容接口（仅截断），供单元测试使用。"""
        if len(raw_result) <= self.threshold_chars:
            return raw_result
        return self._truncate(raw_result)
