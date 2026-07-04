"""测试 AgentRunner 工具循环阻塞、max loop 与重试。"""

from __future__ import annotations

import pytest

from agents.runner import AgentRunner
from app_config.schema import AgentConfig
from providers.base import CompletionResult, IProvider, ToolCall, Usage
from tests.agents.helpers import (
    basic_tool_schema as _basic_tool_schema,
)
from tests.agents.helpers import (
    work_call as _work_call,
)


@pytest.mark.asyncio
async def test_runner_legacy_max_loops_no_longer_hard_limits_tool_loop():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            if len(self.calls) <= 2:
                return CompletionResult(
                    tool_calls=[_work_call(len(self.calls))],
                    finish_reason="tool_calls",
                )
            return CompletionResult(
                tool_calls=[ToolCall(id="tc-na", name="no_action", arguments="{}")],
                finish_reason="tool_calls",
            )

        async def aclose(self) -> None:
            pass

    async def executor(name, _args):
        if name == "no_action":
            return {"ok": True, "no_action": True}
        return {"ok": True, "status": "partial"}

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(
            provider="fake",
            model="fake",
            max_loops=1,
            tool_loop_reminder_interval=20,
            tool_loop_final_warning_count=99,
        ),
    )

    result = await runner.run(
        [{"role": "user", "content": "处理资料"}],
        tools=[_basic_tool_schema(), _basic_tool_schema("no_action")],
        tool_executor=executor,
    )

    assert result.finish_reason == "no_action"
    assert len(provider.calls) == 3

@pytest.mark.asyncio
async def test_runner_failed_no_action_does_not_finish_tool_loop():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            if len(self.calls) == 1:
                return CompletionResult(
                    tool_calls=[ToolCall(id="tc-na-fail", name="no_action", arguments="{}")],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=5),
                )
            if len(self.calls) == 2:
                assert "policy_rejected" in messages[-1]["content"]
                return CompletionResult(
                    tool_calls=[
                        ToolCall(
                            id="tc-send",
                            name="send_private_messages",
                            arguments=(
                                '{"targets": [{"target_qq": 123, "content": "已处理",'
                                ' "order": 1, "delay": 0}]}'
                            ),
                        )
                    ],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=6),
                )
            return CompletionResult(
                tool_calls=[ToolCall(id="tc-na-ok", name="no_action", arguments="{}")],
                finish_reason="tool_calls",
                usage=Usage(prompt_tokens=6),
            )

        async def aclose(self) -> None:
            pass

    no_action_calls = 0

    async def executor(name, _args):
        nonlocal no_action_calls
        if name == "no_action":
            no_action_calls += 1
            if no_action_calls > 1:
                return {"ok": True, "status": "done"}
            return {"ok": False, "status": "policy_rejected"}
        if name == "send_private_messages":
            return {"ok": True, "status": "sent"}
        raise AssertionError(name)

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(provider="fake", model="fake", max_loops=3),
    )

    result = await runner.run(
        [{"role": "user", "content": "交付结果"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "no_action",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "send_private_messages",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ],
        tool_executor=executor,
    )

    assert result.finish_reason == "no_action"
    assert len(provider.calls) == 3

@pytest.mark.parametrize("pending_status", ["failed", "partial", "needs_review"])
@pytest.mark.asyncio
async def test_runner_pending_no_action_status_does_not_finish_tool_loop(pending_status):
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            if len(self.calls) == 1:
                return CompletionResult(
                    tool_calls=[
                        ToolCall(id="tc-na-pending", name="no_action", arguments="{}")
                    ],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=5),
                )
            assert messages[-1]["role"] == "tool"
            assert pending_status in messages[-1]["content"]
            return CompletionResult(
                tool_calls=[
                    ToolCall(id="tc-na-ok", name="no_action", arguments="{}")
                ],
                finish_reason="tool_calls",
                usage=Usage(prompt_tokens=6),
            )

        async def aclose(self) -> None:
            pass

    no_action_calls = 0

    async def executor(name, _args):
        nonlocal no_action_calls
        assert name == "no_action"
        no_action_calls += 1
        if no_action_calls == 1:
            return {"status": pending_status}
        return {"ok": True, "status": "done", "no_action": True}

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(provider="fake", model="fake", max_loops=3),
    )

    result = await runner.run(
        [{"role": "user", "content": "不操作"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "no_action",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        tool_executor=executor,
    )

    assert result.finish_reason == "no_action"
    assert len(provider.calls) == 2

@pytest.mark.asyncio
async def test_runner_stop_after_tool_finishes_immediately():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            return CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-write",
                        name="write_file",
                        arguments='{"path": "result.md", "content": "done"}',
                    )
                ],
                finish_reason="tool_calls",
                usage=Usage(prompt_tokens=5),
            )

        async def aclose(self) -> None:
            pass

    async def executor(name, _args):
        assert name == "write_file"
        return {"ok": True, "path": "result.md", "stop_after_tool": True}

    runner = AgentRunner(
        FakeProvider(),
        AgentConfig(provider="fake", model="fake", max_loops=3),
    )

    result = await runner.run(
        [{"role": "user", "content": "写结果"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        tool_executor=executor,
    )

    assert result.finish_reason == "tool_stop"
    assert result.loop_count == 1

@pytest.mark.asyncio
async def test_runner_drops_plain_text_draft_before_retry():
    leaked_draft = "思考过程\nRAG里提到撤回消息，所以我应该这样回复"

    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            if len(self.calls) == 1:
                return CompletionResult(
                    content=leaked_draft,
                    finish_reason="stop",
                    usage=Usage(prompt_tokens=5),
                )
            joined = "\n".join(str(m.get("content") or "") for m in messages)
            assert leaked_draft not in joined
            assert "上一轮纯文本已被系统丢弃" in joined
            return CompletionResult(
                tool_calls=[
                    ToolCall(id="tc-na", name="no_action", arguments="{}")
                ],
                finish_reason="tool_calls",
                usage=Usage(prompt_tokens=6),
            )

        async def aclose(self) -> None:
            pass

    async def executor(name, _args):
        assert name == "no_action"
        return {"ok": True, "no_action": True}

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(provider="fake", model="fake", max_loops=3),
    )

    result = await runner.run(
        [{"role": "user", "content": "不要泄漏内部分析"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "no_action",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        tool_executor=executor,
    )

    assert result.finish_reason == "no_action"
    assert len(provider.calls) == 2
    assert not any(
        r.get("role") == "assistant" and r.get("content") == leaked_draft
        for r in result.records
    )
