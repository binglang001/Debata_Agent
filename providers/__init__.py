"""Debata_Agent 多提供商系统。

公开 API：
    IProvider             —— 提供商抽象
    CompletionResult      —— 调用结果统一格式
    ReasoningConfig       —— 思考配置
    ProviderRegistry      —— 提供商实例管理
    build_provider        —— 根据 ProviderConfig 构造一个 provider
    load_all_presets      —— 加载内置预设
    register_protocol     —— 注册新协议实现
"""

from .base import (
    CompletionResult,
    IProvider,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnsupportedError,
    ReasoningConfig,
    ToolCall,
    Usage,
)
from .presets_loader import ModelInfo, ProviderPreset, load_all_presets
from .health import (
    ProviderHealth,
    probe_embedding_endpoint,
    probe_embedding_provider_instance,
    probe_provider_endpoint,
    probe_provider_instance,
)
from .protocols import AnthropicProvider, OpenAICompatProvider
from .registry import (
    PROTOCOL_REGISTRY,
    ProviderRegistry,
    build_provider,
    known_protocols,
    register_protocol,
)

__all__ = [
    # base
    "IProvider",
    "CompletionResult",
    "ReasoningConfig",
    "ToolCall",
    "Usage",
    "ProviderError",
    "ProviderAuthError",
    "ProviderRateLimitError",
    "ProviderTimeoutError",
    "ProviderUnsupportedError",
    # presets
    "ProviderPreset",
    "ModelInfo",
    "load_all_presets",
    "ProviderHealth",
    "probe_embedding_endpoint",
    "probe_embedding_provider_instance",
    "probe_provider_endpoint",
    "probe_provider_instance",
    # protocols
    "OpenAICompatProvider",
    "AnthropicProvider",
    # registry
    "ProviderRegistry",
    "PROTOCOL_REGISTRY",
    "build_provider",
    "register_protocol",
    "known_protocols",
]
