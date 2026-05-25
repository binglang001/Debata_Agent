"""Embedding 服务接口 + OpenAI 兼容实现骨架。

Claude 定义 IEmbeddingService 接口和构造签名，DeepSeek 实装具体方法
（见 docs/deepseek_tasks.md 任务 1）。

OpenAI 兼容协议示例端点：
    POST {base_url}/embeddings
    Body: {"model": "text-embedding-v1", "input": "你好" 或 ["a", "b"]}
    Header: Authorization: Bearer {api_key}
返回：
    {"data": [{"embedding": [0.1, ...], "index": 0}, ...]}
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import httpx

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """embedding 调用相关异常。"""


class IEmbeddingService(ABC):
    """文本向量化的抽象接口。"""

    @abstractmethod
    async def embed_one(self, text: str) -> list[float]:
        """把一段文本变成一个向量。"""

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量 embed；返回顺序与输入一致。"""

    @abstractmethod
    async def aclose(self) -> None:
        """释放底层连接（httpx client / 本地模型等）。"""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """向量维度。第一次成功 embed 前可能返回 0。"""


class OpenAICompatEmbeddingService(IEmbeddingService):
    """走 OpenAI 兼容 /embeddings 端点。

    适配：OpenAI / DeepSeek / 智谱 / 火山方舟 / 硅基流动 / OpenRouter ……
    所有 OpenAI 兼容 LLM 平台几乎都提供 /v1/embeddings。

    实装由 DeepSeek 完成。Claude 只保留接口与构造签名。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._dim: int = 0

    def _get_client(self) -> httpx.AsyncClient:
        """懒创建 httpx 客户端。"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        return self._client

    async def _request(self, inputs: str | list[str]) -> list[dict]:
        """发送 POST /embeddings 请求，返回 data 列表。"""
        client = self._get_client()
        try:
            resp = await client.post(
                "/embeddings",
                json={"model": self.model, "input": inputs},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise EmbeddingError(
                f"embedding 请求失败：{e.response.status_code} {inputs}"
            ) from e
        except (httpx.TimeoutException, httpx.RequestError) as e:
            raise EmbeddingError(f"embedding 请求失败：{e}") from e
        data = resp.json()["data"]
        # 缓存维度
        if data and self._dim == 0:
            self._dim = len(data[0]["embedding"])
        return data

    async def embed_one(self, text: str) -> list[float]:
        data = await self._request(text)
        return data[0]["embedding"]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        data = await self._request(texts)
        return [item["embedding"] for item in data]

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def dimension(self) -> int:
        return self._dim


__all__ = ["EmbeddingError", "IEmbeddingService", "OpenAICompatEmbeddingService"]
