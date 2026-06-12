"""RAG 核心功能 unit test。

覆盖：
    - cosine_similarity 数学正确
    - 旧 RagStore CRUD + reload
    - SQLite 向量库持久化
    - RAG 会话历史后台索引 + 按会话召回

OpenAICompatEmbeddingService 实装由 DS 完成，留 stub 测试。
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import httpx
import pytest

from memory.rag_memory import RagMemoryService, SqliteVectorStore
from memory.rag_store import RagEntry, RagStore, cosine_similarity


class _FakeEmbedding:
    """测试用假 embedding：按关键词生成稳定向量，便于断言召回。"""

    def __init__(self) -> None:
        self.batch_calls = 0
        self.one_calls = 0

    async def embed_one(self, text: str) -> list[float]:
        self.one_calls += 1
        return self._vec(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.batch_calls += 1
        return [self._vec(text) for text in texts]

    @property
    def dimension(self) -> int:
        return 3

    async def aclose(self) -> None:
        pass

    def _vec(self, text: str) -> list[float]:
        if "猫" in text:
            return [1.0, 0.0, 0.0]
        if "茶会" in text:
            return [0.0, 1.0, 0.0]
        h = hashlib.md5(text.encode("utf-8")).digest()[0]
        return [0.0, 0.0, h / 255.0]


def test_cosine_basic():
    assert cosine_similarity([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0, 0], [0, 1, 0]) == pytest.approx(0.0)
    assert cosine_similarity([1, 0, 0], [-1, 0, 0]) == pytest.approx(-1.0)
    # 边界
    assert cosine_similarity([], [1, 2]) == 0.0
    assert cosine_similarity([1, 2], [1, 2, 3]) == 0.0
    assert cosine_similarity([0, 0], [0, 0]) == 0.0


@pytest.mark.asyncio
async def test_openai_compat_embedding_uses_list_input_and_reports_body():
    from features.embedding import EmbeddingError, OpenAICompatEmbeddingService

    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        seen.append({"path": request.url.path, "body": body})
        return httpx.Response(400, json={"code": "1210", "message": "参数调用有误"})

    service = OpenAICompatEmbeddingService(
        base_url="https://embedding.example.com/v1",
        api_key="key",
        model="embed-model",
    )
    service._client = httpx.AsyncClient(
        base_url=service.base_url,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(EmbeddingError, match="1210"):
        await service.embed_one("test")

    assert seen[0]["path"] == "/v1/embeddings"
    assert b'"input":["test"]' in seen[0]["body"].replace(b" ", b"")


@pytest.mark.asyncio
async def test_rag_store_crud(tmp_path: Path):
    store = RagStore(tmp_path / "r.jsonl")
    await store.load()
    assert len(store) == 0

    await store.add(RagEntry(id="a", text="文本 A", vector=[1, 0, 0]))
    await store.add(RagEntry(id="b", text="文本 B", vector=[0.9, 0.1, 0]))
    await store.add(RagEntry(id="c", text="文本 C", vector=[0, 1, 0]))
    assert len(store) == 3

    # top_k
    hits = store.top_k([1, 0, 0], k=2)
    assert len(hits) == 2
    assert hits[0][0].id == "a"
    assert hits[1][0].id == "b"

    # remove_by_id
    removed = await store.remove_by_id("a")
    assert removed == 1
    assert len(store) == 2

    # 重新加载验证持久化
    store2 = RagStore(tmp_path / "r.jsonl")
    await store2.load()
    assert len(store2) == 2
    ids = {e.id for e in store2.all_entries()}
    assert ids == {"b", "c"}


@pytest.mark.asyncio
async def test_sqlite_vector_store_persists_and_filters_by_conversation(tmp_path: Path):
    from memory.rag_memory import RagDocument

    store = SqliteVectorStore(tmp_path / "rag.sqlite3")
    await store.load()
    await store.upsert_many(
        [
            RagDocument(
                id="a",
                text="用户喜欢猫",
                vector=[1.0, 0.0, 0.0],
                conversation_id="private:1",
                role="user",
            ),
            RagDocument(
                id="b",
                text="群里的茶会安排",
                vector=[0.0, 1.0, 0.0],
                conversation_id="group:2",
                role="assistant",
            ),
        ]
    )

    reloaded = SqliteVectorStore(tmp_path / "rag.sqlite3")
    await reloaded.load()
    assert len(reloaded) == 2
    hits = reloaded.top_k([1.0, 0.0, 0.0], k=3, conversation_id="private:1")
    assert [hit.document.id for hit in hits] == ["a"]


@pytest.mark.asyncio
async def test_rag_memory_indexes_history_in_background(tmp_path: Path):
    embedding = _FakeEmbedding()
    store = SqliteVectorStore(tmp_path / "rag.sqlite3")
    await store.load()
    service = RagMemoryService(embedding=embedding, store=store, top_k=3)
    await service.load()

    await service.enqueue_records(
        [
            {
                "role": "user",
                "content": "用户说自己喜欢猫",
                "conversation_id": "private:1",
                "metadata": {"timestamp": "2026-06-01 10:00:00"},
            },
            {
                "role": "system",
                "content": "系统记录不进入 RAG",
                "conversation_id": "private:1",
            },
            {
                "role": "assistant",
                "content": "群里的茶会安排在周五",
                "conversation_id": "group:2",
            },
        ]
    )

    assert embedding.batch_calls == 0
    await asyncio.wait_for(service._queue.join(), timeout=1.0)
    assert embedding.batch_calls == 1

    out = await service.retrieve_for_query("猫", conversation_id="private:1")
    assert "相关历史 · RAG 召回" in out
    assert "用户说自己喜欢猫" in out
    assert "茶会安排" not in out

    out_other = await service.retrieve_for_query("猫", conversation_id="private:404")
    assert out_other == ""
    await service.shutdown()
