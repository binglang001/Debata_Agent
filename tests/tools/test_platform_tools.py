"""Platform and conversation retrieval tool tests."""

from __future__ import annotations

import json

import pytest

from core.chat_timeline import ChatTimelineStore
from tests.tools.helpers import (
    _approve_stub_tools,
    _FakeAdapter,
    _make_config,
    _timeline_message,
)
from tools import ToolContext, build_default_registry


@pytest.mark.asyncio
async def test_get_user_info_strips_binary_buffers():
    class FakeAdapter:
        async def get_user_info(self, user_id: str):
            return {
                "user_id": user_id,
                "nickname": "冰狼",
                "sex": "male",
                "age": 17,
                "extra": {
                    "longNick": "愿岁月清净",
                    "qqLevel": 56,
                    "richBuffer": {"0": 1, "1": 2},
                    "extBuffer": {"buf": "noise"},
                },
            }

    cfg = _make_config()
    reg = build_default_registry(cfg)
    executor = reg.get_executor(ToolContext(adapter=FakeAdapter()))
    result = await executor("get_user_info", {"user_id": 123})

    assert result["ok"] is True
    assert result["info"]["nickname"] == "冰狼"
    assert result["info"]["signature"] == "愿岁月清净"
    dumped = str(result)
    assert "richBuffer" not in dumped
    assert "extBuffer" not in dumped


@pytest.mark.asyncio
async def test_get_forward_msg_writes_nested_artifact_and_preserves_image_url(tmp_path):
    class FakeAdapter:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_forward_msg(self, forward_id: str):
            self.calls.append(forward_id)
            if forward_id == "outer":
                return [
                    {
                        "sender": {"nickname": "Lilith", "user_id": 1},
                        "raw_message": (
                            "看图 "
                            "[CQ:image,summary=&#91;动画表情&#93;,file=a.png,url=https://img.example/a.png]"
                            "[CQ:forward,id=inner]"
                        ),
                        "message_id": "m1",
                    }
                ]
            return [
                {
                    "sender": {"nickname": "Diana", "user_id": 2},
                    "content": [
                        {"type": "text", "data": {"text": "内层消息"}},
                        {
                            "type": "image",
                            "data": {
                                "summary": "截图",
                                "file": "b.jpg",
                                "url": "https://img.example/b.jpg",
                            },
                        },
                    ],
                }
            ]

    cfg = _make_config()
    reg = build_default_registry(cfg)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = FakeAdapter()
    ctx = ToolContext(
        adapter=adapter,
        workspace_dir=workspace,
    )
    executor = reg.get_executor(ctx)
    result = await executor("get_forward_msg", {"forward_id": "outer"})

    assert result["ok"] is True
    assert result["status"] == "artifact"
    assert "content" not in result
    assert result["artifact"]["type"] == "json"
    assert result["data"]["message_count"] == 2
    assert result["data"]["nested_forward_count"] == 1
    assert result["data"]["image_count"] == 2
    assert adapter.calls == ["outer", "inner"]
    path = workspace / result["artifact"]["path"]
    tree = json.loads(path.read_text(encoding="utf-8"))
    outer_segments = tree["messages"][0]["segments"]
    assert outer_segments[1]["url"] == "https://img.example/a.png"
    nested = outer_segments[2]["node"]
    assert nested["forward_id"] == "inner"
    assert nested["messages"][0]["segments"][1]["url"] == "https://img.example/b.jpg"
    assert "preview" in result
    assert "artifact.path" in result["next"]


@pytest.mark.asyncio
async def test_get_forward_msg_reads_onebot_message_field(tmp_path):
    class FakeAdapter:
        async def get_forward_msg(self, forward_id: str):
            if forward_id == "outer":
                return [
                    {
                        "sender": {"nickname": "Alice", "user_id": "1001"},
                        "message_id": "m1",
                        "message": [
                            {"type": "text", "data": {"text": "外层文字"}},
                            {"type": "forward", "data": {"id": "inner"}},
                        ],
                    }
                ]
            return [
                {
                    "sender": {"nickname": "Bob", "user_id": "1002"},
                    "message_id": "m2",
                    "message": [{"type": "text", "data": {"text": "内层文字"}}],
                }
            ]

    cfg = _make_config()
    reg = build_default_registry(cfg)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = reg.get_executor(
        ToolContext(adapter=FakeAdapter(), workspace_dir=workspace)
    )

    result = await executor("get_forward_msg", {"forward_id": "outer"})

    assert result["ok"] is True
    assert result["data"]["nested_forward_count"] == 1
    assert result["preview"][0]["segments"][0]["text"] == "外层文字"
    path = workspace / result["artifact"]["path"]
    tree = json.loads(path.read_text(encoding="utf-8"))
    outer_segments = tree["messages"][0]["segments"]
    assert outer_segments[0]["text"] == "外层文字"
    assert outer_segments[1]["forward_id"] == "inner"
    assert outer_segments[1]["node"]["messages"][0]["segments"][0]["text"] == "内层文字"


@pytest.mark.asyncio
async def test_get_forward_msg_keeps_parent_when_nested_forward_expired(tmp_path):
    class FakeAdapter:
        async def get_forward_msg(self, forward_id: str):
            if forward_id == "outer":
                return [
                    {
                        "sender": {"nickname": "Lilith"},
                        "raw_message": "[CQ:forward,id=expired-inner]",
                    }
                ]
            raise RuntimeError("API get_forward_msg 失败 (retcode=1200): 消息已过期或者为内层消息")

    cfg = _make_config()
    reg = build_default_registry(cfg)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = reg.get_executor(ToolContext(adapter=FakeAdapter(), workspace_dir=workspace))

    result = await executor("get_forward_msg", {"forward_id": "outer"})

    assert result["ok"] is True
    assert result["data"]["expired_forward_count"] == 1
    path = workspace / result["artifact"]["path"]
    tree = json.loads(path.read_text(encoding="utf-8"))
    nested = tree["messages"][0]["segments"][0]["node"]
    assert nested["status"] == "expired"
    assert nested["forward_id"] == "expired-inner"


@pytest.mark.asyncio
async def test_get_forward_msg_preserves_unescaped_summary_image_url(tmp_path):
    class Adapter:
        async def get_forward_msg(self, forward_id: str):
            return [
                {
                    "sender": {"nickname": "Alice"},
                    "raw_message": (
                        "[CQ:image,summary=[图片],file=a.jpg,"
                        "url=https://img.example/a.jpg]"
                    ),
                }
            ]

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cfg = _make_config()
    reg = build_default_registry(cfg)
    executor = reg.get_executor(ToolContext(adapter=Adapter(), workspace_dir=workspace))

    result = await executor("get_forward_msg", {"forward_id": "outer"})

    assert result["ok"] is True
    path = workspace / result["artifact"]["path"]
    tree = json.loads(path.read_text(encoding="utf-8"))
    segment = tree["messages"][0]["segments"][0]
    assert segment["summary"] == "[图片]"
    assert segment["file"] == "a.jpg"
    assert segment["url"] == "https://img.example/a.jpg"


@pytest.mark.asyncio
async def test_get_recent_chat_messages_requires_timeline():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    executor = reg.get_executor(ToolContext(conversation_id="private:123"))

    result = await executor("get_recent_chat_messages", {"limit": 5})

    assert result["ok"] is False
    assert "聊天时间线" in result["error"]


@pytest.mark.asyncio
async def test_get_recent_chat_messages_returns_inline_markdown():
    timeline = ChatTimelineStore()
    timeline.append(_timeline_message("m1", "你好"))
    timeline.append(_timeline_message("m2", "我改口"))
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(
        conversation_id="private:123",
        extras={"chat_timeline": timeline},
    )
    executor = reg.get_executor(ctx)

    result = await executor("get_recent_chat_messages", {"limit": 2})

    assert result["ok"] is True
    assert result["status"] == "inline"
    assert result["data"]["count"] == 2
    assert result["data"]["first_msg_id"] == "m1"
    assert result["data"]["last_msg_id"] == "m2"
    assert "2026-05-30 00:00:00 用户(123)：你好 [msg_id=m1]" in result["content"]
    assert "我改口" in result["content"]


@pytest.mark.asyncio
async def test_get_recent_chat_messages_writes_complete_artifact(tmp_path):
    timeline = ChatTimelineStore()
    for idx in range(20):
        timeline.append(_timeline_message(f"m{idx}", f"消息{idx} " + "很长" * 40))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(
        conversation_id="private:123",
        workspace_dir=workspace,
        extras={"chat_timeline": timeline},
        tool_result_budgets={
            "get_recent_chat_messages": {
                "inline_budget_tokens": 256,
                "artifact_threshold_tokens": 256,
                "hard_cap_tokens": 1200,
            }
        },
    )
    executor = reg.get_executor(ctx)

    result = await executor("get_recent_chat_messages", {"limit": 20})

    assert result["ok"] is True
    assert result["status"] == "artifact"
    assert "content" not in result
    assert result["data"]["count"] == 20
    assert result["data"]["first_msg_id"] == "m0"
    assert result["data"]["last_msg_id"] == "m19"
    path = workspace / result["path"]
    text = path.read_text(encoding="utf-8")
    assert "消息0" in text
    assert "消息19" in text
    assert "msg_id=m0" in text
    assert "msg_id=m19" in text
    assert "已按 token 预算截断" not in text


@pytest.mark.asyncio
async def test_recall_history_writes_complete_artifact(tmp_path):
    from memory import ArchiveStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    archive = ArchiveStore(tmp_path / "archive.sqlite3")
    await archive.append_many(
        [
            {
                "role": "user",
                "content": f"历史消息 {idx} " + ("很长" * 80),
                "conversation_id": "group:42",
                "metadata": {"timestamp": f"2026-05-30 00:{idx:02d}"},
            }
            for idx in range(12)
        ]
    )
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(
        archive=archive,
        workspace_dir=workspace,
        tool_result_budgets={
            "recall_history": {
                "inline_budget_tokens": 256,
                "artifact_threshold_tokens": 256,
                "hard_cap_tokens": 1200,
            }
        },
    )
    _approve_stub_tools(ctx, "recall_history")
    executor = reg.get_executor(ctx)

    result = await executor(
        "recall_history",
        {"conversation_id": "group:42", "limit": 12},
    )

    assert result["ok"] is True
    assert result["status"] == "artifact"
    assert "content" not in result
    assert result["artifact"]["count"] == 12
    assert result["count"] == 12
    assert "metadata" not in result["results"][0]
    text = (workspace / result["artifact"]["path"]).read_text(encoding="utf-8")
    assert "历史消息 0" in text
    assert "历史消息 11" in text
    assert "2026-05-30 00:00" in text
    assert "2026-05-30 00:11" in text
    assert "已按 token 预算截断" not in text


@pytest.mark.asyncio
async def test_upload_file_outside_whitelist_rejected(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x")

    ctx = ToolContext(adapter=_FakeAdapter(), workspace_dir=allowed)
    _approve_stub_tools(ctx, "upload_file")
    executor = reg.get_executor(ctx)
    result = await executor(
        "upload_file",
        {"target_type": "private", "target_id": 1, "file_path": str(outside)},
    )
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "上传文件失败" in result["brief"]
    assert "workspace" in result["error"]


@pytest.mark.asyncio
async def test_upload_file_inside_whitelist_ok(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    inside = allowed / "x.txt"
    inside.write_text("x")

    fake = _FakeAdapter()
    ctx = ToolContext(adapter=fake, workspace_dir=allowed)
    _approve_stub_tools(ctx, "upload_file")
    executor = reg.get_executor(ctx)
    result = await executor(
        "upload_file",
        {"target_type": "group", "target_id": 1, "file_path": str(inside)},
    )
    assert result["ok"] is True
    assert result["status"] == "done"
    assert result["data"]["file_name"] == "x.txt"
    assert result["data"]["target_type"] == "group"
    assert len(fake.uploaded) == 1
    assert fake.uploaded[0][2] == "x.txt"


@pytest.mark.asyncio
async def test_upload_file_no_adapter():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext()  # 没 adapter
    _approve_stub_tools(ctx, "upload_file")
    executor = reg.get_executor(ctx)
    result = await executor(
        "upload_file",
        {"target_type": "private", "target_id": 1, "file_path": "/tmp/x"},
    )
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "未连接适配器" in result["brief"]


@pytest.mark.asyncio
async def test_list_contacts_no_adapter():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext()
    executor = reg.get_executor(ctx)
    result = await executor("list_contacts", {"scope": "friends"})
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_list_contacts_with_adapter():
    """以伪适配器验证 list_friends 转 dict。"""
    from adapters.types import FriendInfo

    class FakeAd:
        name = "fake"
        is_connected = True

        async def list_friends(self):
            return [FriendInfo(user_id="1", nickname="A")]

    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(adapter=FakeAd())  # type: ignore
    executor = reg.get_executor(ctx)
    result = await executor("list_contacts", {"scope": "friends"})
    assert result["ok"] is True
    assert result["status"] == "inline"
    assert result["count"] == 1
    assert result["friends"][0]["nickname"] == "A"


@pytest.mark.asyncio
async def test_list_contacts_returns_explicit_pages():
    """联系人列表按 offset/limit 显式分页，不依赖压缩器截断。"""
    from adapters.types import FriendInfo

    class FakeAd:
        name = "fake"
        is_connected = True

        async def list_friends(self):
            return [FriendInfo(user_id=str(i), nickname=f"F{i}") for i in range(60)]

    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(adapter=FakeAd())  # type: ignore
    executor = reg.get_executor(ctx)
    result = await executor("list_contacts", {"scope": "friends", "limit": 50})

    assert result["ok"] is True
    assert result["count"] == 60
    assert len(result["friends"]) == 50
    assert result["next_offset"] == 50
    assert "_condensed" not in result

    second = await executor(
        "list_contacts",
        {"scope": "friends", "limit": 50, "offset": result["next_offset"]},
    )
    assert len(second["friends"]) == 10
    assert "next_offset" not in second


@pytest.mark.asyncio
async def test_list_contacts_group_members_requires_group_id():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(adapter=object())  # type: ignore  # 仅占位
    executor = reg.get_executor(ctx)
    result = await executor("list_contacts", {"scope": "group_members"})
    assert result["ok"] is False
    assert "group_id" in result["error"]

