"""KV 缓存命中率测量基础设施。

Phase 1.10 验证清单要求：连续多轮对话 cached_tokens / prompt_tokens > 90%。
此模块提供测量包装器，方便把任意 IProvider 包装一层后采集指标。

各家 provider 的 cached_tokens 字段路径：
    - DeepSeek:  usage.prompt_cache_hit_tokens
    - OpenAI:    usage.prompt_tokens_details.cached_tokens
    - Anthropic: usage.cache_read_input_tokens（cache_creation_input_tokens 是创建数）
    - GLM/SiliconFlow: 未明确暴露（按 0 计）
    - Gemini:    usage_metadata.cached_content_token_count

该模块抹平这些差异。

使用方式（在 P1.10 测试中）：
    base_provider = build_provider(...)
    wrapped = MetricsProvider(base_provider)
    chat_agent = ChatAgent(wrapped, agent_cfg)
    await chat_agent.run(...)
    print(wrapped.report())
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from providers.base import CompletionResult, IProvider, ReasoningConfig

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CacheMetricsSample:
    """单次 chat_completion 调用的缓存指标。"""

    prompt_tokens: int = 0
    cached_tokens: int = 0
    cache_creation_tokens: int = 0
    """Anthropic 特有：本次新写入缓存的 token 数（不计入 hit_rate）"""

    completion_tokens: int = 0
    model: str = ""

    @property
    def hit_rate(self) -> float:
        if self.prompt_tokens <= 0:
            return 0.0
        return self.cached_tokens / self.prompt_tokens


@dataclass(slots=True)
class CacheMetricsReport:
    """多次调用的汇总报告。"""

    samples: list[CacheMetricsSample] = field(default_factory=list)

    @property
    def total_prompt(self) -> int:
        return sum(s.prompt_tokens for s in self.samples)

    @property
    def total_cached(self) -> int:
        return sum(s.cached_tokens for s in self.samples)

    @property
    def overall_hit_rate(self) -> float:
        if self.total_prompt == 0:
            return 0.0
        return self.total_cached / self.total_prompt

    @property
    def per_round_hit_rates(self) -> list[float]:
        return [s.hit_rate for s in self.samples]

    def to_text(self) -> str:
        if not self.samples:
            return "无样本"
        lines = [
            f"共 {len(self.samples)} 次调用",
            f"总 prompt_tokens = {self.total_prompt}",
            f"总 cached_tokens = {self.total_cached}",
            f"整体命中率 = {self.overall_hit_rate:.2%}",
            "",
            "逐轮：",
        ]
        for i, s in enumerate(self.samples, 1):
            lines.append(
                f"  轮 {i}: model={s.model}, prompt={s.prompt_tokens}, "
                f"cached={s.cached_tokens}, hit_rate={s.hit_rate:.2%}"
            )
        return "\n".join(lines)


class MetricsProvider(IProvider):
    """包装 IProvider，每次 chat_completion 都采集缓存指标。

    完全透传请求参数和返回值；仅在内部累计样本。
    """

    def __init__(self, inner: IProvider) -> None:
        super().__init__(name=f"metrics({inner.name})")
        self.inner = inner
        self.report = CacheMetricsReport()

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.6,
        top_p: float = 1.0,
        max_tokens: int = 16384,
        reasoning: ReasoningConfig | None = None,
        stream: bool = True,
        timeout: float = 120.0,
        first_token_timeout: float | None = 30.0,
        extra: dict[str, Any] | None = None,
    ) -> CompletionResult:
        result = await self.inner.chat_completion(
            messages,
            model=model,
            tools=tools,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            reasoning=reasoning,
            stream=stream,
            timeout=timeout,
            first_token_timeout=first_token_timeout,
            extra=extra,
        )
        sample = extract_cache_metrics(result, model=model)
        self.report.samples.append(sample)
        logger.debug(
            f"Cache sample: prompt={sample.prompt_tokens}, "
            f"cached={sample.cached_tokens}, hit={sample.hit_rate:.2%}"
        )
        return result

    async def aclose(self) -> None:
        await self.inner.aclose()


def extract_cache_metrics(
    result: CompletionResult,
    *,
    model: str = "",
) -> CacheMetricsSample:
    """从 CompletionResult.raw 中按 provider 类型提取缓存指标。

    TODO（按需扩展）：每家 provider 的 usage 字段不同，
    必要时按 result.raw 的类型分别取。

    当前从统一 Usage 字段读到的是 reasoning_tokens / prompt / completion，
    缓存命中要从 raw（原始 SDK 响应）里再抠。
    """
    sample = CacheMetricsSample(
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        model=model or result.model,
    )

    raw = result.raw
    # OpenAI 风格（DeepSeek/GLM/Moonshot 等都用 openai SDK）
    # 形如：raw.usage.prompt_tokens_details.cached_tokens
    # 或   raw.usage.prompt_cache_hit_tokens（DeepSeek 旧字段）
    cached = _safe_get(raw, ["usage", "prompt_tokens_details", "cached_tokens"])
    if cached is None:
        cached = _safe_get(raw, ["usage", "prompt_cache_hit_tokens"])
    if cached is not None:
        sample.cached_tokens = int(cached)
        return sample

    # Anthropic
    cache_read = _safe_get(raw, ["usage", "cache_read_input_tokens"])
    cache_creation = _safe_get(raw, ["usage", "cache_creation_input_tokens"])
    if cache_read is not None:
        sample.cached_tokens = int(cache_read)
    if cache_creation is not None:
        sample.cache_creation_tokens = int(cache_creation)
    if cache_read is not None or cache_creation is not None:
        return sample

    # Gemini
    gemini_cached = _safe_get(raw, ["usage_metadata", "cached_content_token_count"])
    if gemini_cached is not None:
        sample.cached_tokens = int(gemini_cached)
        return sample

    return sample


def _safe_get(obj: Any, path: list[str]) -> Any:
    """安全地按 attr 路径取值。支持 obj.attr 和 obj["key"] 两种风格。"""
    cur = obj
    for key in path:
        if cur is None:
            return None
        if hasattr(cur, key):
            cur = getattr(cur, key)
        elif isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return None
    return cur
