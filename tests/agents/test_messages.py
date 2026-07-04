"""测试 agents 层：消息构建、任务上下文与记忆写入钩子。"""

from __future__ import annotations

import pytest

from agents.context_builder import build_messages, build_task_context
from agents.persona_loader import Persona
from providers.base import normalize_messages


def _persona(prompt: str = "你是 Debata", admins: list[dict] | None = None) -> Persona:
    return Persona(
        name="test",
        prompt=prompt,
        vars={"name": "Debata", "admins": admins or []},
    )

def test_build_task_context_empty():
    assert build_task_context("") == ""


def test_build_task_context_with_content():
    s = build_task_context("现在是 2026 年 5 月")
    assert "<task_context" in s
    assert "现在是 2026 年 5 月" in s


def test_build_task_context_with_refocus():
    s = build_task_context("ctx", refocus_hint="本轮目标：回应 Lily")
    assert "ctx" in s
    assert "本轮焦点提醒" in s
    assert "Lily" in s


def test_build_task_context_with_persona_context():
    s = build_task_context(
        "现在是 2026 年 5 月",
        persona_context="<persona_context>精力变化摘要</persona_context>",
    )
    assert "现在是 2026 年 5 月" in s
    assert "<persona_context>精力变化摘要</persona_context>" in s
    assert "不是用户新发言" in s


def test_build_messages_structure():
    p = _persona(admins=[{"qq": 1, "name": "A"}])
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    msgs = build_messages(
        p,
        history,
        important_memory_text="[重要记忆]\n- X",
        current_context="时间：2026/05/23",
    )

    # 顺序：system(combined) → system(admin) → user → assistant → user(task_context)
    assert msgs[0]["role"] == "system"
    assert "<persona" in msgs[0]["content"]
    assert msgs[1]["role"] == "system"
    assert "<admin_info" in msgs[1]["content"]
    assert msgs[2]["role"] == "user"
    assert msgs[3]["role"] == "assistant"
    assert msgs[4]["role"] == "user"
    assert "<task_context" in msgs[4]["content"]
    assert "不是用户新发言" in msgs[4]["content"]
    assert "2026/05/23" in msgs[4]["content"]


def test_build_messages_no_admin_no_context():
    p = _persona(admins=[])
    msgs = build_messages(p, [], important_memory_text="", current_context="")
    # 只有 1 个 system（combined system prompt）
    assert len(msgs) == 1
    assert msgs[0]["role"] == "system"


def test_build_messages_system_override():
    """system_override 应跳过结构化拼接。"""
    p = _persona()
    msgs = build_messages(
        p,
        [{"role": "user", "content": "x"}],
        system_override="自定义 system",
    )
    assert msgs[0]["content"] == "自定义 system"
    # 不应包含 persona/core_rules
    assert "core_rules" not in msgs[0]["content"]


def test_build_messages_memory_mode_propagates():
    p = _persona()
    msgs_rag = build_messages(p, [], memory_mode="rag")
    msgs_file = build_messages(p, [], memory_mode="file")
    assert "RAG 会话向量检索" in msgs_rag[0]["content"]
    assert "必须主动保存" not in msgs_rag[0]["content"]
    assert "必须主动保存" in msgs_file[0]["content"]


def test_build_messages_propagates_persona_context_and_physiology_tools():
    p = _persona()
    msgs = build_messages(
        p,
        [],
        current_context="现在是 10:00",
        persona_context="<persona_context>动态人格状态</persona_context>",
        eat_tool=True,
        sleep_tool=True,
    )

    assert "<physiology>" in msgs[0]["content"]
    assert "meal_type" in msgs[0]["content"]
    assert "duration_minutes 填 1-720 分钟" in msgs[0]["content"]
    assert msgs[-1]["role"] == "user"
    assert "现在是 10:00" in msgs[-1]["content"]
    assert "<persona_context>动态人格状态</persona_context>" in msgs[-1]["content"]


def test_build_messages_rag_memory_is_tail_context_for_cache_stability():
    p = _persona()
    history = [{"role": "user", "content": "旧消息"}]
    first = build_messages(
        p,
        history,
        important_memory_text="重要记忆",
        rag_context_text="RAG 片段 A",
        current_context="现在是 10:00",
        memory_mode="rag",
    )
    second = build_messages(
        p,
        history,
        important_memory_text="重要记忆",
        rag_context_text="RAG 片段 B",
        current_context="现在是 10:00",
        memory_mode="rag",
    )

    assert first[0]["content"] == second[0]["content"]
    assert "RAG 片段 A" not in first[0]["content"]
    assert "RAG 片段 B" not in second[0]["content"]
    assert "重要记忆" in first[0]["content"]
    assert "<long_term_memory" in first[0]["content"]
    assert [m["role"] for m in first] == ["system", "user", "user", "user"]
    assert "旧消息" in first[1]["content"]
    assert "task_context" in first[2]["content"]
    assert "不是用户新发言" in first[2]["content"]
    assert "RAG 片段 A" in first[3]["content"]
    assert "不是用户新发言" in first[3]["content"]


def test_build_messages_can_reuse_persisted_task_context_record_for_prefix_stability():
    p = _persona()
    history = [{"role": "user", "content": "本轮用户消息"}]
    task_record = {
        "role": "user",
        "content": "<task_context priority=\"medium\">\n现在是 10:00。\n</task_context>",
        "metadata": {"kind": "task_context_snapshot"},
        "conversation_id": "private:1",
    }

    current = build_messages(
        p,
        history,
        current_context_record=task_record,
        memory_mode="rag",
    )
    next_turn = build_messages(
        p,
        [*history, task_record, {"role": "assistant", "content": ""}],
        current_context_record={
            "role": "user",
            "content": "<task_context priority=\"medium\">\n现在是 10:01。\n</task_context>",
        },
        memory_mode="rag",
    )

    assert normalize_messages(current[:3]) == normalize_messages(next_turn[:3])
    assert current[2]["content"] == task_record["content"]


def test_build_messages_does_not_duplicate_persona_context_with_task_record():
    p = _persona()
    task_record = {
        "role": "user",
        "content": "<task_context priority=\"medium\">\n已持久化上下文\n</task_context>",
        "metadata": {"kind": "task_context_snapshot"},
        "conversation_id": "private:1",
    }

    msgs = build_messages(
        p,
        [],
        current_context_record=task_record,
        persona_context="<persona_context>不应重复追加</persona_context>",
    )

    assert msgs[-1]["content"] == task_record["content"]
    assert "不应重复追加" not in "\n".join(str(msg["content"]) for msg in msgs)


def test_turn_summary_labels_assistant_as_current_persona_reply():
    from core.pipeline_task_context import _compact_chat_summary

    summary = _compact_chat_summary(
        user_or_event_text="[系统事件] 主动思考触发",
        task_context="人格待办：提醒主人喝水",
        records=[{"role": "assistant", "content": "该去喝水了"}],
    )

    assert "外部输入/系统事件：[系统事件] 主动思考触发" in summary
    assert "系统上下文：人格待办：提醒主人喝水" in summary
    assert "当前人格自己的回复：该去喝水了" in summary
    assert "用户/事件：" not in summary
    assert "助手：" not in summary


def test_build_messages_preserves_complete_tool_call_group():
    p = _persona()
    msgs = build_messages(
        p,
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc-ok",
                        "type": "function",
                        "function": {"name": "no_action", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tc-ok", "content": '{"no_action": true}'},
        ],
    )

    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["tool_calls"][0]["id"] == "tc-ok"
    assert msgs[2]["role"] == "tool"
    assert msgs[2]["tool_call_id"] == "tc-ok"


def test_build_messages_converts_orphan_tool_result_without_dropping_content():
    p = _persona()
    msgs = build_messages(
        p,
        [
            {"role": "tool", "tool_call_id": "tc-lost", "content": '{"ok": true}'},
            {"role": "user", "content": "后续消息"},
        ],
    )

    assert msgs[1]["role"] == "system"
    assert "historical_tool_record_unreplayable" in msgs[1]["content"]
    assert "tc-lost" in msgs[1]["content"]
    assert "ok" in msgs[1]["content"]
    assert "true" in msgs[1]["content"]
    assert msgs[2]["role"] == "user"
    assert msgs[2]["content"] == "后续消息"


def test_build_messages_user_event_is_final_user_message():
    p = _persona()
    msgs = build_messages(
        p,
        [{"role": "assistant", "content": "旧回复"}],
        current_context="现在是 10:00",
        user_event="[系统事件 · 非用户消息] 定时唤醒已到。",
    )

    assert msgs[-1]["role"] == "user"
    assert "系统事件" in msgs[-1]["content"]
    assert msgs[-2]["role"] == "user"
    assert "task_context" in msgs[-2]["content"]


# ============================================================
# Memory hooks（新加的 on_append）
# ============================================================


@pytest.mark.asyncio
async def test_history_on_append_called(tmp_path):
    """订阅 on_append 后，每次写入都应触发回调。"""
    from memory import HistoryManager

    h = HistoryManager(tmp_path / "h.jsonl")
    received: list[list[dict]] = []

    async def cb(records):
        received.append(records)

    h.on_append(cb)
    await h.add_user_message("hi")
    await h.add_assistant_message("hello")

    # 给 task 时间执行
    import asyncio
    await asyncio.sleep(0.05)

    assert len(received) == 2
    assert received[0][0]["role"] == "user"
    assert received[1][0]["role"] == "assistant"


@pytest.mark.asyncio
async def test_history_on_append_batch(tmp_path):
    from memory import HistoryManager

    h = HistoryManager(tmp_path / "h.jsonl")
    received: list[list[dict]] = []

    async def cb(records):
        received.append(records)

    h.on_append(cb)
    await h.add_records([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])

    import asyncio
    await asyncio.sleep(0.05)

    assert len(received) == 1
    assert len(received[0]) == 2


@pytest.mark.asyncio
async def test_history_on_append_multiple_subscribers(tmp_path):
    """允许多个订阅者。"""
    from memory import HistoryManager

    h = HistoryManager(tmp_path / "h.jsonl")
    counts = [0, 0]

    async def cb1(records):
        counts[0] += 1

    async def cb2(records):
        counts[1] += 1

    h.on_append(cb1)
    h.on_append(cb2)
    await h.add_user_message("x")

    import asyncio
    await asyncio.sleep(0.05)

    assert counts == [1, 1]


def test_important_memory_keyword_force_save_api_removed():
    import inspect

    import memory.important as important_module
    from memory import ImportantMemoryManager

    assert not hasattr(ImportantMemoryManager, "force_save_from_keyword")
    assert not hasattr(important_module, "DEFAULT_FORCE_SAVE_KEYWORDS")
    assert not hasattr(important_module, "_strip_memory_keyword")
    assert "matched_keyword" not in inspect.getsource(important_module)


@pytest.mark.asyncio
async def test_important_save_keeps_keyword_text_as_explicit_tool_content(tmp_path):
    from memory import ImportantMemoryManager

    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()

    result = await im.save("记住第一条")

    assert result["saved"] is True
    assert im.items()[0]["content"] == "记住第一条"
    assert im.items()[0].get("source") is None
