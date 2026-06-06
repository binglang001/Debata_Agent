from __future__ import annotations

from adapters.types import IncomingMessage, MediaSegment, MediaType
from core.chat_timeline import ChatTimelineMessage, ChatTimelineStore


def _incoming(
    *,
    message_id: str,
    text: str,
    raw_message: str | None = None,
    media: list[MediaSegment] | None = None,
    timestamp: float = 1_780_000_000.0,
    reply_to: str | None = None,
) -> IncomingMessage:
    return IncomingMessage(
        adapter="fake",
        timestamp=timestamp,
        self_id="999",
        message_id=message_id,
        scope="private",
        user_id="123",
        nickname="用户",
        text=text,
        raw_message=raw_message if raw_message is not None else text,
        media=media or [],
        reply_to=reply_to,
    )


def _timeline_message(message_id: str, text: str) -> ChatTimelineMessage:
    return ChatTimelineMessage(
        conversation_id="private:123",
        direction="inbound",
        timestamp=1_780_000_000.0,
        time_text="2026-05-30 00:00:00",
        sender_name="用户",
        sender_id="123",
        target_id="123",
        group_id=None,
        msg_id=message_id,
        text=text,
        raw_message=text,
    )


def test_inbound_markdown_compacts_image_url_and_preserves_forward_id():
    store = ChatTimelineStore()
    raw = (
        "[CQ:image,summary=[图片],file=abc.jpg,url=https://example.com/a.png]"
        "[CQ:forward,id=forward-1]"
    )
    store.append_inbound_event(
        _incoming(
            message_id="m1",
            text="[图片] [合并转发 id=forward-1]",
            raw_message=raw,
            media=[
                MediaSegment(
                    type=MediaType.IMAGE,
                    file_id="abc.jpg",
                    url="https://example.com/a.png",
                ),
                MediaSegment(type=MediaType.FORWARD, file_id="forward-1"),
            ],
        ),
        conversation_id="private:123",
        text="[图片] [合并转发 id=forward-1]",
        timestamp=1_780_000_000.0,
    )

    markdown = store.to_markdown(store.recent("private:123", 10))

    assert "用户(123)" in markdown
    assert "[图片]" in markdown
    assert "https://example.com/a.png" not in markdown
    assert "forward-1" in markdown
    assert "msg_id=m1" in markdown


def test_inbound_markdown_includes_reply_to():
    store = ChatTimelineStore()
    store.append_inbound_event(
        _incoming(message_id="m2", text="回复", reply_to="m1"),
        conversation_id="private:123",
        text="回复",
        timestamp=1_780_000_000.0,
    )

    markdown = store.to_markdown(store.recent("private:123", 10))

    assert "reply_to=m1" in markdown
    assert "msg_id=m2" in markdown


def test_raw_cq_parser_preserves_url_after_unescaped_summary():
    store = ChatTimelineStore()
    raw = "[CQ:image,summary=[图片],file=abc.jpg,url=https://example.com/a.png]"
    store.append_inbound_event(
        _incoming(
            message_id="m1",
            text="[图片]",
            raw_message=raw,
            media=[],
        ),
        conversation_id="private:123",
        text="",
        timestamp=1_780_000_000.0,
    )

    markdown = store.to_markdown(store.recent("private:123", 10), compact=False)

    assert "summary=[图片]" in markdown
    assert "file=abc.jpg" in markdown
    assert "url=https://example.com/a.png" in markdown


def test_sliding_window_keeps_latest_messages_only():
    store = ChatTimelineStore(max_per_conversation=3)
    for idx in range(5):
        store.append(_timeline_message(f"m{idx}", f"消息{idx}"))

    messages = store.recent("private:123", 10)

    assert [item.msg_id for item in messages] == ["m2", "m3", "m4"]


def test_recent_since_and_before_msg_id_return_continuous_windows():
    store = ChatTimelineStore(max_per_conversation=10)
    for idx in range(5):
        store.append(_timeline_message(f"m{idx}", f"消息{idx}"))

    since = store.recent("private:123", 10, since_msg_id="m1")
    before = store.recent("private:123", 10, before_msg_id="m4")

    assert [item.msg_id for item in since] == ["m2", "m3", "m4"]
    assert [item.msg_id for item in before] == ["m0", "m1", "m2", "m3"]
