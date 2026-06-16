"""测试 AgentRunner 工具循环。"""

from __future__ import annotations

import json

import pytest

from agents.base import AgentRunResult
from agents.runner import AgentRunner
from app_config.schema import AgentConfig
from providers.base import CompletionResult, IProvider, ToolCall, Usage


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


def _basic_tool_schema(name: str = "needs_feedback") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _work_call(idx: int, *, name: str = "needs_feedback", args: str = "{}") -> ToolCall:
    return ToolCall(id=f"tc-{idx}", name=name, arguments=args)


def _tool_payloads(result: AgentRunResult) -> list[dict]:
    return [
        json.loads(record["content"])
        for record in result.records
        if record.get("role") == "tool"
    ]


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
