"""Offline diagnostics for working-history window selection.

This module compares the old "latest records until budget is full" behavior
with the current runtime-noise-aware selector.  It is intentionally local and
deterministic: no model calls, no network, and no writes.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import orjson

from utils.token_budget import TokenEstimator

RECENT_RUNTIME_KEEP = 12
SEND_RECEIPT_KEEP = 4
NO_ACTION_KEEP = 8


@dataclass(slots=True)
class HistoryWindowBucket:
    key: str
    record_count: int = 0
    estimated_tokens: int = 0


@dataclass(slots=True)
class HistoryWindowStats:
    label: str
    selected_count: int
    estimated_tokens: int
    buckets: list[HistoryWindowBucket] = field(default_factory=list)


@dataclass(slots=True)
class HistoryWindowAnalysis:
    history_count: int
    working_budget: int
    conversation_id: str | None
    baseline: HistoryWindowStats
    current: HistoryWindowStats

    @property
    def token_delta(self) -> int:
        return self.current.estimated_tokens - self.baseline.estimated_tokens

    @property
    def selected_delta(self) -> int:
        return self.current.selected_count - self.baseline.selected_count


def load_history_records(path: str | Path) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    invalid = 0
    p = Path(path)
    if not p.exists():
        return records, invalid
    for raw in p.read_bytes().splitlines():
        if not raw.strip():
            continue
        try:
            item = orjson.loads(raw)
        except orjson.JSONDecodeError:
            invalid += 1
            continue
        if isinstance(item, dict):
            records.append(item)
        else:
            invalid += 1
    return records, invalid


def analyze_history_window(
    records: list[dict[str, Any]],
    *,
    working_budget: int,
    conversation_id: str | None = None,
    ensure_current_records: int = 8,
    model: str = "",
) -> HistoryWindowAnalysis:
    estimator = TokenEstimator(model)
    baseline = _select_baseline(
        records,
        working_budget=working_budget,
        conversation_id=conversation_id,
        ensure_current_records=ensure_current_records,
        estimator=estimator,
    )
    current = _select_current(
        records,
        working_budget=working_budget,
        conversation_id=conversation_id,
        ensure_current_records=ensure_current_records,
        estimator=estimator,
    )
    return HistoryWindowAnalysis(
        history_count=len(records),
        working_budget=working_budget,
        conversation_id=conversation_id,
        baseline=_stats_for("baseline", baseline, estimator),
        current=_stats_for("current", current, estimator),
    )


def format_history_window_analysis(analysis: HistoryWindowAnalysis) -> str:
    lines = [
        "工作历史窗口诊断",
        "",
        f"历史记录: {analysis.history_count:,} 条",
        f"工作预算: {analysis.working_budget:,} tokens",
        f"会话: {analysis.conversation_id or '(统一窗口)'}",
        "",
        _format_stats("旧选择", analysis.baseline),
        "",
        _format_stats("当前选择", analysis.current),
        "",
        (
            "变化: "
            f"记录 {analysis.selected_delta:+,} 条，"
            f"估算 token {analysis.token_delta:+,}。"
        ),
    ]
    return "\n".join(lines)


def _select_baseline(
    records: list[dict[str, Any]],
    *,
    working_budget: int,
    conversation_id: str | None,
    ensure_current_records: int,
    estimator: TokenEstimator,
) -> list[dict[str, Any]]:
    selected_indices: set[int] = set()
    used = 0

    def add_index(index: int, *, force: bool = False) -> bool:
        nonlocal used
        if index in selected_indices:
            return True
        cost = estimator.estimate_messages([records[index]])
        if not force and selected_indices and used + cost > working_budget:
            return False
        selected_indices.add(index)
        used += cost
        return True

    if conversation_id and ensure_current_records > 0:
        current = _force_keep_indices(
            records,
            conversation_id=conversation_id,
            ensure_current_records=ensure_current_records,
        )
        for idx in sorted(current):
            add_index(idx, force=True)

    for idx in range(len(records) - 1, -1, -1):
        if idx in selected_indices:
            continue
        if not add_index(idx):
            break
    return [records[idx] for idx in sorted(selected_indices)]


def _select_current(
    records: list[dict[str, Any]],
    *,
    working_budget: int,
    conversation_id: str | None,
    ensure_current_records: int,
    estimator: TokenEstimator,
) -> list[dict[str, Any]]:
    selected_indices: set[int] = set()
    noise = _noise_indices(
        records,
        conversation_id=conversation_id,
        ensure_current_records=ensure_current_records,
    )
    optional = _optional_runtime_indices(
        records,
        conversation_id=conversation_id,
        ensure_current_records=ensure_current_records,
    )
    used = 0

    def add_index(index: int, *, force: bool = False) -> bool:
        nonlocal used
        if index in selected_indices:
            return True
        if not force and index in noise:
            return True
        cost = estimator.estimate_messages([records[index]])
        if not force and selected_indices and used + cost > working_budget:
            return False
        selected_indices.add(index)
        used += cost
        return True

    if conversation_id and ensure_current_records > 0:
        for idx in sorted(
            _force_keep_indices(
                records,
                conversation_id=conversation_id,
                ensure_current_records=ensure_current_records,
            )
        ):
            add_index(idx, force=True)

    for idx in range(len(records) - 1, -1, -1):
        if idx in selected_indices or idx in optional:
            continue
        if not add_index(idx):
            break

    for idx in range(len(records) - 1, -1, -1):
        if idx in selected_indices or idx not in optional:
            continue
        if not add_index(idx):
            break

    selected = [records[idx] for idx in sorted(selected_indices)]
    return _filter_noise_view(
        selected,
        conversation_id=conversation_id,
        ensure_current_records=ensure_current_records,
    )


def _stats_for(
    label: str,
    records: list[dict[str, Any]],
    estimator: TokenEstimator,
) -> HistoryWindowStats:
    bucket_map: dict[str, HistoryWindowBucket] = {}
    total = 0
    for record in records:
        cost = estimator.estimate_messages([record])
        total += cost
        key = _record_category(record)
        bucket = bucket_map.setdefault(key, HistoryWindowBucket(key))
        bucket.record_count += 1
        bucket.estimated_tokens += cost
    return HistoryWindowStats(
        label=label,
        selected_count=len(records),
        estimated_tokens=total,
        buckets=sorted(
            bucket_map.values(),
            key=lambda item: (item.estimated_tokens, item.record_count),
            reverse=True,
        ),
    )


def _format_stats(title: str, stats: HistoryWindowStats) -> str:
    lines = [
        f"{title}: {stats.selected_count:,} 条，估算 {stats.estimated_tokens:,} tokens",
        "  类型 | 记录 | 估算 token",
    ]
    for bucket in stats.buckets:
        lines.append(
            f"  {bucket.key} | {bucket.record_count:,} | {bucket.estimated_tokens:,}"
        )
    return "\n".join(lines)


def _record_category(record: dict[str, Any]) -> str:
    role = str(record.get("role") or "")
    content = str(record.get("content") or "")
    if _runtime_kind(record) == "send_receipt":
        return "send_receipt"
    if _runtime_kind(record) is not None:
        return "runtime_context"
    if role == "assistant" and _assistant_tool_call_names(record):
        names = set(_assistant_tool_call_names(record).values())
        if names == {"no_action"}:
            return "no_action_call"
        return "assistant_tool_call"
    if role == "tool":
        try:
            payload = json.loads(content or "{}")
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict) and payload.get("no_action"):
            return "no_action_result"
        return "tool_result"
    if role == "system":
        return "system_event"
    if role == "user":
        return "real_or_legacy_user"
    if role == "assistant":
        return "assistant_text"
    return role or "unknown"


def _runtime_kind(record: dict[str, Any]) -> str | None:
    if record.get("role") != "user":
        return None
    meta = record.get("metadata")
    if isinstance(meta, dict):
        kind = str(meta.get("kind") or "")
        if kind in {"task_context_snapshot", "send_done_snapshot"}:
            return kind
    content = str(record.get("content") or "")
    if "<task_context" in content:
        return "task_context_snapshot"
    if "<send_status" in content:
        return "send_done_snapshot"
    if "<send_receipt" in content:
        return "send_receipt"
    return None


def _assistant_tool_call_names(record: dict[str, Any]) -> dict[str, str]:
    if record.get("role") != "assistant":
        return {}
    result: dict[str, str] = {}
    for tool_call in record.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        call_id = str(tool_call.get("id") or "")
        function = tool_call.get("function")
        name = ""
        if isinstance(function, dict):
            name = str(function.get("name") or "")
        if call_id and name:
            result[call_id] = name
    return result


def _tool_result_is_no_action(record: dict[str, Any], tool_call_id: str) -> bool:
    if record.get("role") != "tool":
        return False
    if str(record.get("tool_call_id") or "") != tool_call_id:
        return False
    try:
        payload = json.loads(str(record.get("content") or "{}"))
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and bool(payload.get("no_action"))


def _no_action_pair_indices(records: list[dict[str, Any]], start: int) -> set[int]:
    if start >= len(records):
        return set()
    assistant_record = records[start]
    if str(assistant_record.get("content") or "").strip():
        return set()
    tool_names = _assistant_tool_call_names(assistant_record)
    if not tool_names or any(name != "no_action" for name in tool_names.values()):
        return set()
    ids = list(tool_names)
    end = start + len(ids)
    if end >= len(records):
        return set()
    for offset, tool_call_id in enumerate(ids, start=1):
        if not _tool_result_is_no_action(records[start + offset], tool_call_id):
            return set()
    return set(range(start, end + 1))


def _record_conversation_id(record: dict[str, Any]) -> str | None:
    if record.get("conversation_id"):
        return str(record.get("conversation_id"))
    meta = record.get("metadata")
    if not isinstance(meta, dict):
        return None
    messages = meta.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, dict):
            scope = last.get("scope")
            target_id = last.get("target_id")
            group_id = last.get("group_id")
            user_id = last.get("user_id")
            if scope == "group" and (group_id or target_id):
                return f"group:{group_id or target_id}"
            if scope == "private" and (target_id or user_id):
                return f"private:{target_id or user_id}"
            if group_id:
                return f"group:{group_id}"
            if user_id:
                return f"private:{user_id}"
    return None


def _force_keep_indices(
    records: list[dict[str, Any]],
    *,
    conversation_id: str | None,
    ensure_current_records: int,
) -> set[int]:
    result: set[int] = set()
    if not conversation_id or ensure_current_records <= 0:
        return result
    for idx in range(len(records) - 1, -1, -1):
        if _record_conversation_id(records[idx]) != conversation_id:
            continue
        result.add(idx)
        if len(result) >= ensure_current_records:
            break
    return result


def _noise_indices(
    records: list[dict[str, Any]],
    *,
    conversation_id: str | None,
    ensure_current_records: int,
) -> set[int]:
    force_keep = _force_keep_indices(
        records,
        conversation_id=conversation_id,
        ensure_current_records=ensure_current_records,
    )
    runtime_indices = [
        idx for idx, record in enumerate(records)
        if _runtime_kind(record) is not None
    ]
    keep_runtime = set(runtime_indices[-RECENT_RUNTIME_KEEP:])
    send_receipt_indices = [
        idx for idx in runtime_indices
        if _runtime_kind(records[idx]) == "send_receipt"
    ]
    keep_runtime.update(send_receipt_indices[-SEND_RECEIPT_KEEP:])
    drop = {
        idx for idx in runtime_indices
        if idx not in force_keep and idx not in keep_runtime
    }

    pairs: list[set[int]] = []
    idx = 0
    while idx < len(records):
        pair = _no_action_pair_indices(records, idx)
        if not pair:
            idx += 1
            continue
        pairs.append(pair)
        idx = max(pair) + 1
    kept_no_action: set[int] = set()
    for pair in pairs[-NO_ACTION_KEEP:]:
        kept_no_action.update(pair)
    for pair in pairs:
        if pair & force_keep or pair & kept_no_action:
            continue
        drop.update(pair)
    return drop


def _optional_runtime_indices(
    records: list[dict[str, Any]],
    *,
    conversation_id: str | None,
    ensure_current_records: int,
) -> set[int]:
    drop = _noise_indices(
        records,
        conversation_id=conversation_id,
        ensure_current_records=ensure_current_records,
    )
    force = _force_keep_indices(
        records,
        conversation_id=conversation_id,
        ensure_current_records=ensure_current_records,
    )
    optional = {
        idx for idx, record in enumerate(records)
        if _runtime_kind(record) is not None
    }
    idx = 0
    while idx < len(records):
        pair = _no_action_pair_indices(records, idx)
        if not pair:
            idx += 1
            continue
        optional.update(pair)
        idx = max(pair) + 1
    return optional - drop - force


def _filter_noise_view(
    records: list[dict[str, Any]],
    *,
    conversation_id: str | None,
    ensure_current_records: int,
) -> list[dict[str, Any]]:
    drop = _noise_indices(
        records,
        conversation_id=conversation_id,
        ensure_current_records=ensure_current_records,
    )
    if not drop:
        return records
    return [record for idx, record in enumerate(records) if idx not in drop]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze Debata history window selection")
    parser.add_argument("path", help="Path to history.jsonl")
    parser.add_argument("--budget", type=int, default=96_000, help="Working history token budget")
    parser.add_argument("--conversation-id", default=None, help="Current conversation id")
    parser.add_argument("--ensure-current-records", type=int, default=8)
    parser.add_argument("--model", default="")
    args = parser.parse_args(argv)

    records, invalid = load_history_records(args.path)
    analysis = analyze_history_window(
        records,
        working_budget=max(1, args.budget),
        conversation_id=args.conversation_id,
        ensure_current_records=max(0, args.ensure_current_records),
        model=args.model,
    )
    print(format_history_window_analysis(analysis))
    if invalid:
        print(f"\n跳过无法解析的 JSONL 行: {invalid:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
