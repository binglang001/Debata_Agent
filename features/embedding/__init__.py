"""Embedding 服务 —— 把文本变成向量，给 RAG 长期记忆用。

接口由 Claude 定义，OpenAICompatEmbeddingService 的具体实装由 DeepSeek 完成
（见 docs/deepseek_tasks.md 任务 1）。
"""

from __future__ import annotations

from .service import (
    EmbeddingError,
    IEmbeddingService,
    OpenAICompatEmbeddingService,
)

__all__ = [
    "EmbeddingError",
    "IEmbeddingService",
    "OpenAICompatEmbeddingService",
]
