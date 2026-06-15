"""测试 NapCat 事件解析。"""

from __future__ import annotations

from adapters.napcat.events import parse_napcat_event
from adapters.types import (
    EventType,
    IncomingMessage,
    IncomingNotice,
    IncomingRequest,
    MediaType,
    MetaEvent,
    NoticeType,
    RequestType,
)
from utils.cq_parser import parse_raw_cq as _parse_raw_message

# ============================================================
# Message 解析
# ============================================================


def test_parse_private_message_basic():
    raw = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 12345,
        "user_id": 1001,
        "self_id": 9999,
        "time": 1234567890,
        "sub_type": "friend",
        "raw_message": "你好",
        "message": [{"type": "text", "data": {"text": "你好"}}],
        "sender": {"user_id": 1001, "nickname": "Alice"},
    }
    event = parse_napcat_event("nc", raw)
    assert isinstance(event, IncomingMessage)
    assert event.scope == "private"
    assert event.user_id == "1001"
    assert event.nickname == "Alice"
    assert event.message_id == "12345"
    assert event.text == "你好"
    assert event.group_id is None
    assert event.event_type == EventType.MESSAGE


def test_parse_group_message_with_card():
    raw = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 11111,
        "group_id": 200,
        "user_id": 1001,
        "self_id": 9999,
        "time": 1234567890,
        "raw_message": "hi",
        "message": [{"type": "text", "data": {"text": "hi"}}],
        "sender": {"user_id": 1001, "nickname": "Alice", "card": "Captain"},
    }
    event = parse_napcat_event("nc", raw)
    assert isinstance(event, IncomingMessage)
    assert event.is_group()
    assert event.group_id == "200"
    assert event.nickname == "Captain"  # 群名片优先


def test_parse_group_message_without_card_falls_back():
    raw = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 1,
        "group_id": 200,
        "user_id": 1001,
        "self_id": 9999,
        "time": 1,
        "raw_message": "hi",
        "message": [],
        "sender": {"user_id": 1001, "nickname": "Alice", "card": ""},
    }
    event = parse_napcat_event("nc", raw)
    assert event.nickname == "Alice"


def test_parse_message_with_image():
    raw = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 1,
        "user_id": 1001,
        "self_id": 9999,
        "time": 1,
        "raw_message": "看图[CQ:image,file=abc.jpg]",
        "message": [
            {"type": "text", "data": {"text": "看图"}},
            {"type": "image", "data": {"url": "http://x/y.jpg", "file": "abc.jpg"}},
        ],
        "sender": {"user_id": 1001, "nickname": "Alice"},
    }
    event = parse_napcat_event("nc", raw)
    assert len(event.media) == 1
    assert event.media[0].type == MediaType.IMAGE
    assert event.media[0].url == "http://x/y.jpg"


def test_parse_raw_cq_image_url_unescapes_html_entities():
    raw = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 1,
        "user_id": 1001,
        "self_id": 9999,
        "time": 1,
        "raw_message": (
            "[CQ:image,file=abc.jpg,"
            "url=https://multimedia.nt.qq.com.cn/download?"
            "appid=1407&amp;fileid=x&amp;rkey=y]"
        ),
        "message": (
            "[CQ:image,file=abc.jpg,"
            "url=https://multimedia.nt.qq.com.cn/download?"
            "appid=1407&amp;fileid=x&amp;rkey=y]"
        ),
        "sender": {"user_id": 1001, "nickname": "Alice"},
    }

    event = parse_napcat_event("nc", raw)

    assert event is not None
    assert event.media[0].type == MediaType.IMAGE
    assert event.media[0].url == (
        "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=x&rkey=y"
    )


def test_parse_message_with_voice_and_file():
    raw = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 1,
        "user_id": 1001,
        "self_id": 9999,
        "time": 1,
        "raw_message": "[CQ:record,file=v.silk][CQ:file,file=f.zip,file_name=name.zip]",
        "message": [
            {"type": "record", "data": {"file": "v.silk"}},
            {"type": "file", "data": {"file": "f.zip", "file_name": "name.zip"}},
        ],
        "sender": {"user_id": 1001, "nickname": "Alice"},
    }
    event = parse_napcat_event("nc", raw)
    types = [m.type for m in event.media]
    assert MediaType.VOICE in types
    assert MediaType.FILE in types


def test_parse_message_string_file_cq_with_local_path():
    raw = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 1,
        "user_id": 1001,
        "self_id": 9999,
        "time": 1,
        "raw_message": "[CQ:file,file=abc,file_name=问卷.pdf,url=D:\\QQ_data\\Tencent Files\\NapCat\\temp\\问卷.pdf]",
        "message": "[CQ:file,file=abc,file_name=问卷.pdf,url=D:\\QQ_data\\Tencent Files\\NapCat\\temp\\问卷.pdf]",
        "sender": {"user_id": 1001, "nickname": "Alice"},
    }

    event = parse_napcat_event("nc", raw)

    assert event is not None
    assert event.media[0].type == MediaType.FILE
    assert event.media[0].file_id == "abc"
    assert event.media[0].name == "问卷.pdf"
    assert event.media[0].url == "D:\\QQ_data\\Tencent Files\\NapCat\\temp\\问卷.pdf"


def test_parse_message_with_reply():
    raw = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 1,
        "user_id": 1001,
        "self_id": 9999,
        "time": 1,
        "raw_message": "[CQ:reply,id=999]回复",
        "message": [
            {"type": "reply", "data": {"id": "999"}},
            {"type": "text", "data": {"text": "回复"}},
        ],
        "sender": {"user_id": 1001, "nickname": "A"},
    }
    event = parse_napcat_event("nc", raw)
    assert event.reply_to == "999"


def test_parse_message_structured_forward_when_raw_message_empty():
    raw = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 1,
        "user_id": 1001,
        "self_id": 9999,
        "time": 1,
        "raw_message": "",
        "message": [
            {
                "type": "forward",
                "data": {"id": "fwd123", "title": "聊天记录"},
            }
        ],
        "sender": {"user_id": 1001, "nickname": "Alice"},
    }

    event = parse_napcat_event("nc", raw)

    assert isinstance(event, IncomingMessage)
    assert event.text == "[合并转发 id=fwd123 title=聊天记录]"
    assert len(event.media) == 1
    assert event.media[0].type == MediaType.FORWARD
    assert event.media[0].file_id == "fwd123"
    assert event.media[0].extra["title"] == "聊天记录"


def test_parse_message_string_forward_cq_extracts_media_and_title():
    raw = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 1,
        "user_id": 1001,
        "self_id": 9999,
        "time": 1,
        "raw_message": "[CQ:forward,id=fwd123,title=聊天记录]",
        "message": "[CQ:forward,id=fwd123,title=聊天记录]",
        "sender": {"user_id": 1001, "nickname": "Alice"},
    }

    event = parse_napcat_event("nc", raw)

    assert isinstance(event, IncomingMessage)
    assert event.text == "[合并转发 id=fwd123]"
    assert len(event.media) == 1
    assert event.media[0].type == MediaType.FORWARD
    assert event.media[0].file_id == "fwd123"
    assert event.media[0].extra["title"] == "聊天记录"


def test_parse_message_missing_fields_returns_none():
    raw = {"post_type": "message", "message_type": "private"}  # 缺关键字段
    event = parse_napcat_event("nc", raw)
    assert event is None


# ============================================================
# Notice 解析
# ============================================================


def test_parse_group_recall():
    raw = {
        "post_type": "notice",
        "notice_type": "group_recall",
        "group_id": 200,
        "user_id": 1001,
        "operator_id": 1002,
        "message_id": 555,
        "self_id": 9999,
        "time": 1,
    }
    event = parse_napcat_event("nc", raw)
    assert isinstance(event, IncomingNotice)
    assert event.notice_type == NoticeType.GROUP_RECALL
    assert event.group_id == "200"
    assert event.operator_id == "1002"
    assert event.message_id == "555"


def test_parse_friend_recall():
    raw = {
        "post_type": "notice",
        "notice_type": "friend_recall",
        "user_id": 1001,
        "message_id": 555,
        "self_id": 9999,
        "time": 1,
    }
    event = parse_napcat_event("nc", raw)
    assert event.notice_type == NoticeType.FRIEND_RECALL


def test_parse_unknown_notice_falls_back_to_other():
    raw = {
        "post_type": "notice",
        "notice_type": "some_new_notice",
        "self_id": 1,
        "time": 1,
    }
    event = parse_napcat_event("nc", raw)
    assert event.notice_type == NoticeType.OTHER


# ============================================================
# Request 解析
# ============================================================


def test_parse_friend_request():
    raw = {
        "post_type": "request",
        "request_type": "friend",
        "user_id": 1001,
        "comment": "想加你",
        "flag": "abc123",
        "self_id": 9999,
        "time": 1,
    }
    event = parse_napcat_event("nc", raw)
    assert isinstance(event, IncomingRequest)
    assert event.request_type == RequestType.FRIEND
    assert event.flag == "abc123"
    assert event.comment == "想加你"


def test_parse_group_add_request():
    raw = {
        "post_type": "request",
        "request_type": "group",
        "sub_type": "add",
        "group_id": 200,
        "user_id": 1001,
        "comment": "求通过",
        "flag": "xyz",
        "self_id": 9999,
        "time": 1,
    }
    event = parse_napcat_event("nc", raw)
    assert event.request_type == RequestType.GROUP_ADD


def test_parse_group_invite_request():
    raw = {
        "post_type": "request",
        "request_type": "group",
        "sub_type": "invite",
        "group_id": 200,
        "user_id": 1001,
        "comment": "",
        "flag": "xyz",
        "self_id": 9999,
        "time": 1,
    }
    event = parse_napcat_event("nc", raw)
    assert event.request_type == RequestType.GROUP_INVITE


def test_parse_request_missing_flag_returns_none():
    raw = {
        "post_type": "request",
        "request_type": "friend",
        "user_id": 1001,
        "self_id": 9999,
        "time": 1,
    }
    event = parse_napcat_event("nc", raw)
    assert event is None


# ============================================================
# Meta 事件
# ============================================================


def test_parse_meta_lifecycle():
    raw = {
        "post_type": "meta_event",
        "meta_event_type": "lifecycle",
        "sub_type": "connect",
        "self_id": 9999,
        "time": 1,
    }
    event = parse_napcat_event("nc", raw)
    assert isinstance(event, MetaEvent)
    assert event.meta_type == "lifecycle"
    assert event.sub_type == "connect"


# ============================================================
# API 响应（含 echo）应被忽略
# ============================================================


def test_api_response_with_echo_returns_none():
    raw = {"status": "ok", "retcode": 0, "data": {}, "echo": "abc"}
    event = parse_napcat_event("nc", raw)
    assert event is None


# ============================================================
# CQ 码解析单元测试
# ============================================================


def test_cq_parser_basic_text():
    assert _parse_raw_message("hello", "9999") == "hello"


def test_cq_parser_at_self():
    assert _parse_raw_message("[CQ:at,qq=9999]在吗", "9999") == "@我在吗"


def test_cq_parser_at_other():
    assert _parse_raw_message("[CQ:at,qq=1234]hi", "9999") == "@QQ1234hi"


def test_cq_parser_at_all():
    assert _parse_raw_message("[CQ:at,qq=all]注意", "9999") == "@全体成员注意"


def test_cq_parser_reply_moved_to_front():
    # reply 应被提取到开头
    out = _parse_raw_message("正文[CQ:reply,id=555]", "9999")
    assert out.startswith("[引用msg_id=555]")
    assert "正文" in out


def test_cq_parser_image_voice_video_placeholders():
    assert _parse_raw_message("[CQ:image,file=x.jpg]", "9999") == "[图片]"
    assert _parse_raw_message("[CQ:record,file=v.silk]", "9999") == "[语音]"
    assert _parse_raw_message("[CQ:video,file=v.mp4]", "9999") == "[视频]"


def test_cq_parser_face():
    assert _parse_raw_message("[CQ:face,id=21]", "9999") == "[表情21]"


def test_cq_parser_forward():
    assert _parse_raw_message("[CQ:forward,id=fwd123]", "9999") == "[合并转发 id=fwd123]"


def test_cq_parser_preserves_repeated_at():
    """绕过 NoneBot 合并优化：连续相同 @ 应保留。"""
    out = _parse_raw_message("[CQ:at,qq=1234][CQ:at,qq=1234]都来", "9999")
    assert out == "@QQ1234@QQ1234都来"


def test_cq_parser_mixed():
    raw = "[CQ:reply,id=99][CQ:at,qq=1234]你看[CQ:image,file=x.jpg][CQ:face,id=13]"
    out = _parse_raw_message(raw, "9999")
    assert out.startswith("[引用msg_id=99]")
    assert "@QQ1234" in out
    assert "[图片]" in out
    assert "[表情13]" in out


def test_cq_parser_unknown_type():
    """未知 CQ 类型应保留为占位。"""
    out = _parse_raw_message("[CQ:unknown,x=1]后续", "9999")
    assert "[unknown]" in out


def test_cq_parser_malformed_returns_remainder():
    """未闭合的 CQ 标签按字面保留。"""
    out = _parse_raw_message("[CQ:image,file=x", "9999")
    assert "[CQ:image" in out
