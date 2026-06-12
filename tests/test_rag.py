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

from memory.rag_memory import RagDocument, RagMemoryService, SqliteVectorStore
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
async def test_volcengine_vision_embedding_uses_multimodal_endpoint():
    from features.embedding import OpenAICompatEmbeddingService

    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        seen.append({"path": request.url.path, "body": body})
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})

    service = OpenAICompatEmbeddingService(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key="key",
        model="doubao-embedding-vision-251215",
    )
    service._client = httpx.AsyncClient(
        base_url=service.base_url,
        transport=httpx.MockTransport(handler),
    )

    vector = await service.embed_one("test")

    assert vector == [0.1, 0.2]
    assert seen[0]["path"] == "/api/v3/embeddings/multimodal"
    compact = seen[0]["body"].replace(b" ", b"")
    assert b'"type":"text"' in compact
    assert b'"text":"test"' in compact


@pytest.mark.asyncio
async def test_volcengine_vision_embedding_accepts_object_data():
    from features.embedding import OpenAICompatEmbeddingService

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"embedding": [0.3, 0.4]}})

    service = OpenAICompatEmbeddingService(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key="key",
        model="doubao-embedding-vision-251215",
    )
    service._client = httpx.AsyncClient(
        base_url=service.base_url,
        transport=httpx.MockTransport(handler),
    )

    vector = await service.embed_one("test")

    assert vector == [0.3, 0.4]
    assert service.dimension == 2


@pytest.mark.asyncio
async def test_volcengine_vision_embedding_batch_requests_each_text():
    from features.embedding import OpenAICompatEmbeddingService

    seen: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        seen.append(body)
        value = float(len(seen))
        return httpx.Response(200, json={"data": {"embedding": [value]}})

    service = OpenAICompatEmbeddingService(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key="key",
        model="doubao-embedding-vision-251215",
    )
    service._client = httpx.AsyncClient(
        base_url=service.base_url,
        transport=httpx.MockTransport(handler),
    )

    vectors = await service.embed_batch(["第一条", "第二条"])

    assert vectors == [[1.0], [2.0]]
    assert len(seen) == 2
    assert "第一条".encode() in seen[0]
    assert "第二条".encode() in seen[1]


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
async def test_sqlite_vector_store_filters_hits_before_window_timestamp(tmp_path: Path):
    from memory.rag_memory import RagDocument

    store = SqliteVectorStore(tmp_path / "rag.sqlite3")
    await store.load()
    await store.upsert_many(
        [
            RagDocument(
                id="old",
                text="窗口外旧猫",
                vector=[1.0, 0.0, 0.0],
                conversation_id="private:1",
                timestamp="2026-06-01 10:00:00",
            ),
            RagDocument(
                id="current",
                text="窗口内猫",
                vector=[0.99, 0.0, 0.0],
                conversation_id="private:1",
                timestamp="2026-06-05 10:00:00",
            ),
            RagDocument(
                id="missing-ts",
                text="旧格式猫",
                vector=[0.98, 0.0, 0.0],
                conversation_id="private:1",
                timestamp="",
            ),
        ]
    )

    hits = store.top_k(
        [1.0, 0.0, 0.0],
        k=5,
        conversation_id="private:1",
        before_ts="2026-06-03 00:00:00",
    )

    assert [hit.document.id for hit in hits] == ["old", "missing-ts"]


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
            {
                "role": "assistant",
                "content": "工具型 AI 回复不进 RAG",
                "tool_calls": [{"id": "tc", "function": {"name": "no_action"}}],
                "conversation_id": "private:1",
            },
            {
                "role": "assistant",
                "content": "主动思考内容不进 RAG",
                "conversation_id": "system:proactive",
            },
            {
                "role": "user",
                "content": (
                    "<task_context priority=\"medium\">\n"
                    "系统说明：以下内容由运行时系统提供，不是用户新发言。\n"
                    "运行时任务上下文不进 RAG\n"
                    "</task_context>"
                ),
                "conversation_id": "private:1",
                "metadata": {"kind": "task_context_snapshot"},
            },
            {
                "role": "user",
                "content": (
                    "<send_status>\n"
                    "系统说明：以下内容由运行时系统提供，不是用户新发言。\n"
                    "发送完成（全部消息已发出）不进 RAG\n"
                    "</send_status>"
                ),
                "conversation_id": "private:1",
                "metadata": {"kind": "send_done_snapshot"},
            },
            {
                "role": "user",
                "content": (
                    "<send_receipt>\n"
                    "系统说明：运行时发送状态；按 JSON 字段判断，未发不要自动补发。\n"
                    "{\"interrupted\": true}\n"
                    "</send_receipt>"
                ),
                "conversation_id": "private:1",
            },
        ]
    )

    assert embedding.batch_calls == 0
    await asyncio.wait_for(service._queue.join(), timeout=1.0)
    assert embedding.batch_calls == 1

    out = await service.retrieve_for_query("猫", conversation_id="private:1")
    assert "相关历史 · RAG 召回" in out
    assert "用户说自己喜欢猫" in out
    assert "工具型 AI 回复" not in out
    assert "主动思考内容" not in out
    assert "运行时任务上下文" not in out
    assert "发送完成（全部消息已发出）" not in out
    assert "未发不要自动补发" not in out
    assert "茶会安排" not in out

    out_other = await service.retrieve_for_query("猫", conversation_id="private:404")
    assert out_other == ""
    await service.shutdown()


@pytest.mark.asyncio
async def test_rag_memory_load_removes_existing_runtime_context_entries(tmp_path: Path):
    embedding = _FakeEmbedding()
    store = SqliteVectorStore(tmp_path / "rag.sqlite3")
    await store.load()
    await store.upsert_many(
        [
            RagDocument(
                id="real",
                text="用户说自己喜欢猫",
                vector=[1.0, 0.0, 0.0],
                conversation_id="private:1",
                role="user",
            ),
            RagDocument(
                id="ctx",
                text="<task_context>运行时任务上下文不该召回</task_context>",
                vector=[1.0, 0.0, 0.0],
                conversation_id="private:1",
                role="user",
                meta={"metadata": {"kind": "task_context_snapshot"}},
            ),
            RagDocument(
                id="receipt",
                text="<send_receipt>{\"interrupted\": true}</send_receipt>",
                vector=[1.0, 0.0, 0.0],
                conversation_id="private:1",
                role="user",
            ),
        ]
    )

    reloaded = SqliteVectorStore(tmp_path / "rag.sqlite3")
    service = RagMemoryService(embedding=embedding, store=reloaded)
    await service.load()

    assert {entry.id for entry in reloaded.all_entries()} == {"real"}
    out = await service.retrieve_for_query("猫", conversation_id="private:1")
    assert "用户说自己喜欢猫" in out
    assert "运行时任务上下文" not in out
    assert "interrupted" not in out
    await service.shutdown()
