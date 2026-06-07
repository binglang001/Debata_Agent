from __future__ import annotations

from core.usage_analysis import (
    analyze_usage_records,
    format_usage_analysis,
    load_usage_records,
    percentile,
)


def test_usage_analysis_groups_runtime_flags_and_top_records():
    records = [
        {
            "ts": 1780804569,
            "provider": "deepseek_main",
            "model": "deepseek-v4-flash",
            "agent": "主模型",
            "operation": "agent_loop",
            "prompt_tokens": 1000,
            "completion_tokens": 20,
            "cached_tokens": 800,
            "cache_creation_tokens": 200,
            "total_tokens": 1020,
            "kv_message_count": 10,
            "kv_tools_count": 25,
            "kv_tools_hash": "abcdef123456",
            "kv_has_send_receipt": True,
            "kv_has_recent_group_messages": True,
            "kv_has_rag": False,
        },
        {
            "ts": 1780805570,
            "provider": "deepseek_main",
            "model": "deepseek-v4-pro",
            "agent": "主动思考",
            "operation": "proactive_route",
            "prompt_tokens": 300,
            "completion_tokens": 5,
            "cached_tokens": 0,
            "cache_creation_tokens": 300,
            "total_tokens": 305,
        },
        {
            "ts": 1780805585,
            "provider": "deepseek_main",
            "model": "deepseek-v4-flash",
            "agent": "主模型",
            "operation": "agent_loop",
            "prompt_tokens": 2000,
            "completion_tokens": 30,
            "cached_tokens": 1500,
            "cache_creation_tokens": 500,
            "total_tokens": 2030,
            "kv_message_count": 80,
            "kv_tools_count": 25,
            "kv_tools_hash": "abcdef123456",
            "kv_has_send_receipt": True,
            "kv_has_recent_group_messages": True,
            "kv_has_rag": True,
        },
    ]

    analysis = analyze_usage_records(records, top_n=2)

    assert analysis.total.request_count == 3
    assert analysis.total.prompt_tokens == 3300
    assert analysis.total.cache_hit_rate == 2300 / 3300
    assert analysis.by_agent[0].key == "主模型"
    assert analysis.by_operation[0].key == "agent_loop"
    assert analysis.by_kv_flags[0].key == "send_receipt+recent_group+rag"
    assert analysis.by_tool_schema[0].key == "25 tools / abcdef12"
    assert analysis.by_message_band[0].key == "065-128"
    assert analysis.top_prompt_records[0]["prompt_tokens"] == 2000

    report = format_usage_analysis(analysis, top_n=2)
    assert "模型用量分析" in report
    assert "按 KV/Runtime 标记" in report
    assert "最大输入调用 Top 2" in report


def test_load_usage_records_skips_invalid_jsonl(tmp_path):
    path = tmp_path / "usage.jsonl"
    path.write_text(
        '{"prompt_tokens":1,"total_tokens":1}\n'
        'not json\n'
        '[1,2]\n',
        encoding="utf-8",
    )

    records, invalid = load_usage_records(path)

    assert records == [{"prompt_tokens": 1, "total_tokens": 1}]
    assert invalid == 2


def test_percentile_handles_empty_and_singleton():
    assert percentile([], 0.95) == 0
    assert percentile([7], 0.95) == 7
    assert percentile([1, 2, 3, 4, 5], 0.50) == 3
