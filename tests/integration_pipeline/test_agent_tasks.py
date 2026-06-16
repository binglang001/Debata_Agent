"""Agent task integration pipeline tests."""

from __future__ import annotations

import asyncio
import json

import pytest

from agents.base import AgentRunResult
from providers.base import CompletionResult, ToolCall
from tests.integration_pipeline.helpers import (
    _ai_no_action,
    _ai_send_private,
    _ai_tool_search,
    _drain_pipeline,
    _msg,
)


@pytest.mark.asyncio

async def test_agent_task_materializes_sources_without_url(build_pipeline, tmp_path):

    pipeline, _, adapter, history, _ = await build_pipeline([])

    workspace = tmp_path / "workspace"

    workspace.mkdir()

    pipeline.workspace_dir = workspace

    (workspace / "input.md").write_text("已有文件", encoding="utf-8")

    await history.add_user_message("这里有 msg_id=abc123 的记录", conversation_id="private:123")

    task_dir = workspace / "agent_tasks" / "manual"

    task_dir.mkdir(parents=True)

    async def fake_get_forward_msg(forward_id: str):

        if forward_id == "outer":

            return [

                {

                    "sender": {"nickname": "Lilith"},

                    "raw_message": "[CQ:forward,id=inner]",

                }

            ]

        return [

            {

                "sender": {"nickname": "Diana"},

                "raw_message": "内层消息",

            }

        ]

    adapter.get_forward_msg = fake_get_forward_msg  # type: ignore[method-assign]

    manifest = await pipeline._materialize_agent_task_sources(

        [

            {"type": "workspace_path", "value": "input.md"},

            {"type": "inline_text", "value": "内联材料"},

            {"type": "message_id", "value": "abc123"},

            {"type": "image_ref", "value": "https://example.com/a.png"},

            {"type": "forward_id", "value": "outer"},

        ],

        task_dir,

    )

    assert manifest["count"] == 5

    assert manifest["sources"][0]["path"] == "input.md"

    inline_path = workspace / manifest["sources"][1]["path"]

    assert inline_path.read_text(encoding="utf-8") == "内联材料"

    assert manifest["sources"][2]["record_count"] == 1

    assert "暂不支持直接传 URL" in manifest["sources"][3]["error"]

    assert manifest["sources"][4]["message_count"] == 2

    assert manifest["sources"][4]["nested_forward_count"] == 1

    forward_tree = json.loads(

        (workspace / manifest["sources"][4]["path"]).read_text(encoding="utf-8")

    )

    assert forward_tree["type"] == "forward"

    assert forward_tree["messages"][0]["segments"][0]["node"]["forward_id"] == "inner"

@pytest.mark.asyncio

async def test_agent_task_max_loops_returns_partial_result(build_pipeline, tmp_path):

    pipeline, _, _, _, _ = await build_pipeline([])

    workspace = tmp_path / "workspace"

    workspace.mkdir()

    pipeline.workspace_dir = workspace

    async def fake_run(_messages, *, max_loops=None, **_kwargs):

        assert max_loops == 17

        return AgentRunResult(

            final_content="还没完全整理完",

            records=[{"role": "assistant", "content": "正在整理资料"}],

            loop_count=17,

            finish_reason="max_loops",

        )

    pipeline.chat_agent.run = fake_run  # type: ignore[method-assign]

    result = await pipeline._run_agent_task(

        "task-partial",

        {

            "prompt": "整理资料并输出 Markdown",

            "max_loops": 17,

            "output_format": "markdown",

            "output_name": "result.md",

        },

        conversation_id="private:123",

        default_target=None,

    )

    result_path = workspace / "agent_tasks" / "task-partial" / "result.md"

    assert result_path.exists()

    text = result_path.read_text(encoding="utf-8")

    assert "部分结果" in text

    assert "整理资料并输出 Markdown" in text

    assert result["ok"] is True

    assert result["status"] == "partial"

    assert result["result_file"] == "agent_tasks/task-partial/result.md"

    assert "工具循环最终收尾条件" in result["error"]

    assert "content" in result and "部分结果" in result["content"]

@pytest.mark.asyncio

async def test_agent_task_returns_after_target_output_written(build_pipeline, tmp_path):

    output_name = "target_result.md"

    write_args = {

        "path": f"agent_tasks/task-write/{output_name}",

        "content": "# 结果\n\n完成。",

    }

    pipeline, provider, _, _, _ = await build_pipeline(

        [

            CompletionResult(

                tool_calls=[

                    ToolCall(

                        id="tc-write-target",

                        name="write_file",

                        arguments=json.dumps(write_args),

                    )

                ],

                finish_reason="tool_calls",

            ),

            _ai_no_action(),

        ]

    )

    workspace = tmp_path / "workspace"

    workspace.mkdir()

    pipeline.workspace_dir = workspace

    result = await pipeline._run_agent_task(

        "task-write",

        {

            "prompt": "写出结果文件",

            "output_format": "markdown",

            "output_name": output_name,

        },

        conversation_id="private:123",

        default_target=None,

    )

    assert len(provider.calls) == 1

    result_path = workspace / "agent_tasks" / "task-write" / output_name

    assert result_path.exists()

    assert result["status"] == "completed"

    assert result["result_file"] == f"agent_tasks/task-write/{output_name}"

    assert result["content"] == "# 结果\n\n完成。"

    assert result["data"]["finish_reason"] == "tool_stop"

@pytest.mark.asyncio

async def test_agent_task_timeout_returns_existing_target_output(

    build_pipeline,

    tmp_path,

    monkeypatch,

):

    pipeline, _, _, _, _ = await build_pipeline([])

    workspace = tmp_path / "workspace"

    workspace.mkdir()

    pipeline.workspace_dir = workspace

    import core.message_pipeline as message_pipeline

    monkeypatch.setattr(

        message_pipeline,

        "_agent_task_timeout_seconds",

        lambda *_args, **_kwargs: 0.01,

    )

    async def fake_run(_messages, **_kwargs):

        result_path = workspace / "agent_tasks" / "task-timeout" / "result.md"

        result_path.parent.mkdir(parents=True, exist_ok=True)

        result_path.write_text("# 结果\n\n已经写出。", encoding="utf-8")

        await asyncio.sleep(3600)

    pipeline.chat_agent.run = fake_run  # type: ignore[method-assign]

    result = await pipeline._run_agent_task(

        "task-timeout",

        {

            "prompt": "写出结果后模拟挂起",

            "output_format": "markdown",

            "output_name": "result.md",

        },

        conversation_id="private:123",

        default_target=None,

    )

    result_path = workspace / "agent_tasks" / "task-timeout" / "result.md"

    assert result_path.exists()

    assert result["ok"] is True

    assert result["status"] == "completed"

    assert result["result_file"] == "agent_tasks/task-timeout/result.md"

    assert "超时" in result["error"]

    assert pipeline._agent_task_meta["task-timeout"]["timeout_with_existing_output"] is True

@pytest.mark.asyncio

async def test_start_agent_task_result_is_in_band_same_turn(build_pipeline, tmp_path):

    start_args = {

        "prompt": "整理资料",

        "sources": [{"type": "inline_text", "value": "资料"}],

        "output_format": "markdown",

    }

    pipeline, provider, adapter, history, _ = await build_pipeline(

        [

            _ai_tool_search("start_agent_task"),

            CompletionResult(

                tool_calls=[

                    ToolCall(

                        id="tc-start",

                        name="start_agent_task",

                        arguments=json.dumps(start_args),

                    )

                ],

                finish_reason="tool_calls",

            ),

            _ai_send_private(target_qq="123", content="后台结果已完成"),

        ]

    )

    workspace = tmp_path / "workspace"

    workspace.mkdir()

    pipeline.workspace_dir = workspace

    async def fake_run_agent_task(task_id, _payload, *, conversation_id, default_target):

        result_path = workspace / "agent_tasks" / task_id / "result.md"

        result_path.parent.mkdir(parents=True)

        result_path.write_text("# 结果\n\n完成。", encoding="utf-8")

        return {

            "ok": True,

            "status": "completed",

            "brief": "子 Agent 已完成：结果",

            "task_id": task_id,

            "result_file": f"agent_tasks/{task_id}/result.md",

            "path": f"agent_tasks/{task_id}/result.md",

            "content": "# 结果\n\n完成。",

            "summary": "结果",

            "data": {"task_id": task_id, "result_file": f"agent_tasks/{task_id}/result.md"},

        }

    pipeline._run_agent_task = fake_run_agent_task  # type: ignore[method-assign]

    await pipeline.enqueue(_msg(user_id="123", text="先把能做的完成", message_id="m-start"))

    await _drain_pipeline(pipeline, max_wait=3.0)

    assert len(provider.calls) == 4

    second_messages = provider.calls[2]["messages"]

    tool_records = [m for m in second_messages if m.get("role") == "tool"]

    assert tool_records

    assert "agent_tasks/" in tool_records[-1]["content"]

    assert "agent_task_result" not in "\n".join(str(m.get("content") or "") for m in second_messages)

    assert any(

        sent_target.target_id == "123" and text == "后台结果已完成"

        for sent_target, text in adapter.sent

    )

    records = await history.records()

    joined_records = "\n".join(str(record.get("content", "")) for record in records)

    assert "agent_task_result" not in joined_records

    assert "后台结果已完成" in joined_records
