"""测试 AgentRunner 工具循环 reminder。"""

from __future__ import annotations

import pytest

from agents.runner import AgentRunner
from app_config.schema import AgentConfig
from providers.base import CompletionResult, IProvider, ToolCall, Usage
from tests.agents.helpers import (
    basic_tool_schema as _basic_tool_schema,
)
from tests.agents.helpers import (
    tool_payloads as _tool_payloads,
)
from tests.agents.helpers import (
    work_call as _work_call,
)


@pytest.mark.asyncio
async def test_runner_tool_loop_reminder_attaches_to_last_tool_result():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools, **kwargs})
            if len(self.calls) <= 2:
                return CompletionResult(
                    tool_calls=[_work_call(len(self.calls))],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=5),
                )
            return CompletionResult(
                tool_calls=[ToolCall(id="tc-na", name="no_action", arguments="{}")],
                finish_reason="tool_calls",
                usage=Usage(prompt_tokens=5),
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
            tool_loop_reminder_interval=2,
            tool_loop_final_warning_count=99,
        ),
    )

    result = await runner.run(
        [{"role": "user", "content": "处理资料"}],
        tools=[_basic_tool_schema(), _basic_tool_schema("no_action")],
        tool_executor=executor,
    )

    payloads = _tool_payloads(result)
    assert "loop_reminder" not in payloads[0]
    reminder = payloads[1]["loop_reminder"]
    assert reminder["level"] == "reminder"
    assert reminder["tool_loop_reminder_interval"] == 2
    assert reminder["reminder_count"] == 1
    assert result.finish_reason == "no_action"

@pytest.mark.asyncio
async def test_runner_tool_loop_reminder_resets_and_can_repeat():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools, **kwargs})
            if len(self.calls) <= 4:
                return CompletionResult(
                    tool_calls=[_work_call(len(self.calls))],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=5),
                )
            return CompletionResult(
                tool_calls=[ToolCall(id="tc-na", name="no_action", arguments="{}")],
                finish_reason="tool_calls",
                usage=Usage(prompt_tokens=5),
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
            tool_loop_reminder_interval=2,
            tool_loop_final_warning_count=99,
        ),
    )

    result = await runner.run(
        [{"role": "user", "content": "处理资料"}],
        tools=[_basic_tool_schema(), _basic_tool_schema("no_action")],
        tool_executor=executor,
    )

    reminders = [
        payload["loop_reminder"]["reminder_count"]
        for payload in _tool_payloads(result)
        if "loop_reminder" in payload
    ]
    assert reminders == [1, 2]
    assert result.finish_reason == "no_action"
