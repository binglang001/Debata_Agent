"""Basic message flow integration pipeline tests."""

from __future__ import annotations

import pytest

from core.state import PendingMessageItem, RateLimiter
from tests.integration_pipeline.helpers import (
    _ai_no_action,
    _ai_send_group,
    _ai_send_private,
    _drain_pipeline,
    _msg,
)


@pytest.mark.asyncio

async def test_slash_hash_group_messages_are_not_explicit_batch_triggers(build_pipeline):

    pipeline, _, _, _, _ = await build_pipeline([])

    events = [

        _msg(user_id="111", group_id="5555", text="/other", message_id="m-slash"),

        _msg(user_id="222", group_id="5555", text="#topic", message_id="m-hash"),

        _msg(user_id="333", group_id="5555", text="普通接话", message_id="m-normal"),

    ]

    items = [

        PendingMessageItem(

            message_id=event.message_id,

            user_id=event.user_id,

            nickname=event.nickname,

            location=f"群聊 {event.group_id}",

            text=event.text,

            conversation_id=f"group:{event.group_id}",

            inbound_seq=index,

            received_at=1.0,

            raw_event=event,

        )

        for index, event in enumerate(events, start=1)

    ]

    trigger_ids = pipeline._batch_trigger_message_ids(items)

    focus_ids = pipeline._batch_focus_user_ids(items, trigger_ids)

    assert trigger_ids == ["m-normal"]

    assert focus_ids == ["333"]

@pytest.mark.asyncio

async def test_basic_private_message_flow(build_pipeline):

    """私聊：enqueue → batch → chat_agent → tool → adapter.send_text。

    这条链路全程不抛错，意味着所有跨模块接口名/字段名一致。"""

    pipeline, provider, adapter, history, _ = await build_pipeline(

        [_ai_send_private(target_qq="123", content="收到，你好~")]

    )

    await pipeline.enqueue(_msg(user_id="123", text="你好"))

    await _drain_pipeline(pipeline)

    # adapter 收到了对应发送

    assert len(adapter.sent) == 1, f"expected 1 send, got {adapter.sent}"

    target, content = adapter.sent[0]

    assert target.scope == "private"

    assert target.target_id == "123"

    assert content == "收到，你好~"

    # history 不仅写了 user/assistant，且 records() 别名能用

    records = await history.records()

    roles = [r.get("role") for r in records]

    assert "user" in roles

    assert "assistant" in roles

    # 发送后还会把工具结果交还给模型，由 no_action 明确收尾。

    assert len(provider.calls) == 2

    call_msgs = provider.calls[0]["messages"]

    assert call_msgs[0]["role"] == "system"

    assert any(m["role"] == "user" for m in call_msgs)

@pytest.mark.asyncio

async def test_group_message_flow(build_pipeline):

    """群聊：send_group_message 路径走通，scope=group。"""

    pipeline, _, adapter, _, _ = await build_pipeline([_ai_send_group("5555", "群好")])

    await pipeline.enqueue(_msg(user_id="9", group_id="5555", text="在吗"))

    await _drain_pipeline(pipeline)

    assert any(

        t.scope == "group" and t.target_id == "5555" and c == "群好"

        for t, c in adapter.sent

    ), f"群消息未发出: {adapter.sent}"

@pytest.mark.asyncio

async def test_group_messages_bypass_private_rate_limiter(build_pipeline):

    """群聊由群白名单/审核控制，不应套用陌生私聊频控。"""

    limiter = RateLimiter(window_seconds=60, max_messages=0)

    pipeline, provider, adapter, _, _ = await build_pipeline(

        [_ai_send_group("5555", "群内正常回复")],

        rate_limiter=limiter,

    )

    await pipeline.enqueue(_msg(user_id="stranger", group_id="5555", text="群里说话"))

    await _drain_pipeline(pipeline)

    assert len(provider.calls) == 2

    assert [content for _, content in adapter.sent] == ["群内正常回复"]

@pytest.mark.asyncio

async def test_merge_window_groups_by_conversation(build_pipeline):

    """同一合并窗口内的私聊和群聊应拆成两轮，目标不串。"""

    pipeline, provider, adapter, history, _ = await build_pipeline(

        [

            _ai_send_private(target_qq="123", content="私聊回复"),

            _ai_no_action(),

            _ai_send_group("5555", "群聊回复"),

        ]

    )

    await pipeline.enqueue(_msg(user_id="123", text="私聊消息", message_id="p1"))

    await pipeline.enqueue(

        _msg(user_id="9", group_id="5555", text="群消息", message_id="g1")

    )

    await _drain_pipeline(pipeline)

    assert [(t.scope, t.target_id, c) for t, c in adapter.sent] == [

        ("private", "123", "私聊回复"),

        ("group", "5555", "群聊回复"),

    ]

    assert len(provider.calls) == 4

    call1 = "\n".join(str(m.get("content", "")) for m in provider.calls[0]["messages"])

    call2 = "\n".join(str(m.get("content", "")) for m in provider.calls[2]["messages"])

    assert "当前会话：private:123" in call1

    assert "当前会话：group:5555" in call2

    records = await history.records()

    convs = [r.get("conversation_id") for r in records if r.get("role") == "user"]

    assert "private:123" in convs

    assert "group:5555" in convs
