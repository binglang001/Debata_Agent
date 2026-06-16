"""Group administration and platform action tool tests."""

from __future__ import annotations

import pytest

from core.state import RateLimiter
from tests.tools.helpers import _approve_stub_tools, _FakeAdapter, _make_config
from tools import ToolContext, build_default_registry


@pytest.mark.asyncio
async def test_get_group_self_role_uses_current_group_and_self_id():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    adapter = _FakeAdapter()
    ctx = ToolContext(
        adapter=adapter,
        conversation_id="group:42",
        extras={"self_id": "999"},
    )
    executor = reg.get_executor(ctx)

    result = await executor("get_group_self_role", {})

    assert result["ok"] is True
    assert result["role"] == "admin"
    assert result["group_id"] == "42"
    assert adapter.api_calls == [
        (
            "get_group_member_info",
            {"group_id": 42, "user_id": 999, "no_cache": True},
        )
    ]


@pytest.mark.asyncio
async def test_group_admin_stub_requires_tool_search_before_execution():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    adapter = _FakeAdapter()
    ctx = ToolContext(adapter=adapter, extras={"self_id": "999"})
    executor = reg.get_executor(ctx)

    result = await executor(
        "set_group_ban",
        {
            "group_id": 42,
            "user_id": 123,
            "duration_seconds": 600,
            "reason": "管理员明确要求测试",
        },
    )

    assert result["ok"] is False
    assert result["status"] == "need_tool_search"
    assert adapter.api_calls == []

    details = await executor("tool_search", {"tool_name": "set_group_ban", "detail": "full"})
    assert details["ok"] is True
    assert details["risk_level"] == "high"
    assert "duration_seconds" in details["parameters_schema"]["properties"]


@pytest.mark.asyncio
async def test_group_ban_requires_bot_admin_role():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    adapter = _FakeAdapter()
    adapter.member_role = "member"
    ctx = ToolContext(adapter=adapter, extras={"self_id": "999"})
    _approve_stub_tools(ctx, "set_group_ban")
    executor = reg.get_executor(ctx)

    result = await executor(
        "set_group_ban",
        {
            "group_id": 42,
            "user_id": 123,
            "duration_seconds": 600,
            "reason": "管理员明确要求测试",
        },
    )

    assert result["ok"] is False
    assert result["status"] == "insufficient_permission"
    assert [call[0] for call in adapter.api_calls] == ["get_group_member_info"]


@pytest.mark.asyncio
async def test_group_ban_calls_napcat_api_when_admin():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    adapter = _FakeAdapter()
    ctx = ToolContext(adapter=adapter, extras={"self_id": "999"})
    _approve_stub_tools(ctx, "set_group_ban")
    executor = reg.get_executor(ctx)

    result = await executor(
        "set_group_ban",
        {
            "group_id": 42,
            "user_id": 123,
            "duration_seconds": 600,
            "reason": "管理员明确要求测试",
        },
    )

    assert result["ok"] is True
    assert result["status"] == "done"
    assert adapter.api_calls == [
        (
            "get_group_member_info",
            {"group_id": 42, "user_id": 999, "no_cache": True},
        ),
        (
            "set_group_ban",
            {"group_id": 42, "user_id": 123, "duration": 600},
        ),
    ]


@pytest.mark.asyncio
async def test_group_leave_only_allows_current_group():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    adapter = _FakeAdapter()
    ctx = ToolContext(
        adapter=adapter,
        conversation_id="group:42",
        extras={"self_id": "999"},
    )
    _approve_stub_tools(ctx, "set_group_leave")
    executor = reg.get_executor(ctx)

    result = await executor(
        "set_group_leave",
        {"group_id": 43, "reason": "用户明确要求退群"},
    )

    assert result["ok"] is False
    assert result["status"] == "target_mismatch"
    assert adapter.api_calls == []


@pytest.mark.asyncio
async def test_group_whole_ban_calls_napcat_api_when_admin():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    adapter = _FakeAdapter()
    ctx = ToolContext(adapter=adapter, extras={"self_id": "999"})
    _approve_stub_tools(ctx, "set_group_whole_ban")
    executor = reg.get_executor(ctx)

    result = await executor(
        "set_group_whole_ban",
        {"group_id": 42, "enable": True, "reason": "管理员明确要求全员禁言"},
    )

    assert result["ok"] is True
    assert adapter.api_calls[-1] == (
        "set_group_whole_ban",
        {"group_id": 42, "enable": True},
    )


@pytest.mark.asyncio
async def test_qq_action_tools_call_napcat_api():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    adapter = _FakeAdapter()
    ctx = ToolContext(adapter=adapter, conversation_id="group:42")
    executor = reg.get_executor(ctx)

    msg = await executor("get_msg", {"message_id": 123})
    poke = await executor("send_poke", {"user_id": 456})
    emoji = await executor(
        "set_msg_emoji_like",
        {"message_id": 123, "emoji_id": "76", "set": False},
    )

    assert msg["ok"] is True
    assert msg["content"] == "你好[face]"
    assert msg["data"]["conversation_id"] == "private:123"
    assert poke["ok"] is True
    assert poke["data"]["group_id"] == "42"
    assert emoji["ok"] is True
    assert emoji["data"]["set"] is False
    assert adapter.api_calls[-3:] == [
        ("get_msg", {"message_id": 123}),
        ("send_poke", {"user_id": 456, "group_id": 42}),
        ("set_msg_emoji_like", {"message_id": 123, "emoji_id": "76", "set": False}),
    ]


@pytest.mark.asyncio
async def test_send_poke_rate_limited_in_same_context():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    adapter = _FakeAdapter()
    ctx = ToolContext(
        adapter=adapter,
        conversation_id="group:42",
        extras={"now_monotonic": 10.0},
    )
    executor = reg.get_executor(ctx)

    first = await executor("send_poke", {"user_id": 456})
    second = await executor("send_poke", {"user_id": 456})

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["status"] == "rate_limited"
    assert [call[0] for call in adapter.api_calls] == ["send_poke"]


@pytest.mark.asyncio
async def test_recall_message_result_envelope():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    fake = _FakeAdapter()
    executor = reg.get_executor(ToolContext(adapter=fake))

    result = await executor("recall_message", {"message_id": 123})

    assert result["ok"] is True
    assert result["status"] == "done"
    assert result["data"]["message_id"] == "123"
    assert fake.recalled == ["123"]


@pytest.mark.asyncio
async def test_recall_message_failure_result_envelope():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    executor = reg.get_executor(ToolContext(adapter=_FakeAdapter()))

    result = await executor("recall_message", {"message_id": 999})

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "撤回失败" in result["brief"]


@pytest.mark.asyncio
async def test_request_action_tools_result_envelope():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    fake = _FakeAdapter()
    executor = reg.get_executor(ToolContext(adapter=fake))

    friend = await executor(
        "set_friend_add_request",
        {"flag": "f1", "approve": True, "remark": "熟人"},
    )
    group = await executor(
        "set_group_add_request",
        {"flag": "g1", "sub_type": "invite", "approve": False, "reason": "暂不加入"},
    )

    assert friend["ok"] is True
    assert friend["status"] == "done"
    assert friend["data"]["flag"] == "f1"
    assert group["ok"] is True
    assert group["status"] == "done"
    assert group["data"]["approve"] is False
    assert fake.friend_requests == [("f1", True, "熟人")]
    assert fake.group_requests == [("g1", "invite", False, "暂不加入")]


@pytest.mark.asyncio
async def test_friend_request_already_friend_clears_pending_and_whitelist_cache():
    class Pending:
        def __init__(self):
            self.removed: list[str] = []

        def remove(self, flag: str) -> None:
            self.removed.append(flag)

    cfg = _make_config()
    reg = build_default_registry(cfg)
    fake = _FakeAdapter()
    fake.friend_request_result = {
        "ok": True,
        "status": "already_friend",
        "already_handled": True,
        "user_id": "1001",
    }
    pending = Pending()
    limiter = RateLimiter(window_seconds=60, max_messages=0)
    limiter.remember_friend("old")
    ctx = ToolContext(
        adapter=fake,
        extras={"pending_requests": pending, "rate_limiter": limiter},
    )
    executor = reg.get_executor(ctx)

    result = await executor(
        "set_friend_add_request",
        {"flag": "f1", "approve": False, "remark": "熟人"},
    )

    assert result["ok"] is True
    assert result["status"] == "already_friend"
    assert result["data"]["already_handled"] is True
    assert pending.removed == ["f1"]
    assert limiter._whitelist_cache == {"old", "1001"}
    assert await limiter.check_and_log("1001") is False
    assert fake.friend_requests == [("f1", False, "熟人")]

