"""测试 AgentRunner 工具循环 final warning 与宽限。"""

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
async def test_runner_tool_loop_final_warning_and_grace_then_finalizes():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools, **kwargs})
            call_no = len(self.calls)
            if call_no == 5:
                assert tools is not None
                assert messages[-1]["role"] == "user"
                assert "<tool_loop_final_warning" in messages[-1]["content"]
                assert "你还有 2 轮工具调用机会" in messages[-1]["content"]
            if call_no <= 6:
                return CompletionResult(
                    tool_calls=[_work_call(call_no)],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=5),
                )
            assert tools is None
            assert messages[-1]["role"] == "user"
            assert "<tool_loop_stop" in messages[-1]["content"]
            return CompletionResult(
                content="工具循环已收尾。",
                finish_reason="stop",
                usage=Usage(prompt_tokens=7),
            )

        async def aclose(self) -> None:
            pass

    async def executor(_name, _args):
        return {"ok": True, "status": "partial"}

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(
            provider="fake",
            model="fake",
            tool_loop_reminder_interval=3,
            tool_loop_final_warning_count=1,
            tool_loop_final_grace_loops=2,
            tool_loop_final_max_tokens=1536,
        ),
    )

    result = await runner.run(
        [{"role": "user", "content": "持续处理"}],
        tools=[_basic_tool_schema()],
        tool_executor=executor,
    )

    assert result.finish_reason == "tool_loop_finalized"
    assert result.final_content == "工具循环已收尾。"
    assert provider.calls[4]["tools"] is not None
    assert provider.calls[5]["tools"] is not None
    assert provider.calls[6]["tools"] is None
    assert provider.calls[6]["max_tokens"] == 1536

@pytest.mark.asyncio
async def test_runner_tool_loop_zero_grace_finalizes_at_next_warning_threshold():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            call_no = len(self.calls)
            if call_no <= 4:
                assert tools is not None
                return CompletionResult(
                    tool_calls=[_work_call(call_no)],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=5),
                )
            assert tools is None
            assert messages[-1]["role"] == "user"
            assert "<tool_loop_stop" in messages[-1]["content"]
            assert messages[-2]["role"] == "user"
            assert "<tool_loop_final_warning" in messages[-2]["content"]
            assert "你还有 0 轮工具调用机会" in messages[-2]["content"]
            return CompletionResult(
                content="零宽限收尾。",
                finish_reason="stop",
                usage=Usage(prompt_tokens=7),
            )

        async def aclose(self) -> None:
            pass

    async def executor(_name, _args):
        return {"ok": True, "status": "partial"}

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(
            provider="fake",
            model="fake",
            tool_loop_reminder_interval=2,
            tool_loop_final_warning_count=1,
            tool_loop_final_grace_loops=0,
        ),
    )

    result = await runner.run(
        [{"role": "user", "content": "持续处理"}],
        tools=[_basic_tool_schema()],
        tool_executor=executor,
    )

    assert result.finish_reason == "tool_loop_finalized"
    assert result.final_content == "零宽限收尾。"
    assert len(provider.calls) == 5
    assert provider.calls[4]["tools"] is None

@pytest.mark.asyncio
async def test_runner_no_action_finishes_during_tool_loop_grace():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            if len(self.calls) <= 3:
                return CompletionResult(
                    tool_calls=[_work_call(len(self.calls))],
                    finish_reason="tool_calls",
                )
            assert "<tool_loop_final_warning" in messages[-1]["content"]
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
            tool_loop_reminder_interval=2,
            tool_loop_final_warning_count=1,
            tool_loop_final_grace_loops=1,
        ),
    )

    result = await runner.run(
        [{"role": "user", "content": "持续处理"}],
        tools=[_basic_tool_schema(), _basic_tool_schema("no_action")],
        tool_executor=executor,
    )

    assert result.finish_reason == "no_action"
    assert all(call["tools"] is not None for call in provider.calls)

@pytest.mark.asyncio
async def test_runner_finish_after_success_finishes_during_tool_loop_grace():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            if len(self.calls) <= 3:
                return CompletionResult(
                    tool_calls=[_work_call(len(self.calls))],
                    finish_reason="tool_calls",
                )
            assert "<tool_loop_final_warning" in messages[-1]["content"]
            return CompletionResult(
                tool_calls=[
                    _work_call(
                        99,
                        name="save_important_memory",
                        args='{"memory_text":"完成","finish_after_success":true}',
                    )
                ],
                finish_reason="tool_calls",
            )

        async def aclose(self) -> None:
            pass

    async def executor(name, _args):
        if name == "save_important_memory":
            return {"ok": True, "status": "done"}
        return {"ok": True, "status": "partial"}

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(
            provider="fake",
            model="fake",
            tool_loop_reminder_interval=2,
            tool_loop_final_warning_count=1,
            tool_loop_final_grace_loops=1,
        ),
    )

    result = await runner.run(
        [{"role": "user", "content": "持续处理"}],
        tools=[_basic_tool_schema(), _basic_tool_schema("save_important_memory")],
        tool_executor=executor,
    )

    assert result.finish_reason == "finish_after_success"
    assert all(call["tools"] is not None for call in provider.calls)
