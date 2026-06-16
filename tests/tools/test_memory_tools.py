"""Memory tool execution tests."""

from __future__ import annotations

import pytest

from tests.tools.helpers import _make_config
from tools import ToolContext, build_default_registry


@pytest.mark.asyncio
async def test_save_memory_no_manager():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext()
    executor = reg.get_executor(ctx)
    result = await executor("save_important_memory", {"memory_text": "x"})
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_save_memory_with_manager(tmp_path):
    from memory import ImportantMemoryManager

    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()

    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(important=im, conversation_id="private:123")
    executor = reg.get_executor(ctx)
    missing_scope = await executor(
        "save_important_memory", {"memory_text": "记住张三是朋友"}
    )
    assert missing_scope["ok"] is False
    assert missing_scope["status"] == "missing_scope"
    assert missing_scope["retryable"] is True
    assert "显式填写 scope" in missing_scope["brief"]
    assert "提到某用户不等于 user scope" in missing_scope["next"]
    assert "global" in missing_scope["data"]["allowed_scopes"]
    assert "冰狼正在做短中期项目" in missing_scope["data"]["examples"][1]
    assert im.items() == []

    result = await executor(
        "save_important_memory",
        {"memory_text": "记住张三是朋友", "scope": "user:123"},
    )
    assert result["ok"] is True
    assert result["saved"] is True
    assert result["memory_id"].startswith("mem_")
    assert result["data"]["memory_id"] == result["memory_id"]
    assert result["scope"] == "user:123"
    assert len(im.items()) == 1
    assert im.items()[0]["id"] == result["memory_id"]
    assert im.items()[0]["scope"] == "user:123"

    duplicate = await executor(
        "save_important_memory",
        {"memory_text": "记住张三是朋友", "scope": "user:123"},
    )
    assert duplicate["ok"] is True
    assert duplicate["status"] == "exact_duplicate"
    assert duplicate["saved"] is False
    assert duplicate["existing_id"] == result["memory_id"]


@pytest.mark.asyncio
async def test_save_memory_explicit_scope_and_pinned(tmp_path):
    from memory import ImportantMemoryManager

    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()

    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(important=im, conversation_id="private:123")
    executor = reg.get_executor(ctx)
    result = await executor(
        "save_important_memory",
        {"memory_text": "全局稳定约定", "scope": "global", "pinned": True},
    )

    assert result["ok"] is True
    assert result["scope"] == "global"
    assert result["pinned"] is True
    assert im.items()[0]["scope"] == "global"
    assert im.items()[0]["pinned"] is True


@pytest.mark.asyncio
async def test_save_memory_rejects_private_scope_without_saving(tmp_path):
    from memory import ImportantMemoryManager

    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()

    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(important=im, conversation_id="private:123")
    executor = reg.get_executor(ctx)
    result = await executor(
        "save_important_memory",
        {"memory_text": "张三喜欢红茶", "scope": "private:123"},
    )

    assert result["ok"] is False
    assert result["status"] == "invalid_scope"
    assert result["retryable"] is True
    assert "不能写 private:QQ" in result["brief"]
    assert result["data"]["raw_scope"] == "private:123"
    assert im.items() == []


@pytest.mark.asyncio
async def test_update_memory_with_manager(tmp_path):
    from memory import ImportantMemoryManager

    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()
    await im.replace_all([{"id": "mem-1", "timestamp": "T1", "content": "张三是朋友"}])

    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(important=im)
    executor = reg.get_executor(ctx)
    result = await executor(
        "update_important_memory",
        {
            "memory_id": "mem-1",
            "memory_text": "张三是朋友，生日是7月8日",
            "reason": "补充生日",
        },
    )

    assert result["ok"] is True
    assert result["updated"] is True
    assert result["memory_id"] == "mem-1"
    assert im.items()[0]["content"] == "张三是朋友，生日是7月8日"


@pytest.mark.asyncio
async def test_update_memory_exact_duplicate_returns_existing_id(tmp_path):
    from memory import ImportantMemoryManager

    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()
    await im.replace_all(
        [
            {"id": "mem-1", "timestamp": "T1", "content": "张三是朋友"},
            {"id": "mem-2", "timestamp": "T2", "content": "李四是朋友"},
        ]
    )

    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(important=im)
    executor = reg.get_executor(ctx)
    result = await executor(
        "update_important_memory",
        {"memory_id": "mem-2", "memory_text": "张三是朋友"},
    )

    assert result["ok"] is True
    assert result["status"] == "exact_duplicate"
    assert result["updated"] is False
    assert result["existing_id"] == "mem-1"
    assert im.items()[1]["content"] == "李四是朋友"


@pytest.mark.asyncio
async def test_delete_memory_with_manager(tmp_path):
    from memory import ImportantMemoryManager

    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()
    saved = await im.save("张三是朋友")
    await im.save("张三喜欢咖啡")

    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(important=im)
    executor = reg.get_executor(ctx)
    result = await executor(
        "delete_important_memory", {"memory_id": saved["id"], "keyword": ""}
    )
    assert result["ok"] is True
    assert result["deleted"] == 1
    assert result["memory_id"] == saved["id"]
    assert "张三是朋友" not in im.text()
    assert "张三喜欢咖啡" in im.text()

    missing = await executor(
        "delete_important_memory", {"memory_id": "mem_missing"}
    )
    assert missing["ok"] is False
    assert missing["status"] == "not_found"
    assert missing["deleted"] == 0


@pytest.mark.asyncio
async def test_delete_memory_keyword_legacy_compat(tmp_path):
    from memory import ImportantMemoryManager

    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()
    await im.save("张三是朋友")
    await im.save("李四是同事")

    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(important=im)
    executor = reg.get_executor(ctx)
    result = await executor(
        "delete_important_memory", {"memory_id": "", "keyword": "张三"}
    )

    assert result["ok"] is True
    assert result["status"] == "legacy_keyword"
    assert result["deleted"] == 1


@pytest.mark.asyncio
async def test_rag_mode_memory_tools_execute_with_manager(tmp_path):
    from memory import ImportantMemoryManager

    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()

    cfg = _make_config(memory_mode="rag")
    reg = build_default_registry(cfg)
    ctx = ToolContext(important=im, conversation_id="group:42")
    executor = reg.get_executor(ctx)

    saved = await executor(
        "save_important_memory",
        {"memory_text": "测试群42有固定茶会", "scope": "group:42"},
    )
    item_id = saved["memory_id"]
    updated = await executor(
        "update_important_memory",
        {
            "memory_id": item_id,
            "memory_text": "测试群42有固定茶会，时间是周五",
            "reason": "补充时间",
        },
    )
    deleted = await executor("delete_important_memory", {"memory_id": item_id})

    assert saved["saved"] is True
    assert updated["updated"] is True
    assert updated["memory_id"] == item_id
    assert deleted["deleted"] == 1
    assert im.items() == []

