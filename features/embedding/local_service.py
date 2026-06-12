"""本地 sentence-transformers embedding —— 纯离线，不联网。"""

from __future__ import annotations

import asyncio
import logging

from .service import IEmbeddingService

logger = logging.getLogger(__name__)


class LocalEmbeddingService(IEmbeddingService):
    """用 sentence-transformers 做本地向量化。

    模型在 warmup() 中加载（Runtime 启动后台触发）。
    embed_one/batch 若 warmup 未完成会先 await 完成。
    """

    def __init__(self, model_dir: str, device: str = "auto") -> None:
        self._model_dir = model_dir
        self._device = device
        self._model = None
        self._ready = asyncio.Event()
        self._loading_lock = asyncio.Lock()

    async def warmup(self) -> None:
        """加载 SentenceTransformer 模型。幂等。"""
        if self._ready.is_set():
            return
        async with self._loading_lock:
            if self._ready.is_set():
                return
            await asyncio.to_thread(self._load_sync)
            self._ready.set()

    def _load_sync(self) -> None:
        """实际同步加载（在 to_thread 里跑）。"""
        from sentence_transformers import SentenceTransformer

        device = self._device
        if device == "auto":
            try:
                import torch
                device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                device = "cpu"
        logger.info(f"加载本地 embedding 模型: {self._model_dir} (device={device})")
        self._model = SentenceTransformer(self._model_dir, device=device)

    async def embed_one(self, text: str) -> list[float]:
        if not self._ready.is_set():
            await self.warmup()
        result = await asyncio.to_thread(self._model.encode, text)
        return result.tolist()

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not self._ready.is_set():
            await self.warmup()
        result = await asyncio.to_thread(self._model.encode, texts)
        return result.tolist()

    async def aclose(self) -> None:
        self._model = None
        self._ready.clear()

    @property
    def dimension(self) -> int:
        if self._model is None:
            return 0
        return self._model.get_sentence_embedding_dimension()
