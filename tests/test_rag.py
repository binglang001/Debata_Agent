"""RAG 核心功能 unit test。

只测 Claude 实装的部分：
    - cosine_similarity 数学正确
    - RagStore CRUD + reload
    - ImportantMemoryManager attach_rag 后 save 自动索引 + retrieve_for_query 召回

OpenAICompatEmbeddingService 实装由 DS 完成，留 stub 测试。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from memory.important import ImportantMemoryManager
from memory.rag_store import RagEntry, RagStore, cosine_similarity


class _FakeEmbedding:
    """测试用假 embedding：md5 前 6 字节作伪向量。同 text 必相同。"""

    async def embed_one(self, text: str) -> list[float]:
        h = hashlib.md5(text.encode("utf-8")).digest()[:6]
        return [b / 255.0 for b in h]

    @property
    def dimension(self) -> int:
        return 6

    async def aclose(self) -> None:
        pass


def test_cosine_basic():
    assert cosine_similarity([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0, 0], [0, 1, 0]) == pytest.approx(0.0)
    assert cosine_similarity([1, 0, 0], [-1, 0, 0]) == pytest.approx(-1.0)
    # 边界
    assert cosine_similarity([], [1, 2]) == 0.0
    assert cosine_similarity([1, 2], [1, 2, 3]) == 0.0
    assert cosine_similarity([0, 0], [0, 0]) == 0.0


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
async def test_important_with_rag(tmp_path: Path):
    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()
    assert not im.rag_enabled

    # 未 attach 时 retrieve 走 text() 全部
    await im.save("用户喜欢猫")
    out = await im.retrieve_for_query("不相关", top_k=1)
    assert "用户喜欢猫" in out

    # attach RAG
    rs = RagStore(tmp_path / "rag.jsonl")
    await rs.load()
    im.attach_rag(_FakeEmbedding(), rs)
    assert im.rag_enabled

    # 再保存：会异步索引
    await im.save("约定每周汇报")
    assert len(rs) == 1  # 只有 attach 后的新增被索引（这是预期：attach 前的旧条目不重建索引）

    # 召回：完全相同 query 应命中
    out = await im.retrieve_for_query("约定每周汇报", top_k=1)
    assert "约定每周汇报" in out
    assert "RAG 召回" in out

    # 空 query 退回 text()
    out2 = await im.retrieve_for_query("", top_k=3)
    assert "RAG 召回" not in out2


@pytest.mark.asyncio
async def test_rag_delete_propagates(tmp_path: Path):
    # 用递增 timestamp 模拟，避免默认 now_fn 秒级精度让两条记录同 id
    counter = [0]
    def now() -> str:
        counter[0] += 1
        return f"T{counter[0]:04d}"

    im = ImportantMemoryManager(tmp_path / "imp.json", now_fn=now)
    await im.load()
    rs = RagStore(tmp_path / "rag.jsonl")
    await rs.load()
    im.attach_rag(_FakeEmbedding(), rs)

    await im.save("关于猫的事")
    await im.save("关于狗的事")
    assert len(rs) == 2

    n = await im.delete_by_keyword("猫")
    assert n == 1
    # RAG 索引也应同步删除
    assert len(rs) == 1
    assert "狗" in rs.all_entries()[0].text
