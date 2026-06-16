"""Compaction, archive, budget, and RAG integration tests."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.message_pipeline import MessagePipeline
from memory import ArchiveStore, HistoryManager
from tests.integration_pipeline.helpers import (
    _ai_no_action,
    _approve_stub_tools,
    _drain_pipeline,
    _make_root_config,
    _msg,
)
from tools import ToolContext, build_default_registry


async def _add_history_until_active_tokens(
    pipeline: MessagePipeline,
    history: HistoryManager,
    *,
    min_tokens: int,
    prefix: str,
    conversation_id: str = "private:123",
) -> None:
    estimator = pipeline._token_estimator()
    for _ in range(80):
        active = await pipeline._select_working_history(conversation_id)
        if estimator.estimate_messages(active) >= min_tokens:
            return
        idx = await history.length()
        await history.add_user_message(
            f"{prefix} {idx} " + ("很长的预算测试内容 " * 260),
            conversation_id=conversation_id,
        )
    active = await pipeline._select_working_history(conversation_id)
    raise AssertionError(
        f"未能构造足够长的活跃历史: {estimator.estimate_messages(active)} < {min_tokens}"
    )

@pytest.mark.asyncio
async def test_history_records_alias_returns_records(tmp_path):
    """B13 单点回归：HistoryManager.records() 别名必须返回 list[dict]。"""
    hm = HistoryManager(tmp_path / "h.jsonl")
    await hm.load()
    await hm.add_user_message("hi")
    await hm.add_assistant_message("hello")

    records = await hm.records()
    assert isinstance(records, list)
    roles = [r.get("role") for r in records]
    assert "user" in roles
    assert "assistant" in roles


@pytest.mark.asyncio
async def test_inbound_keyword_text_does_not_auto_save_important_memory(build_pipeline):
    """普通入站关键词不再自动写入 important.json。"""
    pipeline, _, _, _, important = await build_pipeline([_ai_no_action()])

    await pipeline.enqueue(_msg(text="记住我喜欢吃寿司"))
    await _drain_pipeline(pipeline)

    items = important.items()
    assert not any("寿司" in (i.get("content") or "") for i in items)


@pytest.mark.asyncio
async def test_token_compaction_archives_without_truncating_full_history(build_pipeline):
    class FakeSummaryAgent:
        def __init__(self) -> None:
            self.existing_important_text = ""

        async def summarize_rolling(
            self,
            history_slice,
            existing_summary_text,
            existing_important_text,
        ):
            self.existing_important_text = existing_important_text
            return {
                "summary_text": f"{existing_summary_text}\n已归档 {len(history_slice)} 条".strip(),
                "new_important": [
                    {
                        "content": "归档中提到用户喜欢测试",
                        "scope": "user:123",
                        "pinned": True,
                    },
                    {"content": "归档中提到用户喜欢测试"},
                    {"content": "归档中提到旧格式也要保存"},
                    {
                        "content": "归档中提到非 bool pinned 不应置顶",
                        "scope": "group:5555",
                        "pinned": "true",
                    },
                    {"content": ""},
                    "invalid",
                ],
            }

    pipeline, _, _, history, important = await build_pipeline([])
    pipeline.summary_agent = FakeSummaryAgent()
    pipeline.behavior_cfg.summarize.trigger_at_tokens = 50
    pipeline.behavior_cfg.summarize.target_after_tokens = 20

    for idx in range(6):
        await history.add_user_message(
            f"旧消息 {idx} " + ("很长的测试内容 " * 20),
            conversation_id="private:123",
        )

    before = await history.length()
    result = await pipeline._maybe_summarize()

    archived = await pipeline.archive.records()
    after = await history.length()
    assert archived, "压缩前应先把原文写入 archive"
    assert result.success
    assert after == before + 1
    assert pipeline.rolling_summary.active_start_index() == len(archived)
    full_joined = "\n".join(str(r.get("content", "")) for r in await history.records())
    active_joined = "\n".join(
        str(r.get("content", ""))
        for r in await pipeline._select_working_history("private:123")
    )
    assert "旧消息 0" in full_joined
    assert "旧消息 0" not in active_joined
    assert "已归档" in pipeline.rolling_summary.text()
    items = important.items()
    scoped_item = next(
        item for item in items if item.get("content") == "归档中提到用户喜欢测试"
    )
    assert scoped_item.get("scope") == "user:123"
    assert scoped_item.get("pinned") is True
    assert sum(
        1 for item in items if item.get("content") == "归档中提到用户喜欢测试"
    ) == 1
    assert any(item.get("content") == "归档中提到旧格式也要保存" for item in items)
    non_bool_pinned_item = next(
        item for item in items if item.get("content") == "归档中提到非 bool pinned 不应置顶"
    )
    assert non_bool_pinned_item.get("scope") == "group:5555"
    assert non_bool_pinned_item.get("pinned") is False


@pytest.mark.asyncio
async def test_compaction_does_not_trigger_at_active_message_count(build_pipeline):
    class FakeSummaryAgent:
        def __init__(self) -> None:
            self.calls: list[list[dict[str, Any]]] = []

        async def summarize_rolling(
            self,
            history_slice,
            existing_summary_text,
            existing_important_text,
        ):
            self.calls.append(list(history_slice))
            return {"summary_text": "不应因记录数量摘要", "new_important": []}

    pipeline, _, _, history, _ = await build_pipeline([])
    agent = FakeSummaryAgent()
    pipeline.summary_agent = agent
    summarize = pipeline.behavior_cfg.summarize
    summarize.trigger_at_tokens = 999_999
    summarize.target_after_tokens = 1

    for idx in range(3):
        await history.add_user_message(
            f"短消息不触发摘要 {idx}",
            conversation_id="private:123",
        )

    result = await pipeline._maybe_summarize()

    assert result.status == "not_needed"
    assert result.reason == "below_trigger"
    assert agent.calls == []
    assert pipeline.rolling_summary.active_start_index() == 0
    assert await history.length() == 3


@pytest.mark.asyncio
async def test_compaction_slice_keeps_assistant_tool_result_group_together(build_pipeline):
    pipeline, _, _, _, _ = await build_pipeline([])
    estimator = pipeline._token_estimator()
    records = [
        {"role": "user", "content": "很早的普通消息 " + ("内容 " * 40)},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "tc-boundary",
                    "type": "function",
                    "function": {"name": "no_action", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tc-boundary", "content": '{"no_action": true}'},
        {"role": "user", "content": "必须留在活跃窗口里的新消息"},
    ]
    active_tokens = estimator.estimate_messages(records)
    target_after = active_tokens - estimator.estimate_messages(records[:2])

    selected = pipeline._select_compaction_slice(
        records,
        active_tokens=active_tokens,
        target_after_tokens=target_after,
        estimator=estimator,
    )

    assert selected == records[:3]
    assert selected[-1]["role"] == "tool"


@pytest.mark.asyncio
async def test_active_start_index_moves_back_from_orphan_tool_result(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    await history.add_user_message("旧消息", conversation_id="private:123")
    await history.add_records(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc-orphan",
                        "type": "function",
                        "function": {"name": "no_action", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tc-orphan", "content": '{"no_action": true}'},
            {"role": "user", "content": "后续消息"},
        ],
        conversation_id="private:123",
    )
    await pipeline.rolling_summary.update("旧摘要", active_start_index=2)

    active = await pipeline._select_working_history("private:123")

    assert active[0]["role"] == "assistant"
    assert active[0]["tool_calls"][0]["id"] == "tc-orphan"
    assert active[1]["role"] == "tool"
    assert active[1]["tool_call_id"] == "tc-orphan"


@pytest.mark.asyncio
async def test_concurrent_compaction_is_serialized_without_duplicate_archive(
    build_pipeline,
):
    class BlockingSummaryAgent:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.first_started = asyncio.Event()
            self.first_release = asyncio.Event()
            self.second_started = asyncio.Event()

        async def summarize_rolling(
            self,
            history_slice,
            existing_summary_text,
            existing_important_text,
        ):
            self.calls.append([str(record.get("content") or "") for record in history_slice])
            if len(self.calls) == 1:
                self.first_started.set()
                await self.first_release.wait()
            else:
                self.second_started.set()
            return {
                "summary_text": f"{existing_summary_text}\n并发摘要 {len(self.calls)}".strip(),
                "new_important": [],
            }

    pipeline, _, _, history, _ = await build_pipeline([])
    agent = BlockingSummaryAgent()
    pipeline.summary_agent = agent
    for idx in range(8):
        await history.add_user_message(
            f"并发压缩消息 {idx} " + ("内容 " * 100),
            conversation_id="private:123",
        )
    active = await pipeline._select_working_history("private:123")
    estimator = pipeline._token_estimator()
    target_after = max(
        1,
        estimator.estimate_messages(active)
        - estimator.estimate_messages(active[:2]),
    )

    first_task = asyncio.create_task(
        pipeline._maybe_summarize(
            force=True,
            target_after_tokens=target_after,
            reason="concurrent_first",
        )
    )
    await asyncio.wait_for(agent.first_started.wait(), timeout=1.0)
    second_task = asyncio.create_task(
        pipeline._maybe_summarize(
            force=True,
            target_after_tokens=target_after,
            reason="concurrent_second",
        )
    )
    await asyncio.sleep(0.05)

    assert len(agent.calls) == 1
    assert not agent.second_started.is_set()

    agent.first_release.set()
    first_result, second_result = await asyncio.gather(first_task, second_task)

    assert first_result.success
    assert second_result.success
    assert second_result.active_start_before >= first_result.active_start_after
    assert len(agent.calls) == 2
    assert set(agent.calls[0]).isdisjoint(agent.calls[1])
    archived = await pipeline.archive.records()
    archived_contents = [
        str(record.get("content") or "")
        for record in archived
        if "并发压缩消息" in str(record.get("content") or "")
    ]
    assert len(archived_contents) == len(set(archived_contents))
    assert pipeline.rolling_summary.active_start_index() == second_result.active_start_after


@pytest.mark.asyncio
async def test_compaction_partial_archive_retry_does_not_append_duplicate(
    build_pipeline,
    monkeypatch,
):
    class FakeSummaryAgent:
        async def summarize_rolling(
            self,
            history_slice,
            existing_summary_text,
            existing_important_text,
        ):
            return {"summary_text": f"partial retry {len(history_slice)}", "new_important": []}

    pipeline, _, _, history, _ = await build_pipeline([])
    pipeline.summary_agent = FakeSummaryAgent()
    for idx in range(5):
        await history.add_user_message(
            f"partial archive 旧消息 {idx} " + ("内容 " * 120),
            conversation_id="private:123",
        )

    original_update = pipeline.rolling_summary.update

    async def fail_update(*args, **kwargs):
        raise RuntimeError("rolling summary write failed")

    monkeypatch.setattr(pipeline.rolling_summary, "update", fail_update)
    first = await pipeline._maybe_summarize(
        force=True,
        target_after_tokens=5,
        reason="partial_archive_first",
    )

    assert not first.success
    assert first.reason == "commit_error"
    assert first.partial_archive_committed is True
    assert pipeline.rolling_summary.active_start_index() == 0
    archived_after_failure = await pipeline.archive.records()
    assert archived_after_failure

    pipeline._summary_partial_archives = {}
    pipeline.archive = ArchiveStore(pipeline.archive.path)
    await pipeline.archive.load()
    monkeypatch.setattr(pipeline.rolling_summary, "update", original_update)
    second = await pipeline._maybe_summarize(
        force=True,
        target_after_tokens=5,
        reason="partial_archive_retry",
    )
    archived_after_retry = await pipeline.archive.records()

    assert second.success
    assert second.archive_reused is False
    assert len(archived_after_retry) == len(archived_after_failure)
    assert pipeline.rolling_summary.active_start_index() == second.active_start_after


@pytest.mark.asyncio
async def test_main_reply_budget_overflow_compacts_before_calling_model(build_pipeline):
    class FakeSummaryAgent:
        def __init__(self) -> None:
            self.calls: list[list[dict[str, Any]]] = []

        async def summarize_rolling(
            self,
            history_slice,
            existing_summary_text,
            existing_important_text,
        ):
            self.calls.append(list(history_slice))
            return {"summary_text": "预算预检摘要完成", "new_important": []}

    pipeline, provider, _, history, _ = await build_pipeline([_ai_no_action()])
    agent = FakeSummaryAgent()
    pipeline.summary_agent = agent
    pipeline.behavior_cfg.context.max_context_tokens = 31_000
    pipeline.behavior_cfg.context.reserve_output_tokens = 1_000
    summarize = pipeline.behavior_cfg.summarize
    summarize.trigger_at_tokens = 999_999
    summarize.target_after_tokens = 3_000
    await _add_history_until_active_tokens(
        pipeline,
        history,
        min_tokens=24_000,
        prefix="预算压缩旧消息",
    )

    await pipeline.enqueue(_msg(text="触发预算压缩", message_id="budget-compact"))
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert agent.calls
    assert provider.calls
    assert pipeline.rolling_summary.active_start_index() > 0
    joined = "\n".join(str(m.get("content", "")) for m in provider.calls[0]["messages"])
    assert "预算预检摘要完成" in joined
    assert "触发预算压缩" in joined
    assert "预算压缩旧消息 0" not in joined


@pytest.mark.asyncio
async def test_main_reply_budget_retry_expands_compaction_range(build_pipeline):
    class FakeSummaryAgent:
        def __init__(self) -> None:
            self.calls: list[list[dict[str, Any]]] = []

        async def summarize_rolling(
            self,
            history_slice,
            existing_summary_text,
            existing_important_text,
        ):
            self.calls.append(list(history_slice))
            return {
                "summary_text": f"预算重试摘要完成 {len(self.calls)}",
                "new_important": [],
            }

    pipeline, provider, _, history, _ = await build_pipeline([_ai_no_action()])
    agent = FakeSummaryAgent()
    pipeline.summary_agent = agent
    pipeline.behavior_cfg.context.max_context_tokens = 31_000
    pipeline.behavior_cfg.context.reserve_output_tokens = 1_000
    summarize = pipeline.behavior_cfg.summarize
    summarize.trigger_at_tokens = 999_999
    summarize.target_after_tokens = 15_000
    summarize.retry_target_after_context_percent = 20
    await _add_history_until_active_tokens(
        pipeline,
        history,
        min_tokens=26_000,
        prefix="预算重试旧消息",
    )

    await pipeline.enqueue(_msg(text="触发预算重试", message_id="budget-retry"))
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert len(agent.calls) == 2
    assert provider.calls
    assert pipeline.rolling_summary.active_start_index() > len(agent.calls[0])
    joined = "\n".join(str(m.get("content", "")) for m in provider.calls[0]["messages"])
    assert "预算重试摘要完成 2" in joined
    assert "触发预算重试" in joined


@pytest.mark.asyncio
async def test_budget_retry_target_never_exceeds_first_target(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    pipeline.behavior_cfg.context.max_context_tokens = 100_000
    summarize = pipeline.behavior_cfg.summarize
    summarize.target_after_tokens = 2_000
    summarize.retry_target_after_context_percent = 50
    await _add_history_until_active_tokens(
        pipeline,
        history,
        min_tokens=8_000,
        prefix="重试目标保护旧消息",
    )

    estimator = pipeline._token_estimator()
    first_target = await pipeline._first_budget_retry_target(estimator)
    retry_target = await pipeline._retry_budget_target(
        estimator,
        first_target=first_target,
    )

    assert first_target == 2_000
    assert retry_target <= first_target


@pytest.mark.asyncio
async def test_run_one_turn_budget_failure_skips_model_and_writes_system_note(
    build_pipeline,
):
    class FailingSummaryAgent:
        def __init__(self) -> None:
            self.calls = 0

        async def summarize_rolling(
            self,
            history_slice,
            existing_summary_text,
            existing_important_text,
        ):
            self.calls += 1
            return None

    pipeline, provider, _, history, _ = await build_pipeline([_ai_no_action()])
    agent = FailingSummaryAgent()
    pipeline.summary_agent = agent
    pipeline.behavior_cfg.context.max_context_tokens = 31_000
    pipeline.behavior_cfg.context.reserve_output_tokens = 1_000
    summarize = pipeline.behavior_cfg.summarize
    summarize.trigger_at_tokens = 999_999
    summarize.target_after_tokens = 3_000
    await _add_history_until_active_tokens(
        pipeline,
        history,
        min_tokens=24_000,
        prefix="预算失败旧消息",
    )

    await pipeline.run_one_turn(
        "预算失败测试",
        user_event="这轮不应调用主模型",
        conversation_id="private:123",
    )

    assert agent.calls == 2
    assert provider.calls == []
    records = await history.records()
    assert any(
        record.get("role") == "system"
        and "主模型输入预检失败" in str(record.get("content") or "")
        for record in records
    )


@pytest.mark.asyncio
async def test_rag_mode_compaction_still_reads_and_writes_important_memory(build_pipeline):
    class FakeSummaryAgent:
        def __init__(self) -> None:
            self.existing_important_text = ""

        async def summarize_rolling(
            self,
            history_slice,
            existing_summary_text,
            existing_important_text,
        ):
            self.existing_important_text = existing_important_text
            return {
                "summary_text": f"RAG 模式归档 {len(history_slice)} 条",
                "new_important": [{"content": "RAG 模式归档中提到用户喜欢测试"}],
            }

    pipeline, _, _, history, important = await build_pipeline([])
    pipeline.features_cfg.long_term_memory.mode = "rag"
    await important.save("已有重要记忆仍应参与摘要")
    agent = FakeSummaryAgent()
    pipeline.summary_agent = agent
    pipeline.behavior_cfg.summarize.trigger_at_tokens = 50
    pipeline.behavior_cfg.summarize.target_after_tokens = 20

    for idx in range(6):
        await history.add_user_message(
            f"RAG 模式旧消息 {idx} " + ("很长的测试内容 " * 20),
            conversation_id="private:123",
        )

    await pipeline._maybe_summarize()

    assert "已有重要记忆仍应参与摘要" in agent.existing_important_text
    assert any(
        "RAG 模式归档中提到用户喜欢测试" in item.get("content", "")
        for item in important.items()
    )


@pytest.mark.asyncio
async def test_compaction_uses_percent_thresholds_when_token_fields_are_unset(
    build_pipeline,
):
    class FakeSummaryAgent:
        def __init__(self) -> None:
            self.history_slice: list[dict[str, Any]] = []

        async def summarize_rolling(
            self,
            history_slice,
            existing_summary_text,
            existing_important_text,
        ):
            self.history_slice = list(history_slice)
            return {"summary_text": "百分比摘要完成", "new_important": []}

    pipeline, _, _, history, _ = await build_pipeline([])
    agent = FakeSummaryAgent()
    pipeline.summary_agent = agent
    pipeline.behavior_cfg.context.max_context_tokens = 1_000
    summarize = pipeline.behavior_cfg.summarize
    summarize.trigger_at_tokens = None
    summarize.target_after_tokens = None
    summarize.trigger_at_context_percent = 50
    summarize.target_after_context_percent = 80

    estimator = pipeline._token_estimator()
    while estimator.estimate_messages(await history.records()) < 950:
        idx = await history.length()
        await history.add_user_message(
            f"百分比旧消息 {idx} " + ("很长的测试内容 " * 20),
            conversation_id="private:123",
        )

    records = await history.records()
    active_tokens = estimator.estimate_messages(records)
    expected_target = int(
        pipeline._context_budget().max_context_tokens
        * summarize.target_after_context_percent
        / 100
    )
    expected_slice = pipeline._select_compaction_slice(
        records,
        active_tokens=active_tokens,
        target_after_tokens=max(1, min(expected_target, active_tokens - 1)),
        estimator=estimator,
    )

    await pipeline._maybe_summarize()

    assert agent.history_slice
    assert len(agent.history_slice) == len(expected_slice)
    assert "百分比摘要完成" in pipeline.rolling_summary.text()


@pytest.mark.asyncio
async def test_triggered_compaction_runs_before_model_call(build_pipeline):
    class BlockingSummaryAgent:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.provider = None
            self.cfg = type("Cfg", (), {"model": "fake-summary"})()

        async def summarize_rolling(
            self,
            history_slice,
            existing_summary_text,
            existing_important_text,
        ):
            self.started.set()
            await self.release.wait()
            return {"summary_text": "后台摘要完成", "new_important": []}

    pipeline, _, _, history, _ = await build_pipeline([_ai_no_action()])
    agent = BlockingSummaryAgent()
    pipeline.summary_agent = agent
    pipeline.behavior_cfg.summarize.trigger_at_tokens = 10
    pipeline.behavior_cfg.summarize.target_after_tokens = 5

    for idx in range(3):
        await history.add_user_message(
            f"旧消息 {idx} " + ("很长的测试内容 " * 10),
            conversation_id="private:123",
        )

    await pipeline.enqueue(_msg(text="触发后台压缩"))
    await asyncio.wait_for(agent.started.wait(), timeout=1.0)

    assert pipeline._batch_task is not None and not pipeline._batch_task.done()

    agent.release.set()
    await _drain_pipeline(pipeline, max_wait=2.0)
    assert "后台摘要完成" in pipeline.rolling_summary.text()


@pytest.mark.asyncio
async def test_recall_history_reads_archive(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    await pipeline.archive.append_many(
        [
            {
                "role": "user",
                "content": "很久以前约定：周日做游戏 Demo",
                "conversation_id": "group:42",
                "metadata": {"timestamp": "2026-05-30 01:00"},
            }
        ]
    )
    await history.add_user_message(
        "当前活跃区约定：周一补玩法文档",
        metadata={"timestamp": "2026-05-30 02:00"},
        conversation_id="group:42",
    )
    registry = build_default_registry(_make_root_config())
    ctx = ToolContext(archive=pipeline.archive, history=history)
    _approve_stub_tools(ctx, "recall_history")
    executor = registry.get_executor(ctx)

    result = await executor(
        "recall_history",
        {"conversation_id": "group:42", "time_range": "2026-05-30", "limit": 5},
    )

    assert result["ok"] is True
    assert result["status"] == "inline"
    assert result["count"] == 2
    assert "周日做游戏 Demo" in result["content"]
    assert "周一补玩法文档" in result["content"]
    assert "metadata" not in result["results"][0]


@pytest.mark.asyncio
async def test_pipeline_injects_scope_filtered_important_memory(build_pipeline):
    pipeline, provider, _, _, important = await build_pipeline([_ai_no_action()])
    await important.save("全局偏好", scope="global")
    await important.save("群 42 约定", scope="group:42")
    await important.save("群 99 约定", scope="group:99")
    await important.save("置顶跨会话事实", scope="user:123", pinned=True)

    await pipeline.enqueue(_msg(text="触发上下文", group_id="42"))
    await _drain_pipeline(pipeline)

    joined = "\n".join(
        str(message.get("content", "")) for message in provider.calls[0]["messages"]
    )
    assert "全局偏好" in joined
    assert "群 42 约定" in joined
    assert "置顶跨会话事实" in joined
    assert "群 99 约定" not in joined


@pytest.mark.asyncio
async def test_rag_mode_still_injects_scope_filtered_important_memory(build_pipeline):
    pipeline, provider, _, _, important = await build_pipeline([_ai_no_action()])
    pipeline.features_cfg.long_term_memory.mode = "rag"
    await important.save("RAG 模式全局偏好", scope="global")
    await important.save("RAG 模式群 42 约定", scope="group:42")
    await important.save("RAG 模式群 99 约定", scope="group:99")

    await pipeline.enqueue(_msg(text="触发 RAG 模式上下文", group_id="42"))
    await _drain_pipeline(pipeline)

    joined = "\n".join(
        str(message.get("content", "")) for message in provider.calls[0]["messages"]
    )
    assert "RAG 模式全局偏好" in joined
    assert "RAG 模式群 42 约定" in joined
    assert "RAG 模式群 99 约定" not in joined
    assert "<long_term_memory" in joined


@pytest.mark.asyncio
async def test_rag_mode_inbound_keyword_text_does_not_auto_save_important_memory(build_pipeline):
    pipeline, _, _, _, important = await build_pipeline([_ai_no_action()])
    pipeline.features_cfg.long_term_memory.mode = "rag"

    await pipeline.enqueue(_msg(text="记一下：我报名了某项长期活动，7月7日有选拔环节"))
    await _drain_pipeline(pipeline)

    assert not any("长期活动" in item.get("content", "") for item in important.items())

