"""Agent task tool execution tests."""

from __future__ import annotations

import pytest

from tests.tools.helpers import _approve_stub_tools, _make_config
from tools import ToolContext, build_default_registry


@pytest.mark.asyncio
async def test_summarize_conversation_starts_agent_task(tmp_path):
    from memory import ArchiveStore, HistoryManager

    archive = ArchiveStore(tmp_path / "archive.sqlite3")
    history = HistoryManager(tmp_path / "history.jsonl")
    await archive.append_many(
        [
            {
                "role": "user",
                "content": "归档里提到茶会安排",
                "conversation_id": "group:42",
            }
        ]
    )
    await history.add_user_message("活跃区继续讨论茶会", conversation_id="group:42")
    calls = []

    async def fake_agent_task(payload):
        calls.append(payload)
        return {
            "ok": True,
            "status": "completed",
            "task_id": "agent-test",
            "result_file": "agent_tasks/agent-test/result.md",
            "content": "茶会总结",
        }

    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(
        archive=archive,
        history=history,
        agent_task_cb=fake_agent_task,
        conversation_id="group:42",
    )
    _approve_stub_tools(ctx, "summarize_conversation")
    executor = reg.get_executor(ctx)

    result = await executor(
        "summarize_conversation",
        {"range_hint": "茶会", "max_tokens": 512},
    )

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["task_id"] == "agent-test"
    assert calls
    assert "茶会" in calls[0]["prompt"]
    assert calls[0]["sources"][0]["type"] == "conversation_history"
    assert calls[0]["sources"][0]["conversation_id"] == "group:42"
    assert calls[0]["sources"][0]["time_range"] == "茶会"


@pytest.mark.asyncio
async def test_start_agent_task_requires_prompt_and_calls_runtime():
    calls = []

    async def fake_agent_task(payload):
        calls.append(payload)
        return {
            "ok": True,
            "status": "completed",
            "task_id": "agent-1",
            "result_file": "agent_tasks/agent-1/result.md",
            "content": "提取结果",
        }

    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(agent_task_cb=fake_agent_task)
    _approve_stub_tools(ctx, "start_agent_task")
    executor = reg.get_executor(ctx)

    result = await executor(
        "start_agent_task",
        {
            "prompt": "提取对话，保留发送者",
            "sources": [{"type": "inline_text", "value": "A: hi"}],
            "output_format": "markdown",
            "max_loops": 30,
            "timeout_seconds": 900,
        },
    )

    assert result["ok"] is True
    assert result["task_id"] == "agent-1"
    assert calls[0]["prompt"] == "提取对话，保留发送者"
    assert calls[0]["sources"][0]["type"] == "inline_text"
    assert calls[0]["max_loops"] == 30
    assert calls[0]["timeout_seconds"] == 900


@pytest.mark.asyncio
async def test_start_agent_task_rejects_image_ref_without_vision_service():
    calls = []

    async def fake_agent_task(payload):
        calls.append(payload)
        return {"ok": True, "status": "completed"}

    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(agent_task_cb=fake_agent_task)
    _approve_stub_tools(ctx, "start_agent_task")
    executor = reg.get_executor(ctx)

    result = await executor(
        "start_agent_task",
        {
            "prompt": "看看这张图",
            "sources": [{"type": "image_ref", "value": "incoming/a.png"}],
            "output_format": "markdown",
        },
    )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "图片理解能力" in result["error"]
    assert calls == []


@pytest.mark.asyncio
async def test_start_agent_task_rejects_image_workspace_path_without_vision_service():
    calls = []

    async def fake_agent_task(payload):
        calls.append(payload)
        return {"ok": True, "status": "completed"}

    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(agent_task_cb=fake_agent_task)
    _approve_stub_tools(ctx, "start_agent_task")
    executor = reg.get_executor(ctx)

    result = await executor(
        "start_agent_task",
        {
            "prompt": "描述这张图片的内容",
            "sources": [{"type": "workspace_path", "value": "incoming/a.jpg"}],
            "output_format": "markdown",
        },
    )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "图片理解能力" in result["error"]
    assert calls == []


@pytest.mark.asyncio
async def test_start_agent_task_rejects_image_retry_after_describe_image_failure():
    class FailingVision:
        async def describe(self, image_url: str, prompt: str = ""):
            raise RuntimeError("vision failed")

    calls = []

    async def fake_agent_task(payload):
        calls.append(payload)
        return {"ok": True, "status": "completed"}

    cfg = _make_config(vision_enabled=True)
    reg = build_default_registry(cfg)
    ctx = ToolContext(vision=FailingVision(), agent_task_cb=fake_agent_task)
    _approve_stub_tools(ctx, "start_agent_task")
    executor = reg.get_executor(ctx)

    image_result = await executor(
        "describe_image",
        {"image_url": "https://example.com/a.jpg", "question": "看图"},
    )
    assert image_result["ok"] is False

    result = await executor(
        "start_agent_task",
        {
            "prompt": "描述这张图片的内容",
            "sources": [{"type": "workspace_path", "value": "incoming/a.jpg"}],
            "output_format": "markdown",
        },
    )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "不能启动子 Agent 代替看图" in result["error"]
    assert calls == []
