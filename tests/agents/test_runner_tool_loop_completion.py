"""测试 AgentRunner 工具循环完成判定。"""

from __future__ import annotations

import pytest

from agents.runner import AgentRunner
from app_config.schema import AgentConfig
from providers.base import CompletionResult, IProvider, ToolCall, Usage


@pytest.mark.asyncio
async def test_runner_async_agent_task_tools_do_not_finish_as_no_feedback():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            if len(self.calls) == 1:
                return CompletionResult(
                    tool_calls=[
                        ToolCall(
                            id="tc-agent",
                            name="start_agent_task",
                            arguments='{"prompt":"整理资料"}',
                        )
                    ],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=7),
                )
            assert messages[-1]["role"] == "tool"
            assert "agent-test" in messages[-1]["content"]
            return CompletionResult(
                tool_calls=[
                    ToolCall(id="tc-na", name="no_action", arguments="{}")
                ],
                finish_reason="tool_calls",
                usage=Usage(prompt_tokens=8),
            )

        async def aclose(self) -> None:
            pass

    async def executor(name, _args):
        if name == "start_agent_task":
            return {
                "ok": True,
                "status": "completed",
                "task_id": "agent-test",
                "result_file": "agent_tasks/agent-test/result.md",
                "content": "任务结果",
            }
        if name == "no_action":
            return {"ok": True, "no_action": True}
        raise AssertionError(name)

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(provider="fake", model="fake", max_loops=3),
    )

    result = await runner.run(
        [{"role": "user", "content": "启动后台任务"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "start_agent_task",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "no_action",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ],
        tool_executor=executor,
    )

    assert result.finish_reason == "no_action"
    assert len(provider.calls) == 2
    assert result.prompt_tokens == 15

@pytest.mark.asyncio
async def test_runner_all_finish_after_success_tools_finish():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            return CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-a",
                        name="save_important_memory",
                        arguments=(
                            '{"memory_text":"用户喜欢咖啡",'
                            '"finish_after_success":true}'
                        ),
                    ),
                    ToolCall(
                        id="tc-b",
                        name="schedule_wakeup",
                        arguments=(
                            '{"delay_seconds":60,"mode":"wakeup",'
                            '"reminder":"提醒用户",'
                            '"finish_after_success":true}'
                        ),
                    ),
                ],
                finish_reason="tool_calls",
                usage=Usage(prompt_tokens=5),
            )

        async def aclose(self) -> None:
            pass

    async def executor(name, _args):
        return {"ok": True, "status": "done", "tool": name}

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(provider="fake", model="fake", max_loops=3),
    )

    result = await runner.run(
        [{"role": "user", "content": "保存并提醒"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "save_important_memory",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "schedule_wakeup",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ],
        tool_executor=executor,
    )

    assert result.finish_reason == "finish_after_success"
    assert len(provider.calls) == 1
    tool_records = [r for r in result.records if r.get("role") == "tool"]
    assert len(tool_records) == 2
    for record in tool_records:
        assert '"turn_completion"' in record["content"]
        assert '"allowed": true' in record["content"]

@pytest.mark.parametrize(
    "blocked_result",
    [
        {"ok": False, "status": "failed"},
        {"ok": True, "status": "partial"},
        {"ok": True, "status": "needs_review"},
        {"ok": True, "status": "need_tool_search"},
        {"ok": True, "errors": ["工具返回错误"]},
    ],
)
@pytest.mark.asyncio
async def test_runner_blocked_finish_after_success_tool_continues(blocked_result):
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            if len(self.calls) == 1:
                return CompletionResult(
                    tool_calls=[
                        ToolCall(
                            id="tc-tool",
                            name="save_important_memory",
                            arguments=(
                                '{"memory_text":"用户喜欢茶",'
                                '"finish_after_success":true}'
                            ),
                        )
                    ],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=5),
                )
            assert messages[-1]["role"] == "tool"
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
        if name == "save_important_memory":
            return dict(blocked_result)
        if name == "no_action":
            return {"ok": True, "status": "done", "no_action": True}
        raise AssertionError(name)

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(provider="fake", model="fake", max_loops=3),
    )

    result = await runner.run(
        [{"role": "user", "content": "保存"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "save_important_memory",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "no_action",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ],
        tool_executor=executor,
    )

    assert result.finish_reason == "no_action"
    assert len(provider.calls) == 2

@pytest.mark.asyncio
async def test_runner_requires_all_non_no_action_tools_to_allow_completion():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            if len(self.calls) == 1:
                return CompletionResult(
                    tool_calls=[
                        ToolCall(
                            id="tc-a",
                            name="save_important_memory",
                            arguments=(
                                '{"memory_text":"用户喜欢咖啡",'
                                '"finish_after_success":true}'
                            ),
                        ),
                        ToolCall(
                            id="tc-b",
                            name="schedule_wakeup",
                            arguments=(
                                '{"delay_seconds":60,"mode":"wakeup",'
                                '"reminder":"提醒用户"}'
                            ),
                        ),
                    ],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=5),
                )
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
        if name == "no_action":
            return {"ok": True, "status": "done", "no_action": True}
        return {"ok": True, "status": "done", "tool": name}

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(provider="fake", model="fake", max_loops=3),
    )

    result = await runner.run(
        [{"role": "user", "content": "保存并提醒"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "save_important_memory",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "schedule_wakeup",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "no_action",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ],
        tool_executor=executor,
    )

    assert result.finish_reason == "no_action"
    assert len(provider.calls) == 2
