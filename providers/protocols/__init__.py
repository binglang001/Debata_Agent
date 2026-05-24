"""协议层：实现各家 LLM API 的具体调用。

可用协议（构造时 protocol 字段对应）：
    openai_compat  —— OpenAI 兼容（DeepSeek/GLM/Moonshot/Qwen/Volcengine/OpenRouter/零一万物/Gemini 兼容端点 等）
    anthropic      —— Anthropic Claude

新增协议时：
    1. 在本目录添加 myproto.py，定义实现 IProvider 的类
    2. 在 providers/registry.py 的 PROTOCOL_REGISTRY 中注册
"""

from .anthropic_proto import AnthropicProvider
from .openai_compat import OpenAICompatProvider

__all__ = ["OpenAICompatProvider", "AnthropicProvider"]
