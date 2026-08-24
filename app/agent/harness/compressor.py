"""
上下文压缩

Phase 2：优先使用 LLM 摘要压缩，失败或未配置模型时降级为规则截断。
"""

from __future__ import annotations

from typing import Any, Optional

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

COMPRESS_THRESHOLD_CHARS = 2000
MAX_OUTPUT_CHARS = 2000


class ContextCompressor:
    def __init__(
        self,
        model: Any = None,
        max_output_chars: int = MAX_OUTPUT_CHARS,
        enabled: bool = True,
        threshold_chars: int = COMPRESS_THRESHOLD_CHARS,
    ):
        self.model = model if enabled else None
        self.max_output_chars = max_output_chars
        self.threshold_chars = max(200, threshold_chars)

    def estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

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
        }
        if source_metadata:
            meta["source_metadata"] = source_metadata

        if original_len <= self.threshold_chars:
            return raw_result, meta

        if self.model is not None:
            llm_result = await self._compress_with_llm(raw_result, step_type)
            if llm_result:
                meta.update(
                    {
                        "method": "llm",
                        "compressed_chars": len(llm_result),
                        "compression_ratio": round(len(llm_result) / original_len, 3),
                    }
                )
                return llm_result, meta

        truncated = self._truncate(raw_result)
        meta.update(
            {
                "method": "truncate",
                "compressed_chars": len(truncated),
                "compression_ratio": round(len(truncated) / original_len, 3),
            }
        )
        return truncated, meta

    async def _compress_with_llm(self, raw_result: str, step_type: str) -> Optional[str]:
        prompt = COMPRESS_PROMPT.format(
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
