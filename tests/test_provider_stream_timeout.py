"""Provider 流式首 chunk 超时的回归测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from providers.base import ProviderTimeoutError, ReasoningConfig
from providers.protocols.anthropic_proto import AnthropicProvider
from providers.protocols.openai_compat import OpenAICompatProvider


class DelayedAsyncStream:
    def __init__(self, delays: list[float], items: list[object]) -> None:
        self._delays = delays
        self._items = items
        self._idx = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._idx >= len(self._items):
            raise StopAsyncIteration
        delay = self._delays[self._idx]
        item = self._items[self._idx]
        self._idx += 1
        if delay:
            await asyncio.sleep(delay)
        return item


class DelayedAnthropicStream(DelayedAsyncStream):
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _openai_chunk(text: str, finish_reason: str | None = None):
    return SimpleNamespace(
        usage=None,
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=text,
                    reasoning_content=None,
                    tool_calls=None,
                ),
                finish_reason=finish_reason,
            )
        ],
    )


def _anthropic_text_delta(text: str):
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="text_delta", text=text),
    )


def test_openai_thinking_extra_body_disables_by_default():
    provider = OpenAICompatProvider(
        "deepseek_test",
        base_url="https://example.com/v1",
        api_key="sk-test",
        reasoning_style="thinking_extra_body",
    )

    assert provider._build_reasoning_extra_body(None, model="fake") == {
        "thinking": {"type": "disabled"}
    }
    assert provider._build_reasoning_extra_body(
        ReasoningConfig(enabled=False), model="fake"
    ) == {
        "thinking": {"type": "disabled"}
    }


def test_openai_thinking_extra_body_enables_only_when_requested():
    provider = OpenAICompatProvider(
        "deepseek_test",
        base_url="https://example.com/v1",
        api_key="sk-test",
        reasoning_style="thinking_extra_body",
    )

    assert provider._build_reasoning_extra_body(
        ReasoningConfig(enabled=True), model="fake"
    ) == {
        "thinking": {"type": "enabled"}
    }


def test_openai_qwen_reasoning_switch_is_explicit():
    provider = OpenAICompatProvider(
        "qwen_test",
        base_url="https://example.com/v1",
        api_key="sk-test",
        reasoning_style="qwen_enable_thinking",
    )

    assert provider._build_reasoning_extra_body(None, model="fake") == {
        "enable_thinking": False
    }
    assert provider._build_reasoning_extra_body(
        ReasoningConfig(enabled=False), model="fake"
    ) == {
        "enable_thinking": False
    }
    assert provider._build_reasoning_extra_body(
        ReasoningConfig(enabled=True), model="fake"
    ) == {
        "enable_thinking": True
    }


def test_openai_reasoning_controls_skip_known_non_reasoning_models():
    provider = OpenAICompatProvider(
        "mixed_test",
        base_url="https://example.com/v1",
        api_key="sk-test",
        reasoning_style="thinking_extra_body",
        known_model_ids={"plain", "reasoner"},
        reasoning_model_ids={"reasoner"},
    )

    assert provider._build_reasoning_extra_body(None, model="plain") == {}
    assert provider._build_reasoning_extra_body(
        ReasoningConfig(enabled=True), model="plain"
    ) == {}
    assert provider._build_reasoning_extra_body(None, model="reasoner") == {
        "thinking": {"type": "disabled"}
    }
    assert provider._build_reasoning_extra_body(
        ReasoningConfig(enabled=True), model="unknown-future-reasoner"
    ) == {"thinking": {"type": "enabled"}}


@pytest.mark.asyncio
async def test_openai_reasoning_mode_replays_assistant_reasoning_content(monkeypatch):
    provider = OpenAICompatProvider(
        "deepseek_test",
        base_url="https://example.com/v1",
        api_key="sk-test",
        reasoning_style="thinking_extra_body",
    )
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="ok",
                        reasoning_content="",
                        tool_calls=None,
                    ),
                    finish_reason="stop",
                )
            ],
            usage=None,
            model="fake",
        )

    monkeypatch.setattr(provider._client.chat.completions, "create", fake_create)

    await provider.chat_completion(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "ok"},
            {"role": "assistant", "content": "", "reasoning_content": ""},
            {"role": "user", "content": "again"},
        ],
        model="fake",
        reasoning=ReasoningConfig(enabled=True),
        stream=False,
    )

    assistant_msgs = [
        msg for msg in captured["messages"] if msg.get("role") == "assistant"
    ]
    assert assistant_msgs[0]["reasoning_content"] == ""
    assert assistant_msgs[1]["reasoning_content"] == ""
    assert captured["extra_body"] == {"thinking": {"type": "enabled"}}
    await provider.aclose()


@pytest.mark.asyncio
async def test_openai_non_reasoning_mode_does_not_replay_reasoning_content(monkeypatch):
    provider = OpenAICompatProvider(
        "deepseek_test",
        base_url="https://example.com/v1",
        api_key="sk-test",
        reasoning_style="thinking_extra_body",
    )
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok", tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=None,
            model="fake",
        )

    monkeypatch.setattr(provider._client.chat.completions, "create", fake_create)

    await provider.chat_completion(
        [
            {"role": "assistant", "content": "ok", "reasoning_content": "hidden"},
        ],
        model="fake",
        reasoning=None,
        stream=False,
    )

    assert "reasoning_content" not in captured["messages"][0]
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
    await provider.aclose()


@pytest.mark.asyncio
async def test_openai_first_token_timeout_does_not_truncate_later_chunks(monkeypatch):
    provider = OpenAICompatProvider(
        "openai_test",
        base_url="https://example.com/v1",
        api_key="sk-test",
    )

    async def fake_create(**kwargs):
        return DelayedAsyncStream(
            [0.0, 0.05],
            [_openai_chunk("第一段"), _openai_chunk("第二段", "stop")],
        )

    monkeypatch.setattr(provider._client.chat.completions, "create", fake_create)

    result = await provider._chat_stream({"model": "fake"}, first_token_timeout=0.01)

    assert result.content == "第一段第二段"
    await provider.aclose()


@pytest.mark.asyncio
async def test_openai_first_token_timeout_still_fails_before_first_chunk(monkeypatch):
    provider = OpenAICompatProvider(
        "openai_test",
        base_url="https://example.com/v1",
        api_key="sk-test",
    )

    async def fake_create(**kwargs):
        return DelayedAsyncStream([0.05], [_openai_chunk("太晚了")])

    monkeypatch.setattr(provider._client.chat.completions, "create", fake_create)

    with pytest.raises(ProviderTimeoutError):
        await provider._chat_stream({"model": "fake"}, first_token_timeout=0.01)
    await provider.aclose()


@pytest.mark.asyncio
async def test_anthropic_first_token_timeout_does_not_truncate_later_events(monkeypatch):
    provider = AnthropicProvider(
        "anthropic_test",
        api_key="sk-ant-test",
    )

    def fake_stream(**kwargs):
        return DelayedAnthropicStream(
            [0.0, 0.05],
            [_anthropic_text_delta("第一段"), _anthropic_text_delta("第二段")],
        )

    monkeypatch.setattr(provider._client.messages, "stream", fake_stream)

    result = await provider._stream_call({"model": "fake"}, first_token_timeout=0.01)

    assert result.content == "第一段第二段"
    await provider.aclose()


@pytest.mark.asyncio
async def test_anthropic_first_token_timeout_still_fails_before_first_event(monkeypatch):
    provider = AnthropicProvider(
        "anthropic_test",
        api_key="sk-ant-test",
    )

    def fake_stream(**kwargs):
        return DelayedAnthropicStream([0.05], [_anthropic_text_delta("太晚了")])

    monkeypatch.setattr(provider._client.messages, "stream", fake_stream)

    with pytest.raises(ProviderTimeoutError):
        await provider._stream_call({"model": "fake"}, first_token_timeout=0.01)
    await provider.aclose()
