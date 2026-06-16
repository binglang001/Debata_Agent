"""测试 AgentRunner 工具循环基础记录构造。"""

from __future__ import annotations


def test_runner_assistant_record_preserves_empty_reasoning_with_blocks():
    from agents.runner import AgentRunner
    from providers.base import CompletionResult

    result = CompletionResult(
        content="ok",
        reasoning_content="",
        reasoning_blocks=[
            {"type": "thinking", "thinking": "", "signature": "sig"}
        ],
    )

    record = AgentRunner._build_assistant_record(result)

    assert record["reasoning_content"] == ""
    assert record["reasoning_blocks"] == [
        {"type": "thinking", "thinking": "", "signature": "sig"}
    ]

def test_runner_assistant_record_preserves_reasoning_content():
    from agents.runner import AgentRunner
    from providers.base import CompletionResult

    record = AgentRunner._build_assistant_record(
        CompletionResult(content="ok", reasoning_content="plan")
    )

    assert record["reasoning_content"] == "plan"
