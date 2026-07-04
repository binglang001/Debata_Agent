"""AgentRunner runtime event tests."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from agents.runner import AgentRunner
from app_config.schema import AgentConfig
from core.message_pipeline import MessagePipeline
from memory import EventStore
from providers.base import CompletionResult, IProvider, ToolCall, Usage


class ScriptedProvider(IProvider):
    def __init__(self, script: list[CompletionResult]) -> None:
        super().__init__("fake")
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def chat_completion(self, messages, *, tools=None, **kwargs):
        self.calls.append({"messages": list(messages), "tools": tools, **kwargs})
        if not self.script:
            raise AssertionError("provider script exhausted")
        return self.script.pop(0)

    async def aclose(self) -> None:
        pass


def _tool_schema(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _assert_runner_error_envelope(
    payload: dict[str, Any],
    *,
    tool: str,
    brief: str,
) -> None:
    assert payload["ok"] is False
    assert payload["status"] == "failed"
    assert payload["tool"] == tool
    assert payload["result_format"] == "structured_json"
    assert payload["brief"] == brief
    assert payload["error"]


@pytest.mark.asyncio
async def test_runner_tool_runtime_events_started_then_result_full_payload(tmp_path):
    raw_args = '{"path": "result.md", "content": "done"}'
    full_tool_result = {
        "ok": True,
        "status": "done",
        "tool": "write_file",
        "result_format": "structured_json",
        "brief": "write_file 已完成。",
        "path": "result.md",
        "content": "完整工具返回必须进入事件 payload",
        "stop_after_tool": True,
    }
    events: list[dict[str, Any]] = []
    event_store = EventStore(tmp_path / "events.sqlite3")

    async def runtime_event_callback(event: dict[str, Any]) -> None:
        events.append(event)
        await event_store.append_event(
            event_type=event["event_type"],
            conversation_id="private:runtime",
            source=event.get("source"),
            tool_call_id=event.get("tool_call_id"),
            payload=event["payload"],
        )

    async def executor(name: str, args: dict[str, Any]) -> dict[str, Any]:
        assert name == "write_file"
        assert args == {"path": "result.md", "content": "done"}
        return dict(full_tool_result)

    runner = AgentRunner(
        ScriptedProvider(
            [
                CompletionResult(
                    tool_calls=[
                        ToolCall(
                            id="tc-write",
                            name="write_file",
                            arguments=raw_args,
                        )
                    ],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=5),
                )
            ]
        ),
        AgentConfig(provider="fake", model="fake", max_loops=3),
    )

    result = await runner.run(
        [{"role": "user", "content": "写结果"}],
        tools=[_tool_schema("write_file")],
        tool_executor=executor,
        runtime_event_callback=runtime_event_callback,
    )

    assert [event["event_type"] for event in events] == [
        "tool_call_started",
        "tool_result_received",
    ]
    started = events[0]
    received = events[1]
    assert started["source"] == "agent_runner"
    assert started["tool_call_id"] == "tc-write"
    assert started["payload"]["tool_name"] == "write_file"
    assert started["payload"]["args"] == {"path": "result.md", "content": "done"}
    assert started["payload"]["raw_arguments"] == raw_args
    assert started["payload"]["args_keys"] == ["content", "path"]
    assert started["payload"]["args_length"] > 0
    assert started["payload"]["loop"] == 1
    assert started["payload"]["step"] == 1
    assert received["payload"]["ok"] is True
    assert received["payload"]["args"] == {"path": "result.md", "content": "done"}
    assert received["payload"]["raw_arguments"] == raw_args
    assert received["payload"]["result"] == full_tool_result
    assert received["payload"]["result_keys"] == [
        "brief",
        "content",
        "ok",
        "path",
        "result_format",
        "status",
        "stop_after_tool",
        "tool",
    ]
    assert received["payload"]["result_length"] > 0
    assert len(started["payload"]["args_preview"]) <= 80
    assert len(received["payload"]["result_preview"]) <= 80
    event_json = json.dumps(events, ensure_ascii=False)
    assert '"content": "done"' in event_json
    assert "result.md" in event_json
    assert "完整工具返回必须进入事件 payload" in event_json

    assert await event_store.wait_projected(timeout=1.0)
    stored_events = await event_store.iter_events(limit=10)
    stored_started = stored_events[0]["payload"]
    stored_received = stored_events[1]["payload"]
    assert stored_started["args"] == {"path": "result.md", "content": "done"}
    assert stored_started["raw_arguments"] == raw_args
    assert stored_received["args"] == {"path": "result.md", "content": "done"}
    assert stored_received["raw_arguments"] == raw_args
    assert stored_received["result"] == full_tool_result
    await event_store.shutdown()

    assistant_record = result.records[0]
    tool_record = result.records[1]
    assert assistant_record["tool_calls"][0]["function"]["arguments"] == raw_args
    decoded_tool_result = json.loads(tool_record["content"])
    assert decoded_tool_result == full_tool_result
    assert decoded_tool_result["tool"] == "write_file"
    assert decoded_tool_result["result_format"] == "structured_json"


@pytest.mark.asyncio
async def test_runner_runtime_event_records_argument_parse_failure_result():
    events: list[dict[str, Any]] = []
    executor_calls: list[str] = []

    async def runtime_event_callback(event: dict[str, Any]) -> None:
        events.append(event)

    async def executor(name: str, _args: dict[str, Any]) -> dict[str, Any]:
        executor_calls.append(name)
        if name == "bad_tool":
            raise AssertionError("bad_tool should not execute")
        return {
            "ok": True,
            "status": "done",
            "tool": "no_action",
            "result_format": "structured_json",
            "brief": "本轮不执行操作。",
            "no_action": True,
        }

    runner = AgentRunner(
        ScriptedProvider(
            [
                CompletionResult(
                    tool_calls=[
                        ToolCall(id="tc-bad", name="bad_tool", arguments="{bad json")
                    ],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=5),
                ),
                CompletionResult(
                    tool_calls=[
                        ToolCall(id="tc-na", name="no_action", arguments="{}")
                    ],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=5),
                ),
            ]
        ),
        AgentConfig(provider="fake", model="fake", max_loops=3),
    )

    result = await runner.run(
        [{"role": "user", "content": "执行坏参数工具"}],
        tools=[_tool_schema("bad_tool"), _tool_schema("no_action")],
        tool_executor=executor,
        runtime_event_callback=runtime_event_callback,
    )

    bad_events = [event for event in events if event["tool_call_id"] == "tc-bad"]
    assert [event["event_type"] for event in bad_events] == ["tool_result_received"]
    bad_payload = bad_events[0]["payload"]
    assert bad_payload["ok"] is False
    assert bad_payload["error_type"] == "JSONDecodeError"
    assert bad_payload["raw_arguments"] == "{bad json"
    assert "args" not in bad_payload
    assert bad_payload["args_keys"] == []
    assert bad_payload["args_length"] == len("{bad json")
    assert bad_payload["result_hash"]
    assert bad_payload["result"]["ok"] is False
    assert bad_payload["result"]["tool"] == "bad_tool"
    assert executor_calls == ["no_action"]
    assert result.finish_reason == "no_action"
    first_tool_record = next(record for record in result.records if record["role"] == "tool")
    parse_result = json.loads(first_tool_record["content"])
    assert bad_payload["result"] == parse_result
    _assert_runner_error_envelope(
        parse_result,
        tool="bad_tool",
        brief="工具参数解析失败。",
    )
    assert parse_result["error"].startswith("参数解析失败")
    no_action_record = next(
        record for record in result.records if record.get("tool_call_id") == "tc-na"
    )
    no_action_result = json.loads(no_action_record["content"])
    assert no_action_result["tool"] == "no_action"
    assert no_action_result["result_format"] == "structured_json"


@pytest.mark.asyncio
async def test_runner_executor_exception_result_uses_stable_envelope():
    events: list[dict[str, Any]] = []
    executor_calls: list[str] = []

    async def runtime_event_callback(event: dict[str, Any]) -> None:
        events.append(event)

    async def executor(name: str, _args: dict[str, Any]) -> dict[str, Any]:
        executor_calls.append(name)
        if name == "boom_tool":
            raise RuntimeError("tool backend down")
        return {
            "ok": True,
            "status": "done",
            "tool": "no_action",
            "result_format": "structured_json",
            "brief": "本轮不执行操作。",
            "no_action": True,
        }

    runner = AgentRunner(
        ScriptedProvider(
            [
                CompletionResult(
                    tool_calls=[
                        ToolCall(
                            id="tc-boom",
                            name="boom_tool",
                            arguments='{"target": "x"}',
                        )
                    ],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=5),
                ),
                CompletionResult(
                    tool_calls=[
                        ToolCall(id="tc-na", name="no_action", arguments="{}")
                    ],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=5),
                ),
            ]
        ),
        AgentConfig(provider="fake", model="fake", max_loops=3),
    )

    result = await runner.run(
        [{"role": "user", "content": "执行会失败的工具"}],
        tools=[_tool_schema("boom_tool"), _tool_schema("no_action")],
        tool_executor=executor,
        runtime_event_callback=runtime_event_callback,
    )

    boom_events = [event for event in events if event["tool_call_id"] == "tc-boom"]
    assert [event["event_type"] for event in boom_events] == [
        "tool_call_started",
        "tool_result_received",
    ]
    boom_payload = boom_events[1]["payload"]
    assert boom_payload["ok"] is False
    assert boom_payload["status"] == "failed"
    assert boom_payload["error_type"] == "RuntimeError"
    assert executor_calls == ["boom_tool", "no_action"]
    assert result.finish_reason == "no_action"

    boom_record = next(
        record for record in result.records if record.get("tool_call_id") == "tc-boom"
    )
    boom_result = json.loads(boom_record["content"])
    _assert_runner_error_envelope(
        boom_result,
        tool="boom_tool",
        brief="工具执行失败。",
    )
    assert boom_result["error"] == "tool backend down"


class FailingProjectionStore(EventStore):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, projection_retry_delay=0.01, **kwargs)
        self.fail_projection = True
        self.projection_failed = threading.Event()

    def _project_events_sync(self, events):  # noqa: ANN001
        if self.fail_projection:
            self.projection_failed.set()
            raise RuntimeError("sqlite projection failed")
        return super()._project_events_sync(events)


async def _wait_thread_event(event: threading.Event, timeout: float = 1.0) -> None:
    assert await asyncio.wait_for(
        asyncio.to_thread(event.wait, timeout),
        timeout=timeout + 0.2,
    )


@pytest.mark.parametrize(
    ("failing_event_type", "expected_executor_calls"),
    [
        ("tool_call_started", []),
        ("tool_result_received", ["stop_tool"]),
    ],
)
@pytest.mark.asyncio
async def test_runner_runtime_event_callback_failure_propagates(
    failing_event_type: str,
    expected_executor_calls: list[str],
):
    event_types: list[str] = []
    executor_calls: list[str] = []
    tool_result = {
        "ok": True,
        "status": "done",
        "tool": "stop_tool",
        "result_format": "structured_json",
        "brief": "stop_tool 已完成。",
        "stop_after_tool": True,
    }

    async def runtime_event_callback(event: dict[str, Any]) -> None:
        event_types.append(event["event_type"])
        if event["event_type"] == failing_event_type:
            raise RuntimeError("event writer down")

    async def executor(name: str, _args: dict[str, Any]) -> dict[str, Any]:
        executor_calls.append(name)
        return dict(tool_result)

    runner = AgentRunner(
        ScriptedProvider(
            [
                CompletionResult(
                    tool_calls=[
                        ToolCall(id="tc-stop", name="stop_tool", arguments="{}")
                    ],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=5),
                )
            ]
        ),
        AgentConfig(provider="fake", model="fake", max_loops=3),
    )

    with pytest.raises(RuntimeError, match="event writer down"):
        await runner.run(
            [{"role": "user", "content": "执行工具"}],
            tools=[_tool_schema("stop_tool")],
            tool_executor=executor,
            runtime_event_callback=runtime_event_callback,
        )

    assert event_types == (
        ["tool_call_started"]
        if failing_event_type == "tool_call_started"
        else ["tool_call_started", "tool_result_received"]
    )
    assert executor_calls == expected_executor_calls


@pytest.mark.asyncio
async def test_pipeline_runtime_event_callback_appends_event_store_shape():
    class Store:
        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        async def append_event(self, **event: Any) -> int:
            self.events.append(event)
            return len(self.events)

    store = Store()
    callback = MessagePipeline._runtime_event_callback(
        SimpleNamespace(event_store=store),
        "private:123",
    )

    assert callback is not None
    await callback(
        {
            "event_type": "tool_call_started",
            "source": "agent_runner",
            "tool_call_id": "tc-1",
            "payload": {"tool_call_id": "tc-1", "tool_name": "write_file"},
        }
    )

    assert store.events == [
        {
            "event_type": "tool_call_started",
            "conversation_id": "private:123",
            "source": "agent_runner",
            "tool_call_id": "tc-1",
            "payload": {"tool_call_id": "tc-1", "tool_name": "write_file"},
        }
    ]


@pytest.mark.asyncio
async def test_pipeline_runtime_event_callback_append_failure_propagates():
    class Store:
        async def append_event(self, **_event: Any) -> int:
            raise OSError("append log failed")

    callback = MessagePipeline._runtime_event_callback(
        SimpleNamespace(event_store=Store()),
        "private:123",
    )

    assert callback is not None
    with pytest.raises(OSError, match="append log failed"):
        await callback(
            {
                "event_type": "tool_call_started",
                "source": "agent_runner",
                "tool_call_id": "tc-1",
                "payload": {"tool_call_id": "tc-1", "tool_name": "write_file"},
            }
        )


@pytest.mark.asyncio
async def test_sqlite_projection_failure_does_not_stop_agent_tool_execution(tmp_path):
    store = FailingProjectionStore(tmp_path / "events.sqlite3")
    callback = MessagePipeline._runtime_event_callback(
        SimpleNamespace(event_store=store),
        "private:123",
    )
    tool_result = {
        "ok": True,
        "status": "done",
        "tool": "stop_tool",
        "result_format": "structured_json",
        "brief": "stop_tool 已完成。",
        "stop_after_tool": True,
    }

    async def executor(_name: str, _args: dict[str, Any]) -> dict[str, Any]:
        return dict(tool_result)

    runner = AgentRunner(
        ScriptedProvider(
            [
                CompletionResult(
                    tool_calls=[
                        ToolCall(id="tc-stop", name="stop_tool", arguments="{}")
                    ],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=5),
                )
            ]
        ),
        AgentConfig(provider="fake", model="fake", max_loops=3),
    )

    assert callback is not None
    result = await runner.run(
        [{"role": "user", "content": "执行工具"}],
        tools=[_tool_schema("stop_tool")],
        tool_executor=executor,
        runtime_event_callback=callback,
    )

    assert result.finish_reason == "tool_stop"
    assert json.loads(result.records[1]["content"]) == tool_result
    await _wait_thread_event(store.projection_failed)
    stats = await store.stats()
    assert stats["last_appended_event_id"] == 2
    assert stats["projection_error_count"] >= 1

    store.fail_projection = False
    assert await store.wait_projected(2, timeout=1.0)
    await store.shutdown()
