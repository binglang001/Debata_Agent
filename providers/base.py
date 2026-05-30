"""LLM 提供商抽象层。

设计目标：
    - 所有提供商（DeepSeek/GLM/OpenAI/Anthropic/Gemini/...）都向上提供同一套调用接口
    - 内部 message 格式统一用 OpenAI 风格（{"role", "content", "tool_calls", "tool_call_id"}）
    - 协议层负责把 OpenAI 风格 ↔ 各家 SDK 的具体格式

内部统一格式（所有 Agent 都用这个交互）：
    [
        {"role": "system", "content": "你是..."},
        {"role": "user", "content": "..."},
        {"role": "assistant", "content": "...", "tool_calls": [...]},
        {"role": "tool", "tool_call_id": "...", "content": "..."},
    ]

返回统一格式（CompletionResult）：
    - content: 文本输出
    - tool_calls: 工具调用列表
    - reasoning_content: 思考内容（如 DeepSeek Reasoner、Claude extended thinking）
    - reasoning_blocks: 需要原样回传的思考块（如 Claude signed thinking blocks）
    - usage: token 用量
    - finish_reason: stop / tool_calls / length / ...
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)


# ============================================================
# 统一数据类型
# ============================================================


@dataclass(slots=True)
class ToolCall:
    """工具调用记录。"""

    id: str
    name: str
    arguments: str
    """JSON 编码的参数字符串（保持原样以避免重新编码丢失精度）"""


@dataclass(slots=True)
class Usage:
    """token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0


@dataclass(slots=True)
class CompletionResult:
    """LLM 调用结果。"""

    content: str = ""
    """主要文本输出"""

    tool_calls: list[ToolCall] = field(default_factory=list)
    """工具调用（如果有）"""

    reasoning_content: str = ""
    """思考内容（reasoning_content / thinking 等）"""

    reasoning_blocks: list[dict[str, Any]] = field(default_factory=list)
    """Provider 特定的思考块。仅用于后续请求原样回传，不用于展示。"""

    finish_reason: str = ""
    """stop / tool_calls / length / content_filter / ..."""

    usage: Usage = field(default_factory=Usage)

    model: str = ""
    """实际返回的模型 ID（可能与请求不同）"""

    raw: Any = None
    """原始响应对象，供调试"""

    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@dataclass(slots=True)
class ReasoningConfig:
    """跨提供商的思考配置（每家 SDK 翻译时映射）。"""

    enabled: bool = False
    budget: Literal["low", "medium", "high"] | None = None
    """语义化预算等级"""

    max_tokens: int | None = None
    """显式 thinking budget（Anthropic/Gemini）。budget 字段未指定时此项生效。"""


# ============================================================
# 错误类型
# ============================================================


class ProviderError(Exception):
    """提供商层异常基类。"""


class ProviderAuthError(ProviderError):
    """认证失败（401/403）。"""


class ProviderRateLimitError(ProviderError):
    """限流（429）。"""


class ProviderTimeoutError(ProviderError):
    """请求超时。"""


class ProviderUnsupportedError(ProviderError):
    """该 provider 不支持请求的功能（如不支持 tool_call）。"""


# ============================================================
# IProvider 抽象基类
# ============================================================


class IProvider(ABC):
    """所有 LLM 提供商必须实现的接口。"""

    name: str
    """provider 实例 ID（如 'deepseek_main'）"""

    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
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
        """执行一次对话补全。

        Args:
            messages: OpenAI 风格消息列表
            model: 模型 ID（如 'deepseek-chat'）
            tools: OpenAI 风格 tool 定义（None 表示不传工具）
            temperature, top_p, max_tokens: 标准采样参数
            reasoning: 思考配置；None 表示不启用
            stream: 是否流式输出
            timeout: 总超时（秒）
            first_token_timeout: 首 token 超时（秒），仅 stream=True 时有效
            extra: 协议特定参数（直接合并到 SDK 调用参数）

        Returns:
            CompletionResult
        """

    @abstractmethod
    async def aclose(self) -> None:
        """关闭底层 HTTP 客户端。"""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"


# ============================================================
# 协议层级简单工具
# ============================================================


def normalize_messages(
    messages: list[dict[str, Any]],
    *,
    preserve_reasoning_content: bool = False,
) -> list[dict[str, Any]]:
    """复制并轻度规整 messages。

    - role/content/tool_calls/tool_call_id 之外的字段会被剔除
      （某些 SDK 严格校验未知字段）
    - preserve_reasoning_content=True 时，assistant 消息中的 reasoning_content
      会被保留（含空字符串），用于 DeepSeek/Qwen 等思考模式回放
    - tool_calls 内部的 'type': 'function' 缺失时补齐
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        if "role" not in m:
            continue
        normalized: dict[str, Any] = {"role": m["role"]}
        if "content" in m and m["content"] is not None:
            normalized["content"] = m["content"]
        if "name" in m:
            normalized["name"] = m["name"]
        if "tool_call_id" in m:
            normalized["tool_call_id"] = m["tool_call_id"]
        if (
            preserve_reasoning_content
            and m.get("role") == "assistant"
            and "reasoning_content" in m
        ):
            value = m.get("reasoning_content")
            normalized["reasoning_content"] = "" if value is None else str(value)
        if "tool_calls" in m and m["tool_calls"]:
            normalized_tcs = []
            for tc in m["tool_calls"]:
                normalized_tc = {
                    "id": tc.get("id", ""),
                    "type": tc.get("type", "function"),
                    "function": {
                        "name": tc.get("function", {}).get("name", ""),
                        "arguments": tc.get("function", {}).get("arguments", ""),
                    },
                }
                normalized_tcs.append(normalized_tc)
            normalized["tool_calls"] = normalized_tcs
        out.append(normalized)
    return out
