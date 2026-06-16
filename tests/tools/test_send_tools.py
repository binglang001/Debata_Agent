"""Messaging send tool execution tests."""

from __future__ import annotations

from typing import Any

import pytest

from tests.tools.helpers import FakeSendAdapter, _make_config
from tools import ToolContext, build_default_registry


@pytest.mark.asyncio
async def test_send_private_sends_immediately(tmp_path):
    """send_private_messages 应即时发送并返回 msg_id。"""
    cfg = _make_config()
    reg = build_default_registry(cfg)
    emoji_dir = tmp_path / "emoji"
    emoji_dir.mkdir()

    adapter = FakeSendAdapter()
    ctx = ToolContext(
        emoji_dir=emoji_dir,
        adapter=adapter,
    )
    executor = reg.get_executor(ctx)
    result = await executor(
        "send_private_messages",
        {
            "targets": [
                {"target_qq": 12345, "content": "你好", "order": 1, "delay": 0},
                {"target_qq": 12345, "content": "在吗", "order": 2, "delay": 0},
            ]
        },
    )
    assert result["ok"] is True
    assert result["count"] == 2
    assert [item["order"] for item in result["sent"]] == [1, 2]
    assert [item["target_qq"] for item in result["sent"]] == ["12345", "12345"]
    assert [item["msg_id"] for item in result["sent"]] == ["100", "101"]
    assert result["status"] == "sent"
    assert result["qq_visible"] is True
    assert result["sent"][0]["conversation_id"] == "private:12345"
    assert result["sent"][0]["content"] == "你好"
    assert result["sent"][0]["qq_visible"] is True
    assert "sent_messages" not in result
    assert ctx.collected == []
    assert [content for _, content in adapter.sent] == ["你好", "在吗"]


@pytest.mark.asyncio
async def test_send_private_single_message_positive_delay_does_not_sleep(
    tmp_path, monkeypatch
):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    adapter = FakeSendAdapter()
    sleep_calls: list[float] = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr("tools.messaging.asyncio.sleep", fake_sleep)

    ctx = ToolContext(
        emoji_dir=tmp_path / "emoji",
        adapter=adapter,
    )
    executor = reg.get_executor(ctx)

    result = await executor(
        "send_private_messages",
        {
            "targets": [
                {"target_qq": 12345, "content": "稍等", "order": 1, "delay": 3.0},
            ]
        },
    )

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["sent"][0]["delay"] == pytest.approx(3.0)
    assert [content for _, content in adapter.sent] == ["稍等"]
    assert sleep_calls == []


@pytest.mark.asyncio
async def test_send_private_forbidden_blocked(tmp_path):
    """含 FORBIDDEN_TAGS 的内容应被拒绝。"""
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(emoji_dir=tmp_path / "emoji", adapter=FakeSendAdapter())
    executor = reg.get_executor(ctx)
    result = await executor(
        "send_private_messages",
        {
            "targets": [
                {"target_qq": 1, "content": "[私聊给 X]什么", "order": 1, "delay": 0},
            ]
        },
    )
    assert result["ok"] is True
    assert result["count"] == 0
    assert "errors" in result
    assert ctx.collected == []


@pytest.mark.asyncio
async def test_send_group_order_sorted(tmp_path):
    """send_group_message 应按 order 升序排列动作。"""
    cfg = _make_config()
    reg = build_default_registry(cfg)
    adapter = FakeSendAdapter()
    ctx = ToolContext(
        emoji_dir=tmp_path / "emoji",
        adapter=adapter,
    )
    executor = reg.get_executor(ctx)
    result = await executor(
        "send_group_message",
        {
            "group_id": 100,
            "targets": [
                {"content": "third", "order": 3, "delay": 0},
                {"content": "first", "order": 1, "delay": 0},
                {"content": "second", "order": 2, "delay": 0},
            ],
        },
    )
    contents = [content for _, content in adapter.sent]
    assert contents == ["first", "second", "third"]
    assert [item["msg_id"] for item in result["sent"]] == ["100", "101", "102"]
    assert result["qq_visible"] is True
    assert result["sent"][0]["conversation_id"] == "group:100"
    assert [item["content"] for item in result["sent"]] == [
        "first",
        "second",
        "third",
    ]
    assert "sent_messages" not in result


@pytest.mark.asyncio
async def test_send_private_keeps_small_model_delay_in_actions(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    captured: list[list[dict[str, Any]]] = []

    async def fake_send_actions(actions, source_tool, *, metadata=None):
        captured.append(actions)
        return {
            "ok": True,
            "status": "accepted",
            "qq_visible": "pending",
            "count": len(actions),
        }

    ctx = ToolContext(
        emoji_dir=tmp_path / "emoji",
        adapter=FakeSendAdapter(),
        send_actions_cb=fake_send_actions,
        typing_chars_per_second=1.0,
        typing_english_chars_per_second=5.0,
    )
    executor = reg.get_executor(ctx)

    result = await executor(
        "send_private_messages",
        {
            "targets": [
                {"target_qq": 12345, "content": "嗯", "order": 1, "delay": 0.2},
                {"target_qq": 12345, "content": "确实", "order": 2, "delay": 0.2},
            ]
        },
    )

    assert result["ok"] is True
    assert [action["delay"] for action in captured[0]] == pytest.approx([0.2, 0.2])


@pytest.mark.asyncio
async def test_send_group_keeps_small_model_delay_in_actions(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    captured: list[list[dict[str, Any]]] = []

    async def fake_send_actions(actions, source_tool, *, metadata=None):
        captured.append(actions)
        return {
            "ok": True,
            "status": "accepted",
            "qq_visible": "pending",
            "count": len(actions),
        }

    ctx = ToolContext(
        emoji_dir=tmp_path / "emoji",
        adapter=FakeSendAdapter(),
        send_actions_cb=fake_send_actions,
        typing_chars_per_second=1.0,
        typing_english_chars_per_second=5.0,
    )
    executor = reg.get_executor(ctx)

    result = await executor(
        "send_group_message",
        {
            "group_id": 100,
            "targets": [
                {"content": "helloworld", "order": 1, "delay": 0.2},
                {"content": "好", "order": 2, "delay": 0.2},
            ],
        },
    )

    assert result["ok"] is True
    assert [action["delay"] for action in captured[0]] == pytest.approx([0.2, 0.2])


@pytest.mark.asyncio
async def test_send_delay_long_text_is_not_capped_by_typing_config(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    captured: list[list[dict[str, Any]]] = []

    async def fake_send_actions(actions, source_tool, *, metadata=None):
        captured.append(actions)
        return {
            "ok": True,
            "status": "accepted",
            "qq_visible": "pending",
            "count": len(actions),
        }

    ctx = ToolContext(
        emoji_dir=tmp_path / "emoji",
        adapter=FakeSendAdapter(),
        send_actions_cb=fake_send_actions,
        typing_chars_per_second=999.0,
        typing_english_chars_per_second=999.0,
    )
    executor = reg.get_executor(ctx)

    result = await executor(
        "send_private_messages",
        {
            "targets": [
                {
                    "target_qq": 12345,
                    "content": "这是一条很长很长很长很长的消息",
                    "order": 1,
                    "delay": 12.3,
                },
                {"target_qq": 12345, "content": "第二条", "order": 2, "delay": 0},
            ]
        },
    )

    assert result["ok"] is True
    assert [action["delay"] for action in captured[0]] == pytest.approx([12.3, 0])


@pytest.mark.asyncio
async def test_send_group_emoji_uses_emoji_name_without_suffix(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    emoji_dir = tmp_path / "emoji"
    emoji_dir.mkdir()
    (emoji_dir / "无语.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    adapter = FakeSendAdapter()
    ctx = ToolContext(emoji_dir=emoji_dir, adapter=adapter)
    executor = reg.get_executor(ctx)

    result = await executor(
        "send_group_message",
        {
            "group_id": 100,
            "targets": [{"emoji": "无语", "order": 1, "delay": 0}],
        },
    )

    assert result["ok"] is True
    assert result["sent"][0]["content"] == "[表情包: 无语]"
    assert adapter.sent == []
    assert adapter.sent_images[0]["image_path"] == emoji_dir / "无语.png"


@pytest.mark.asyncio
async def test_send_group_image_is_workspace_or_url_not_emoji(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image_path = workspace / "incoming" / "a.png"
    image_path.parent.mkdir()
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    adapter = FakeSendAdapter()
    ctx = ToolContext(workspace_dir=workspace, adapter=adapter)
    executor = reg.get_executor(ctx)

    result = await executor(
        "send_group_message",
        {
            "group_id": 100,
            "targets": [{"image": "incoming/a.png", "order": 1, "delay": 0}],
        },
    )

    assert result["ok"] is True
    assert result["sent"][0]["content"] == "[图片: incoming/a.png]"
    assert adapter.sent == []
    assert adapter.sent_images[0]["image_path"] == image_path

