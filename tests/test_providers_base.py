"""测试 provider 基础类型与工具。"""

from __future__ import annotations

from providers.base import (
    CompletionResult,
    ReasoningConfig,
    ToolCall,
    Usage,
    normalize_messages,
)


def test_completion_result_defaults():
    r = CompletionResult()
    assert r.content == ""
    assert r.tool_calls == []
    assert r.reasoning_content == ""
    assert isinstance(r.usage, Usage)
    assert not r.has_tool_calls()


def test_completion_result_with_tool_calls():
    r = CompletionResult(tool_calls=[ToolCall("tc1", "f", "{}")])
    assert r.has_tool_calls()


def test_reasoning_config_defaults():
    rc = ReasoningConfig()
    assert rc.enabled is False
    assert rc.budget is None


def test_normalize_messages_strips_extras_by_default():
    """默认仍剔除 provider 专属字段，避免发给不支持的 SDK。"""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi", "extra_field": "boom"},
        {
            "role": "assistant",
            "content": "ok",
            "reasoning_content": "I think...",
            "weird": 1,
        },
    ]
    out = normalize_messages(messages)
    assert len(out) == 3
    assert "extra_field" not in out[1]
    assert "reasoning_content" not in out[2]
    assert "weird" not in out[2]


def test_normalize_messages_can_preserve_assistant_reasoning_content():
    """DeepSeek/Qwen 思考模式多轮回放需要保留 reasoning_content，含空字符串。"""
    messages = [
        {"role": "user", "content": "hi", "reasoning_content": "ignore"},
        {"role": "assistant", "content": "ok", "reasoning_content": ""},
        {"role": "assistant", "content": "done", "reasoning_content": "I think"},
    ]
    out = normalize_messages(messages, preserve_reasoning_content=True)

    assert "reasoning_content" not in out[0]
    assert out[1]["reasoning_content"] == ""
    assert out[2]["reasoning_content"] == "I think"


def test_normalize_messages_preserves_tool_call_structure():
    msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "tc1",
                "type": "function",
                "function": {"name": "f", "arguments": '{"x":1}'},
            }
        ],
    }
    out = normalize_messages([msg])
    assert out[0]["tool_calls"][0]["id"] == "tc1"
    assert out[0]["tool_calls"][0]["function"]["name"] == "f"


def test_normalize_messages_fills_function_type():
    """缺 'type': 'function' 时应自动补齐。"""
    msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "tc1", "function": {"name": "f", "arguments": "{}"}}
        ],
    }
    out = normalize_messages([msg])
    assert out[0]["tool_calls"][0]["type"] == "function"


def test_normalize_messages_preserves_tool_role():
    msg = {"role": "tool", "tool_call_id": "tc1", "content": '{"ok":true}'}
    out = normalize_messages([msg])
    assert out[0]["role"] == "tool"
    assert out[0]["tool_call_id"] == "tc1"


def test_normalize_messages_drops_role_less():
    out = normalize_messages([{"content": "no role"}, {"role": "user", "content": "ok"}])
    assert len(out) == 1
    assert out[0]["content"] == "ok"


def test_normalize_messages_empty_tool_calls_dropped():
    msg = {"role": "assistant", "content": "ok", "tool_calls": []}
    out = normalize_messages([msg])
    assert "tool_calls" not in out[0]
