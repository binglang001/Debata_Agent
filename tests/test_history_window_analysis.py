from __future__ import annotations

from core.history_window_analysis import (
    analyze_history_window,
    format_history_window_analysis,
    load_history_records,
)


def test_history_window_analysis_compares_baseline_and_current_selection():
    records = [
        {"role": "user", "content": "真实旧聊天 KEEP", "conversation_id": "group:1"},
    ]
    for idx in range(40):
        records.append(
            {
                "role": "user",
                "content": (
                    "<task_context priority=\"medium\">\n"
                    f"旧运行时噪声 {idx} " + ("填充 " * 80) + "\n"
                    "</task_context>"
                ),
                "metadata": {"kind": "task_context_snapshot"},
                "conversation_id": "group:noise",
            }
        )
    records.append({"role": "user", "content": "当前消息", "conversation_id": "private:1"})

    analysis = analyze_history_window(
        records,
        working_budget=4_000,
        conversation_id="private:1",
        ensure_current_records=1,
    )

    baseline_keys = {bucket.key for bucket in analysis.baseline.buckets}
    current_keys = {bucket.key for bucket in analysis.current.buckets}
    current_real_chat = _bucket_count(analysis.current.buckets, "real_or_legacy_user")
    baseline_real_chat = _bucket_count(analysis.baseline.buckets, "real_or_legacy_user")

    assert "runtime_context" in baseline_keys
    assert "real_or_legacy_user" in current_keys
    assert current_real_chat > baseline_real_chat
    assert analysis.current.estimated_tokens <= analysis.baseline.estimated_tokens


def test_history_window_analysis_formats_report():
    analysis = analyze_history_window(
        [
            {"role": "user", "content": "消息", "conversation_id": "private:1"},
            {"role": "tool", "tool_call_id": "tc", "content": '{"ok": true}'},
        ],
        working_budget=8_000,
        conversation_id="private:1",
    )

    report = format_history_window_analysis(analysis)

    assert "工作历史窗口诊断" in report
    assert "旧选择" in report
    assert "当前选择" in report
    assert "变化" in report


def test_load_history_records_skips_invalid_jsonl(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_text(
        '{"role":"user","content":"ok"}\n'
        'not json\n'
        '[]\n',
        encoding="utf-8",
    )

    records, invalid = load_history_records(path)

    assert records == [{"role": "user", "content": "ok"}]
    assert invalid == 2


def _bucket_count(buckets, key: str) -> int:
    for bucket in buckets:
        if bucket.key == key:
            return bucket.record_count
    return 0
