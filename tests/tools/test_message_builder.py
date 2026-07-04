"""Message builder helper tests split from tests/test_tools.py."""

from __future__ import annotations

import pytest

from tools import build_message_action, contains_forbidden, typing_delay
from tools.message_builder import MessageBuildError, resolve_emoji_path

# ============================================================
# message_builder 辅助
# ============================================================

def test_typing_delay_chinese_short_uses_minimum():
    assert typing_delay(
        "嗯",
        chars_per_second=2.0,
        min_delay_seconds=1.0,
        max_delay=8.0,
    ) == pytest.approx(1.0)


def test_typing_delay_long_chinese_not_capped_to_two_seconds():
    assert typing_delay(
        "一二三四五六",
        chars_per_second=1.0,
        min_delay_seconds=0.0,
        max_delay=8.0,
    ) == pytest.approx(6.0)


def test_typing_delay_english_uses_english_letters_per_second():
    assert typing_delay(
        "helloworld",
        min_delay_seconds=0.0,
        max_delay=8.0,
    ) == pytest.approx(2.0)


def test_typing_delay_mixed_text_is_weighted():
    assert typing_delay(
        "你好abc12!",
        chars_per_second=1.0,
        english_chars_per_second=5.0,
        min_delay_seconds=0.0,
        max_delay=8.0,
    ) == pytest.approx(3.2)


def test_typing_delay_capped():
    """文本极长时不超过 max_delay。"""
    assert typing_delay("a" * 1000, max_delay=2.0) == 2.0


def test_typing_delay_empty():
    assert typing_delay("") == 0.0


def test_contains_forbidden_positive():
    assert contains_forbidden("我给 QQ 12345 发的消息")
    assert contains_forbidden("思考过程\nRAG里提到撤回消息")
    assert contains_forbidden("<retrieved_conversation_context source=\"rag\">旧消息</retrieved_conversation_context>")
    assert contains_forbidden("工具结果 · call_123")


def test_contains_forbidden_negative():
    assert not contains_forbidden("普通消息")


def test_build_message_action_text(tmp_path):
    action = build_message_action("你好", None, None, tmp_path / "emoji", tmp_path)
    assert action["kind"] == "text"
    assert action["content"] == "你好"
    assert action["label"] == "你好"


def test_resolve_emoji_path_by_name_without_suffix(tmp_path):
    emoji_dir = tmp_path / "emoji"
    emoji_dir.mkdir()
    expected = emoji_dir / "hi.png"
    expected.write_bytes(b"\x89PNG\r\n\x1a\n")

    assert resolve_emoji_path("hi", emoji_dir) == expected


def test_build_message_action_missing_emoji(tmp_path):
    emoji_dir = tmp_path / "emoji"
    emoji_dir.mkdir()
    with pytest.raises(MessageBuildError, match="表情包不存在"):
        build_message_action(None, "missing", None, emoji_dir, tmp_path)


def test_build_message_action_rejects_emoji_path_traversal(tmp_path):
    emoji_dir = tmp_path / "emoji"
    emoji_dir.mkdir()
    with pytest.raises(MessageBuildError, match="不能包含路径"):
        build_message_action(None, "../etc/passwd", None, emoji_dir, tmp_path)


def test_build_message_action_image_url():
    action = build_message_action(None, None, "https://example.com/a.png", None, None)
    assert action["kind"] == "image"
    assert action["image_url"] == "https://example.com/a.png"
    assert action["label"] == "[图片]"
