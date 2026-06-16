"""Runtime smoke coverage for all registered tools."""

from __future__ import annotations

import json

import pytest

from core.chat_timeline import ChatTimelineStore
from tests.tools.helpers import (
    FakeTTS,
    FakeVision,
    FakeWeather,
    FakeWebSearch,
    FullFakeAdapter,
    _approve_stub_tools,
    _assert_tool_result_envelope,
    _timeline_message,
)
from tools import ToolContext, ToolRegistry, get_default_specs


@pytest.mark.asyncio
async def test_all_tools_have_clear_results_in_simulated_runtime(tmp_path):
    """逐个调用所有工具，检查真实执行链路下的返回结构足够清晰。"""
    from memory import ArchiveStore, HistoryManager, ImportantMemoryManager

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("old line\nsecond line\n", encoding="utf-8")
    (workspace / "delete-me.txt").write_text("delete", encoding="utf-8")
    upload_path = workspace / "report.md"
    upload_path.write_text("# report\n", encoding="utf-8")

    history = HistoryManager(tmp_path / "history.jsonl")
    await history.load()
    await history.add_user_message(
        "历史消息",
        metadata={"timestamp": "2026-06-01 12:00:00"},
        conversation_id="private:123",
    )
    archive = ArchiveStore(tmp_path / "archive.sqlite3")
    await archive.load()
    await archive.append_many(
        [
            {
                "role": "user",
                "content": "归档消息 keyword",
                "conversation_id": "private:123",
                "metadata": {"timestamp": "2026-06-01 11:00:00"},
            }
        ]
    )
    important = ImportantMemoryManager(tmp_path / "important.json")
    await important.load()
    await important.replace_all(
        [{"id": "mem-existing", "timestamp": "T0", "content": "用户喜欢绿茶"}]
    )

    timeline = ChatTimelineStore()
    timeline.append(_timeline_message("m1", "最近消息"))
    wakeups: list[dict] = []
    agent_tasks: list[dict] = []
    sent_actions: list[dict] = []

    async def fake_wakeup(delay_seconds, reminder, target, mode, message_text):
        wakeups.append(
            {
                "delay_seconds": delay_seconds,
                "reminder": reminder,
                "target": target,
                "mode": mode,
                "message_text": message_text,
            }
        )

    async def fake_agent_task(payload):
        agent_tasks.append(payload)
        return {
            "ok": True,
            "status": "completed",
            "task_id": "agent-test",
            "brief": "子 Agent 已完成。",
            "result_file": "agent_tasks/agent-test/result.md",
            "content": "子 Agent 结果",
            "summary": "子 Agent 结果",
        }

    async def fake_send_actions(actions, source_tool, *, metadata=None):
        sent_actions.append({"source_tool": source_tool, "actions": actions, "metadata": metadata})
        if source_tool == "commit_send_attempt":
            return {
                "ok": True,
                "status": "already_committed",
                "send_attempt_id": (metadata or {}).get("commit_send_attempt_id"),
                "qq_visible": False,
            }
        return {
            "ok": True,
            "status": "sent",
            "qq_visible": True,
            "send_id": "send-test",
            "count": len(actions),
            "sent": [
                {
                    "conversation_id": f"{a['target_scope']}:{a['target_id']}",
                    "msg_id": f"msg-{idx}",
                    "order": a.get("order", idx),
                    "qq_visible": True,
                }
                for idx, a in enumerate(actions, start=1)
            ],
        }

    class FakePersonaAgent:
        def __init__(self) -> None:
            self.eat_calls: list[tuple[str, int, str]] = []
            self.sleep_calls: list[tuple[int, str]] = []

        async def on_eat_start(
            self,
            meal_type: str,
            duration_minutes: int,
            description: str,
        ) -> dict:
            self.eat_calls.append((meal_type, duration_minutes, description))
            return {"status": "started", "record_id": "eat-runtime"}

        async def on_sleep_start(
            self,
            duration_minutes: int,
            *,
            reason: str,
        ) -> dict:
            self.sleep_calls.append((duration_minutes, reason))
            return {"status": "started", "record_id": "sleep-runtime"}

    adapter = FullFakeAdapter()
    persona_agent = FakePersonaAgent()
    ctx = ToolContext(
        adapter=adapter,
        important=important,
        conversation_id="private:123",
        history=history,
        archive=archive,
        vision=FakeVision(),
        web_search=FakeWebSearch(),
        weather=FakeWeather(),
        tts=FakeTTS(workspace),
        wakeup_cb=fake_wakeup,
        workspace_dir=workspace,
        send_actions_cb=fake_send_actions,
        agent_task_cb=fake_agent_task,
        persona_agent=persona_agent,
        extras={
            "chat_timeline": timeline,
            "default_reply_target": {"target_type": "private", "target_id": 123},
            "latest_user_message": "帮我测试工具",
            "self_id": "999",
        },
    )
    _approve_stub_tools(ctx, "filter_archive_records")
    reg = ToolRegistry(get_default_specs())
    executor = reg.get_executor(ctx)

    calls: dict[str, dict] = {
        "start_agent_task": {
            "prompt": "读取资料并写出结果",
            "sources": [{"type": "inline_text", "value": "资料"}],
            "output_format": "markdown",
            "output_name": "result.md",
            "max_loops": 5,
        },
        "no_action": {},
        "schedule_wakeup": {
            "delay_seconds": 1,
            "mode": "send_message",
            "message_text": "提醒",
        },
        "describe_image": {"image_url": "https://example.com/a.jpg", "question": "看图"},
        "web_search": {"query": "Debata"},
        "get_weather": {"city": "北京", "days": 1},
        "send_voice_message": {
            "target_type": "private",
            "target_id": 123,
            "text": "语音测试",
            "prompt": "年轻女性，自然口语",
            "ignore_review_interrupts": True,
        },
        "save_important_memory": {"memory_text": "用户喜欢红茶", "scope": "user:123"},
        "update_important_memory": {
            "memory_id": "mem-existing",
            "memory_text": "用户喜欢红茶和乌龙茶",
        },
        "delete_important_memory": {"memory_id": "mem-existing"},
        "send_private_messages": {
            "targets": [{"target_qq": 123, "content": "你好", "order": 1, "delay": 0}],
        },
        "send_group_message": {
            "group_id": 456,
            "targets": [{"content": "群消息", "order": 1, "delay": 0}],
        },
        "commit_send_attempt": {
            "send_attempt_id": "attempt-test",
            "reviewed_until_seq": 1,
            "delivery_interrupt_policy": "interrupt_priority",
        },
        "recall_message": {"message_id": 100},
        "upload_file": {
            "target_type": "private",
            "target_id": 123,
            "file_path": "report.md",
        },
        "list_contacts": {"scope": "friends", "limit": 10},
        "get_user_info": {"user_id": 1001},
        "get_forward_msg": {"forward_id": "root", "recursive": True, "max_depth": 2},
        "get_recent_chat_messages": {"conversation_id": "private:123", "limit": 10},
        "get_msg": {"message_id": 321},
        "send_poke": {
            "user_id": 1001,
            "group_id": 456,
            "reason": "用户明确要求戳一戳",
        },
        "set_msg_emoji_like": {
            "message_id": 321,
            "emoji_id": "76",
            "set": True,
            "reason": "用表情轻量回应",
        },
        "set_friend_add_request": {"flag": "friend-flag", "approve": True, "remark": "A"},
        "set_group_add_request": {
            "flag": "group-flag",
            "sub_type": "add",
            "approve": False,
            "reason": "拒绝",
        },
        "get_group_self_role": {"group_id": 456},
        "set_group_kick": {
            "group_id": 456,
            "user_id": 1002,
            "reason": "管理员明确要求测试",
        },
        "set_group_ban": {
            "group_id": 456,
            "user_id": 1002,
            "duration_seconds": 600,
            "reason": "管理员明确要求测试",
        },
        "set_group_whole_ban": {
            "group_id": 456,
            "enable": True,
            "reason": "管理员明确要求测试",
        },
        "set_group_leave": {
            "group_id": 456,
            "reason": "管理员明确要求测试",
        },
        "summarize_chat_history": {"group_id": 456, "custom_prompt": "总结"},
        "summarize_conversation": {
            "conversation_id": "private:123",
            "range_hint": "2026-06-01",
            "goal": "总结",
        },
        "filter_archive_records": {"keywords": ["keyword"], "limit": 5},
        "recall_history": {"conversation_id": "private:123", "keyword": "keyword", "limit": 5},
        "read_file": {"path": "note.txt", "max_lines": 10},
        "write_file": {"path": "created.txt", "content": "created"},
        "edit_file": {"path": "note.txt", "old": "old line", "new": "new line"},
        "list_files": {"path": ".", "pattern": "*.txt", "limit": 20},
        "delete_file": {"path": "delete-me.txt"},
        "run_python": {"code": "print('ok')", "timeout_seconds": 5},
        "eat": {
            "meal_type": "早餐",
            "duration_minutes": 20,
            "description": "豆浆和包子",
        },
        "sleep": {"duration_minutes": 90, "reason": "午休"},
        "tool_search": {"tool_name": "upload_file", "intent": "测试工具详情查询"},
    }
    assert set(calls) == {spec.name for spec in get_default_specs()}

    results: dict[str, dict] = {}
    for name, args in calls.items():
        result = await executor(name, args)
        results[name] = result
        assert isinstance(result, dict), name
        _assert_tool_result_envelope(result, name)
        assert "ok" in result, name
        if result.get("ok"):
            assert any(k in result for k in ("brief", "status", "data", "sent", "scheduled", "path")), name
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
        assert "_condensed" not in result, name
        assert len(encoded) < 12000, name

    assert results["get_forward_msg"]["status"] == "artifact"
    assert results["get_forward_msg"]["artifact"]["path"].startswith("runtime/forwards/")
    assert results["get_forward_msg"]["data"]["nested_forward_count"] == 1
    forward_artifact = workspace / results["get_forward_msg"]["artifact"]["path"]
    forward_tree = json.loads(forward_artifact.read_text(encoding="utf-8"))
    assert forward_tree["messages"][0]["segments"][1]["url"] == "https://example.com/a.jpg"
    assert (
        forward_tree["messages"][1]["segments"][0]["node"]["messages"][0]
        ["segments"][0]["text"]
        == "内层消息"
    )

    assert results["get_recent_chat_messages"]["content"].count("最近消息") == 1
    assert results["get_recent_chat_messages"]["data"]["range"] == "continuous"
    assert results["get_recent_chat_messages"]["data"]["last_msg_id"] == "m1"
    assert results["get_msg"]["content"] == "单条消息内容"
    assert results["get_msg"]["data"]["conversation_id"] == "group:456"
    assert persona_agent.eat_calls == [("早餐", 20, "豆浆和包子")]
    assert persona_agent.sleep_calls == [(90, "午休")]
    assert results["eat"]["result"]["record_id"] == "eat-runtime"
    assert results["sleep"]["result"]["record_id"] == "sleep-runtime"

    assert results["read_file"]["data"]["range"] == "continuous_page"
    assert "old line" in results["read_file"]["content"]

    assert results["start_agent_task"]["task_id"] == "agent-test"
    assert results["start_agent_task"]["status"] == "completed"
    assert results["start_agent_task"]["result_file"] == "agent_tasks/agent-test/result.md"
    assert agent_tasks[0]["sources"][0]["type"] == "inline_text"
    assert agent_tasks[0]["max_loops"] == 5

    assert results["no_action"]["no_action"] is True
    assert results["no_action"]["brief"] == "本轮不执行操作。"
    assert results["schedule_wakeup"]["scheduled"] is True
    assert wakeups[0]["delay_seconds"] == 1
    assert wakeups[0]["target"] == {"target_type": "private", "target_id": 123}
    assert "提醒" in wakeups[0]["reminder"]

    assert results["describe_image"]["summary"] == "一张测试图片"
    assert results["describe_image"]["data"]["image_ref"] == "https://example.com/a.jpg"
    assert "问题=看图" in results["describe_image"]["description"]
    assert results["web_search"]["query"] == "Debata"
    assert "https://example.com/search?q=Debata" in results["web_search"]["result"]
    assert results["get_weather"]["data"] == {"city": "北京", "days": 1}
    assert "北京 1 天天气晴" in results["get_weather"]["result"]

    assert results["send_voice_message"]["status"] == "sent"
    assert results["send_private_messages"]["status"] == "sent"
    assert results["send_group_message"]["status"] == "sent"
    for name in (
        "send_private_messages",
        "send_group_message",
        "no_action",
        "commit_send_attempt",
    ):
        _assert_tool_result_envelope(results[name], name)
    non_commit_sends = [
        item for item in sent_actions if item["source_tool"] != "commit_send_attempt"
    ]
    assert [
        item["source_tool"] for item in non_commit_sends
    ] == ["send_voice_message", "send_private_messages", "send_group_message"]
    assert results["commit_send_attempt"]["status"] == "already_committed"
    assert sent_actions[0]["actions"][0]["kind"] == "voice"
    assert sent_actions[0]["metadata"]["ignore_review_interrupts"] is True
    assert sent_actions[1]["actions"][0]["target_scope"] == "private"
    assert sent_actions[1]["actions"][0]["content"] == "你好"
    assert sent_actions[2]["actions"][0]["target_scope"] == "group"
    assert sent_actions[2]["actions"][0]["target_id"] == "456"

    assert results["save_important_memory"]["saved"] is True
    assert results["save_important_memory"]["memory_id"].startswith("mem_")
    assert results["save_important_memory"]["scope"] == "user:123"
    assert results["update_important_memory"]["updated"] is True
    assert results["delete_important_memory"]["deleted"] == 1
    assert len(important.items()) == 1
    assert important.items()[0]["id"] == results["save_important_memory"]["memory_id"]

    assert results["recall_message"]["data"]["message_id"] == "100"
    assert adapter.recalled == "100"
    assert results["upload_file"]["data"]["target_type"] == "private"
    assert adapter.uploaded["file_path"] == upload_path
    assert adapter.uploaded["display_name"] == "report.md"
    assert results["tool_search"]["status"] == "found"
    assert results["tool_search"]["result_type"] == "tool_metadata"
    assert results["tool_search"]["tool_name"] == "upload_file"
    assert "parameters_schema" not in results["tool_search"]
    assert "file_path" in {
        item["name"] for item in results["tool_search"]["parameters"]
    }

    assert results["list_contacts"]["data"]["scope"] == "friends"
    assert results["list_contacts"]["friends"][0]["nickname"] == "Alice"
    assert results["get_user_info"]["data"]["user_id"] == "1001"
    assert results["get_user_info"]["info"]["nickname"] == "Alice"
    assert results["send_poke"]["status"] == "done"
    assert results["send_poke"]["data"]["group_id"] == "456"
    assert results["set_msg_emoji_like"]["status"] == "done"
    assert results["set_msg_emoji_like"]["data"]["emoji_id"] == "76"
    assert adapter.friend_request == {"flag": "friend-flag", "approve": True, "remark": "A"}
    assert adapter.group_request == {
        "flag": "group-flag",
        "sub_type": "add",
        "approve": False,
        "reason": "拒绝",
    }
    assert results["get_group_self_role"]["role"] == "admin"
    assert results["set_group_kick"]["status"] == "done"
    assert results["set_group_ban"]["status"] == "done"
    assert results["set_group_whole_ban"]["status"] == "done"
    assert results["set_group_leave"]["status"] == "done"
    assert [call[0] for call in adapter.api_calls].count("set_group_ban") == 1
    assert [call[0] for call in adapter.api_calls].count("send_poke") == 1
    assert [call[0] for call in adapter.api_calls].count("set_msg_emoji_like") == 1

    assert results["summarize_chat_history"]["task_id"] == "agent-test"
    assert agent_tasks[1]["output_name"] == "group_456_summary.md"
    assert agent_tasks[1]["sources"][0]["data"]["messages"][0]["raw_message"] == "群历史"
    assert results["summarize_conversation"]["task_id"] == "agent-test"
    assert agent_tasks[2]["output_name"] == "conversation_summary.md"
    assert agent_tasks[2]["sources"][0]["conversation_id"] == "private:123"
    assert results["filter_archive_records"]["count"] == 1
    _assert_tool_result_envelope(
        results["filter_archive_records"],
        "filter_archive_records",
    )
    assert results["filter_archive_records"]["results"][0]["id"] == "a1"
    assert "content" not in results["filter_archive_records"]["results"][0]
    assert "归档消息 keyword" in results["filter_archive_records"]["results"][0]["snippet"]
    assert results["recall_history"]["count"] == 1
    _assert_tool_result_envelope(results["recall_history"], "recall_history")
    assert "归档消息 keyword" in results["recall_history"]["content"]

    assert results["write_file"]["path"] == "created.txt"
    assert (workspace / "created.txt").read_text(encoding="utf-8") == "created"
    assert results["edit_file"]["data"]["old_length"] == len("old line")
    assert (workspace / "note.txt").read_text(encoding="utf-8").startswith("new line")
    assert results["list_files"]["data"]["count"] >= 1
    assert any(item["path"] == "created.txt" for item in results["list_files"]["entries"])
    assert results["delete_file"]["path"] == "delete-me.txt"
    assert not (workspace / "delete-me.txt").exists()
    assert results["run_python"]["returncode"] == 0
    assert results["run_python"]["stdout"].strip() == "ok"

