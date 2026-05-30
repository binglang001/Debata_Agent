"""测试记忆系统 —— store / history / important。"""

from __future__ import annotations

from pathlib import Path

import pytest

from memory import (
    HistoryManager,
    ImportantMemoryManager,
    JsonlStore,
    JsonStore,
    StoreError,
)


# ============================================================
# JsonStore
# ============================================================


@pytest.mark.asyncio
async def test_json_store_read_missing_returns_default(tmp_path):
    store = JsonStore(tmp_path / "x.json")
    assert await store.read(default={}) == {}
    assert await store.read(default=[]) == []
    assert await store.read(default={"a": 1}) == {"a": 1}


@pytest.mark.asyncio
async def test_json_store_write_read_roundtrip(tmp_path):
    store = JsonStore(tmp_path / "x.json")
    data = {"hello": "世界", "n": 42, "list": [1, 2, 3]}
    await store.write(data)
    assert (tmp_path / "x.json").exists()
    assert await store.read() == data


@pytest.mark.asyncio
async def test_json_store_atomic_write(tmp_path):
    """写入应原子：失败时不留下半完成的文件。"""
    store = JsonStore(tmp_path / "x.json")
    await store.write({"a": 1})
    # 第二次写入新数据
    await store.write({"a": 2})
    assert await store.read() == {"a": 2}
    # 不应有 .tmp 文件残留
    assert not (tmp_path / "x.json.tmp").exists()


@pytest.mark.asyncio
async def test_json_store_corrupted_raises(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("not valid json {", encoding="utf-8")
    store = JsonStore(path)
    with pytest.raises(StoreError):
        await store.read()


# ============================================================
# JsonlStore
# ============================================================


@pytest.mark.asyncio
async def test_jsonl_store_append_persists(tmp_path):
    path = tmp_path / "h.jsonl"
    store = JsonlStore(path)

    await store.append({"role": "user", "content": "hi"})
    await store.append({"role": "assistant", "content": "hello"})

    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert content.count("\n") == 2

    # 重新实例化读取
    store2 = JsonlStore(path)
    records = await store2.load()
    assert len(records) == 2
    assert records[0]["content"] == "hi"


@pytest.mark.asyncio
async def test_jsonl_store_append_many(tmp_path):
    store = JsonlStore(tmp_path / "h.jsonl")
    records = [{"i": i} for i in range(100)]
    await store.append_many(records)
    assert await store.length() == 100


@pytest.mark.asyncio
async def test_jsonl_store_get_slice(tmp_path):
    store = JsonlStore(tmp_path / "h.jsonl")
    await store.append_many([{"i": i} for i in range(10)])

    assert len(await store.get_slice(0, 3)) == 3
    assert len(await store.get_slice(5)) == 5
    items = await store.get_slice(2, 5)
    assert items[0]["i"] == 2
    assert items[-1]["i"] == 4


@pytest.mark.asyncio
async def test_jsonl_store_truncate_head(tmp_path):
    store = JsonlStore(tmp_path / "h.jsonl")
    await store.append_many([{"i": i} for i in range(10)])

    remaining = await store.truncate_head(3)
    assert remaining == 7
    assert await store.length() == 7

    items = await store.load()
    assert items[0]["i"] == 3

    # 磁盘上也应被重写
    content = (tmp_path / "h.jsonl").read_text(encoding="utf-8")
    assert content.count("\n") == 7


@pytest.mark.asyncio
async def test_jsonl_store_skips_corrupt_lines(tmp_path):
    path = tmp_path / "h.jsonl"
    path.write_text(
        '{"valid":1}\n'
        "not json this line\n"
        '{"valid":2}\n',
        encoding="utf-8",
    )
    store = JsonlStore(path)
    records = await store.load()
    assert len(records) == 2  # 跳过了损坏的


@pytest.mark.asyncio
async def test_jsonl_store_clear(tmp_path):
    store = JsonlStore(tmp_path / "h.jsonl")
    await store.append_many([{"i": 1}, {"i": 2}])
    await store.clear()
    assert await store.length() == 0
    assert not (tmp_path / "h.jsonl").exists()


@pytest.mark.asyncio
async def test_jsonl_store_replace_all(tmp_path):
    store = JsonlStore(tmp_path / "h.jsonl")
    await store.append_many([{"i": 1}, {"i": 2}])
    await store.replace_all([{"new": True}])

    items = await store.load()
    assert items == [{"new": True}]


# ============================================================
# HistoryManager
# ============================================================


@pytest.mark.asyncio
async def test_history_basic_operations(tmp_path):
    h = HistoryManager(tmp_path / "history.jsonl")

    await h.add_user_message("hi")
    await h.add_assistant_message("hello", tool_calls=None, reasoning_content=None)
    await h.add_assistant_message(
        "",
        tool_calls=[
            {"id": "t1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
        ],
    )
    await h.add_tool_result("t1", '{"ok": true}')
    await h.add_system_note("[发送成功]")

    assert await h.length() == 5
    records = await h.get_slice()
    assert records[0]["role"] == "user"
    assert records[1]["role"] == "assistant"
    assert "tool_calls" in records[2]
    assert records[3]["role"] == "tool"
    assert records[4]["role"] == "system"


@pytest.mark.asyncio
async def test_history_preserves_empty_reasoning_content(tmp_path):
    h = HistoryManager(tmp_path / "history.jsonl")

    await h.add_assistant_message("hello", reasoning_content="")

    records = await h.records()
    assert records == [
        {"role": "assistant", "content": "hello", "reasoning_content": ""}
    ]


@pytest.mark.asyncio
async def test_history_truncate(tmp_path):
    h = HistoryManager(tmp_path / "history.jsonl")
    for i in range(20):
        await h.add_user_message(f"msg{i}")

    remaining = await h.truncate_head(5)
    assert remaining == 15
    items = await h.get_slice()
    assert items[0]["content"] == "msg5"


@pytest.mark.asyncio
async def test_history_skip_empty_system_note(tmp_path):
    h = HistoryManager(tmp_path / "history.jsonl")
    await h.add_system_note("")
    assert await h.length() == 0


@pytest.mark.asyncio
async def test_history_add_records_batch(tmp_path):
    h = HistoryManager(tmp_path / "history.jsonl")
    await h.add_records(
        [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
    )
    assert await h.length() == 2


# ============================================================
# ImportantMemoryManager
# ============================================================


@pytest.mark.asyncio
async def test_important_load_empty(tmp_path):
    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()
    assert im.text() == ""
    assert im.items() == []


@pytest.mark.asyncio
async def test_important_save_and_read(tmp_path):
    im = ImportantMemoryManager(tmp_path / "imp.json", now_fn=lambda: "2026-05-23")
    await im.load()

    result = await im.save("张三是朋友")
    assert result == {"saved": True, "duplicate": False}

    assert "张三是朋友" in im.text()
    assert im.text().startswith("[重要记忆]")


@pytest.mark.asyncio
async def test_important_persists(tmp_path):
    im1 = ImportantMemoryManager(tmp_path / "imp.json")
    await im1.load()
    await im1.save("永久")

    im2 = ImportantMemoryManager(tmp_path / "imp.json")
    await im2.load()
    assert "永久" in im2.text()


@pytest.mark.asyncio
async def test_important_duplicate_check_skip(tmp_path):
    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()
    await im.save("张三的QQ是123")

    async def checker(items, new_text):
        return any("张三" in m["content"] for m in items)

    result = await im.save("张三确实存在", check_dup=checker)
    assert result == {"saved": False, "duplicate": True}


@pytest.mark.asyncio
async def test_important_delete_by_keyword(tmp_path):
    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()
    await im.save("张三是朋友")
    await im.save("李四是同事")
    await im.save("张三喜欢咖啡")

    deleted = await im.delete_by_keyword("张三")
    assert deleted == 2
    assert "李四" in im.text()
    assert "张三" not in im.text()


@pytest.mark.asyncio
async def test_important_delete_no_match_returns_zero(tmp_path):
    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()
    await im.save("test")
    assert await im.delete_by_keyword("nonexistent") == 0


@pytest.mark.asyncio
async def test_important_replace_all_filters_empty(tmp_path):
    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()
    await im.replace_all(
        [
            {"content": "保留"},
            {"content": ""},
            {"content": "也保留", "timestamp": "2025-01-01"},
        ]
    )
    items = im.items()
    assert len(items) == 2
    assert items[1]["timestamp"] == "2025-01-01"


@pytest.mark.asyncio
async def test_important_requires_load(tmp_path):
    im = ImportantMemoryManager(tmp_path / "imp.json")
    with pytest.raises(RuntimeError):
        await im.save("x")
    with pytest.raises(RuntimeError):
        await im.delete_by_keyword("y")


@pytest.mark.asyncio
async def test_important_empty_content_rejected(tmp_path):
    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()
    result = await im.save("")
    assert result == {"saved": False, "duplicate": False}
    result = await im.save("   ")
    assert result == {"saved": False, "duplicate": False}


@pytest.mark.asyncio
async def test_important_handles_legacy_dict_format(tmp_path):
    """旧版可能存的是非 list 格式，应被重置为空。"""
    import orjson
    path = tmp_path / "imp.json"
    path.write_bytes(orjson.dumps({"old_format": True}))

    im = ImportantMemoryManager(path)
    await im.load()
    assert im.items() == []


# ============================================================
# 并发安全
# ============================================================


@pytest.mark.asyncio
async def test_jsonl_concurrent_appends(tmp_path):
    """多个 task 并发 append 不应丢失数据或损坏文件。"""
    import asyncio
    store = JsonlStore(tmp_path / "h.jsonl")

    async def worker(idx):
        for i in range(20):
            await store.append({"worker": idx, "i": i})

    await asyncio.gather(*(worker(w) for w in range(5)))
    assert await store.length() == 100

    # 重新加载验证
    store2 = JsonlStore(tmp_path / "h.jsonl")
    assert len(await store2.load()) == 100
