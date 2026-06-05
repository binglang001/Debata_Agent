"""测试 Anthropic 协议的 message/tool 转换。

由于实际 API 调用涉及网络，本文件只测试静态的协议翻译逻辑。
"""

from __future__ import annotations

from types import SimpleNamespace

from providers.base import CompletionResult
from providers.protocols.anthropic_proto import AnthropicProvider


def test_convert_messages_system_extracted():
    messages = [
        {"role": "system", "content": "你是 Debata"},
        {"role": "user", "content": "你好"},
    ]
    system, msgs = AnthropicProvider._convert_messages(messages)
    assert system == "你是 Debata"
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"


def test_convert_messages_multiple_system_merged():
    messages = [
        {"role": "system", "content": "规则1"},
        {"role": "system", "content": "规则2"},
        {"role": "user", "content": "ok"},
    ]
    system, _ = AnthropicProvider._convert_messages(messages)
    assert "规则1" in system
    assert "规则2" in system


def test_convert_messages_tool_call_to_tool_use():
    messages = [
        {"role": "user", "content": "查询"},
        {
            "role": "assistant",
            "content": "好的",
            "tool_calls": [
                {
                    "id": "tc1",
                    "function": {
                        "name": "search",
                        "arguments": '{"q":"python"}',
                    },
                }
            ],
        },
    ]
    _, msgs = AnthropicProvider._convert_messages(messages)
    assistant_msg = msgs[1]
    blocks = assistant_msg["content"]
    assert any(b.get("type") == "text" and b.get("text") == "好的" for b in blocks)
    tool_use_blocks = [b for b in blocks if b.get("type") == "tool_use"]
    assert len(tool_use_blocks) == 1
    assert tool_use_blocks[0]["name"] == "search"
    assert tool_use_blocks[0]["input"] == {"q": "python"}
    assert tool_use_blocks[0]["id"] == "tc1"


def test_convert_messages_replays_reasoning_content_as_thinking_block():
    messages = [
        {
            "role": "assistant",
            "content": "答案",
            "reasoning_content": "",
        },
    ]
    _, msgs = AnthropicProvider._convert_messages(messages)
    blocks = msgs[1]["content"]

    assert blocks[0] == {"type": "thinking", "thinking": ""}
    assert blocks[1] == {"type": "text", "text": "答案"}


def test_convert_messages_replays_signed_reasoning_blocks():
    messages = [
        {
            "role": "assistant",
            "content": "答案",
            "reasoning_blocks": [
                {
                    "type": "thinking",
                    "thinking": "chain",
                    "signature": "sig",
                },
                {
                    "type": "redacted_thinking",
                    "data": "opaque",
                },
            ],
        },
    ]
    _, msgs = AnthropicProvider._convert_messages(messages)
    blocks = msgs[1]["content"]

    assert blocks[0] == {
        "type": "thinking",
        "thinking": "chain",
        "signature": "sig",
    }
    assert blocks[1] == {"type": "redacted_thinking", "data": "opaque"}
    assert blocks[2] == {"type": "text", "text": "答案"}


def test_anthropic_response_preserves_reasoning_blocks():
    provider = AnthropicProvider("anthropic_test", api_key="sk-ant-test")
    response = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="plan", signature="sig"),
            SimpleNamespace(type="redacted_thinking", data="opaque"),
            SimpleNamespace(type="text", text="ok"),
        ],
        usage=None,
        stop_reason="end_turn",
        model="claude-test",
    )

    result = provider._build_result_from_response(response, "fallback")

    assert isinstance(result, CompletionResult)
    assert result.content == "ok"
    assert result.reasoning_content == "plan"
    assert result.reasoning_blocks == [
        {"type": "thinking", "thinking": "plan", "signature": "sig"},
        {"type": "redacted_thinking", "data": "opaque"},
    ]


def test_anthropic_response_extracts_cache_usage():
    provider = AnthropicProvider("anthropic_test", api_key="sk-ant-test")
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=70,
            cache_creation_input_tokens=15,
        ),
        stop_reason="end_turn",
        model="claude-test",
    )

    result = provider._build_result_from_response(response, "fallback")

    assert result.usage.prompt_tokens == 100
    assert result.usage.completion_tokens == 20
    assert result.usage.cached_tokens == 70
    assert result.usage.cache_creation_tokens == 15


def test_convert_messages_tool_result():
    messages = [
        {"role": "user", "content": "查询"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "tc1", "function": {"name": "f", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "tc1", "content": '{"result": "ok"}'},
    ]
    _, msgs = AnthropicProvider._convert_messages(messages)
    # 最后一条应是 user role 包含 tool_result 块
    last = msgs[-1]
    assert last["role"] == "user"
    blocks = last["content"]
    assert isinstance(blocks, list)
    assert blocks[0]["type"] == "tool_result"
    assert blocks[0]["tool_use_id"] == "tc1"


def test_convert_messages_handles_invalid_json_arguments():
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "tc1", "function": {"name": "f", "arguments": "not json"}}
            ],
        },
    ]
    _, msgs = AnthropicProvider._convert_messages(messages)
    # 找到 assistant 消息
    assistant_msg = next(m for m in msgs if m["role"] == "assistant")
    blocks = assistant_msg["content"]
    assert isinstance(blocks, list), f"期望 list 类型的 blocks，实际是 {type(blocks)}"
    tool_use = [b for b in blocks if b.get("type") == "tool_use"][0]
    # 应回退到空 dict 而不是崩溃
    assert tool_use["input"] == {}


def test_convert_messages_assistant_first_prepends_user():
    """Anthropic 要求 messages 不能以 assistant 开头。"""
    messages = [{"role": "assistant", "content": "hi"}]
    _, msgs = AnthropicProvider._convert_messages(messages)
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"


def test_convert_tools_openai_to_anthropic():
    openai_tools = [
        {
            "type": "function",
            "function": {
                "name": "search",
                "description": "搜索",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
        }
    ]
    converted = AnthropicProvider._convert_tools(openai_tools)
    assert len(converted) == 1
    assert converted[0]["name"] == "search"
    assert converted[0]["description"] == "搜索"
    assert "input_schema" in converted[0]
    assert converted[0]["input_schema"]["type"] == "object"


def test_convert_tools_handles_flat_format():
    """容错：如果用户传的 tool 已经是 Anthropic 风格的扁平结构。"""
    flat = [{"name": "f", "description": "d", "parameters": {"type": "object"}}]
    converted = AnthropicProvider._convert_tools(flat)
    assert converted[0]["name"] == "f"
