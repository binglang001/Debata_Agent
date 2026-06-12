"""Embedding 服务 —— 把文本变成向量，给 RAG 长期记忆用。"""

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
    "get_local_service",
]


def get_local_service(model_dir: str, device: str = "auto"):
    """工厂函数，按需懒加载 LocalEmbeddingService。"""
    from .local_service import LocalEmbeddingService
    return LocalEmbeddingService(model_dir, device)
