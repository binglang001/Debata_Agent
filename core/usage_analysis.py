"""Offline model usage analysis utilities.

The runtime recorder intentionally stores simple JSONL records.  This module
turns those records into a compact breakdown that is useful for diagnosing
prompt growth, cache behavior, and noisy runtime context.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import orjson

TOKEN_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "cache_creation_tokens",
    "total_tokens",
)


@dataclass(slots=True)
class UsageBucket:
    """Aggregated usage for one dimension value."""

    key: str
    request_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    cache_creation_tokens: int = 0
    total_tokens: int = 0
    prompt_values: list[int] = field(default_factory=list)
    cache_creation_values: list[int] = field(default_factory=list)

    def add(self, record: dict[str, Any]) -> None:
        self.request_count += 1
        self.prompt_tokens += _int(record.get("prompt_tokens"))
        self.completion_tokens += _int(record.get("completion_tokens"))
        self.reasoning_tokens += _int(record.get("reasoning_tokens"))
        self.cached_tokens += _int(record.get("cached_tokens"))
        self.cache_creation_tokens += _int(record.get("cache_creation_tokens"))
        self.total_tokens += _int(record.get("total_tokens"))
        self.prompt_values.append(_int(record.get("prompt_tokens")))
        self.cache_creation_values.append(_int(record.get("cache_creation_tokens")))

    @property
    def avg_prompt_tokens(self) -> float:
        if self.request_count <= 0:
            return 0.0
        return self.prompt_tokens / self.request_count

    @property
    def cache_hit_rate(self) -> float:
        if self.prompt_tokens <= 0:
            return 0.0
        return self.cached_tokens / self.prompt_tokens

    @property
    def prompt_p50(self) -> int:
        return percentile(self.prompt_values, 0.50)

    @property
    def prompt_p95(self) -> int:
        return percentile(self.prompt_values, 0.95)


@dataclass(slots=True)
class PromptComponentBucket:
    """Estimated prompt contribution for one structural component."""

    key: str
    request_count: int = 0
    char_count: int = 0
    estimated_prompt_tokens: int = 0
    unit_count: int = 0

    def add(
        self,
        *,
        chars: int,
        estimated_prompt_tokens: int,
        units: int = 0,
    ) -> None:
        self.request_count += 1
        self.char_count += chars
        self.estimated_prompt_tokens += estimated_prompt_tokens
        self.unit_count += units

    @property
    def avg_chars(self) -> float:
        if self.request_count <= 0:
            return 0.0
        return self.char_count / self.request_count

    @property
    def avg_estimated_prompt_tokens(self) -> float:
        if self.request_count <= 0:
            return 0.0
        return self.estimated_prompt_tokens / self.request_count


@dataclass(slots=True)
class UsageAnalysis:
    """Structured breakdown of a usage JSONL file."""

    total: UsageBucket
    by_agent: list[UsageBucket]
    by_operation: list[UsageBucket]
    by_model: list[UsageBucket]
    by_provider: list[UsageBucket]
    by_kv_flags: list[UsageBucket]
    by_tool_schema: list[UsageBucket]
    by_message_band: list[UsageBucket]
    by_prompt_source: list[PromptComponentBucket]
    by_runtime_component: list[PromptComponentBucket]
    top_prompt_records: list[dict[str, Any]]
    invalid_line_count: int = 0


def load_usage_records(path: str | Path) -> tuple[list[dict[str, Any]], int]:
    """Load usage JSONL records, skipping malformed lines.

    Returns ``(records, invalid_line_count)``.
    """

    p = Path(path)
    records: list[dict[str, Any]] = []
    invalid = 0
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


def analyze_usage_records(
    records: list[dict[str, Any]],
    *,
    invalid_line_count: int = 0,
    top_n: int = 10,
) -> UsageAnalysis:
    total = UsageBucket("all")
    for record in records:
        total.add(record)

    return UsageAnalysis(
        total=total,
        by_agent=_group(records, "agent"),
        by_operation=_group(records, "operation"),
        by_model=_group(records, "model"),
        by_provider=_group(records, "provider"),
        by_kv_flags=_group(records, _kv_flags_key),
        by_tool_schema=_group(records, _tool_schema_key),
        by_message_band=_group(records, _message_band_key),
        by_prompt_source=_prompt_component_breakdown(records, _PROMPT_SOURCE_FIELDS),
        by_runtime_component=_prompt_component_breakdown(
            records,
            _RUNTIME_COMPONENT_FIELDS,
        ),
        top_prompt_records=_top_prompt_records(records, top_n=top_n),
        invalid_line_count=invalid_line_count,
    )


def analyze_usage_file(path: str | Path, *, top_n: int = 10) -> UsageAnalysis:
    records, invalid = load_usage_records(path)
    return analyze_usage_records(records, invalid_line_count=invalid, top_n=top_n)


def format_usage_analysis(analysis: UsageAnalysis, *, top_n: int = 10) -> str:
    """Render a human-readable report for logs/CLI."""

    lines: list[str] = []
    total = analysis.total
    lines.append("模型用量分析")
    lines.append("")
    lines.append(
        "总览: "
        f"请求 {total.request_count:,} 次，"
        f"总 token {total.total_tokens:,}，"
        f"输入 {total.prompt_tokens:,}，"
        f"输出 {total.completion_tokens:,}，"
        f"思考 {total.reasoning_tokens:,}，"
        f"KV 命中 {total.cached_tokens:,}，"
        f"KV 写入 {total.cache_creation_tokens:,}，"
        f"命中率 {_fmt_rate(total.cache_hit_rate)}。"
    )
    if total.request_count:
        lines.append(
            "输入分布: "
            f"平均 {total.avg_prompt_tokens:,.0f}，"
            f"P50 {total.prompt_p50:,}，"
            f"P95 {total.prompt_p95:,}，"
            f"最大 {max(total.prompt_values):,}。"
        )
    if analysis.invalid_line_count:
        lines.append(f"跳过无法解析的 JSONL 行: {analysis.invalid_line_count:,}")

    _append_table(lines, "按 Agent", analysis.by_agent, top_n=top_n)
    _append_table(lines, "按 Operation", analysis.by_operation, top_n=top_n)
    _append_table(lines, "按 Model", analysis.by_model, top_n=top_n)
    _append_table(lines, "按 KV/Runtime 标记", analysis.by_kv_flags, top_n=top_n)
    _append_table(lines, "按工具 schema", analysis.by_tool_schema, top_n=top_n)
    _append_table(lines, "按消息数量区间", analysis.by_message_band, top_n=top_n)
    _append_component_table(
        lines,
        "按 Prompt 构成估算",
        analysis.by_prompt_source,
        top_n=top_n,
    )
    _append_component_table(
        lines,
        "按 Runtime 组件",
        analysis.by_runtime_component,
        top_n=top_n,
    )

    if analysis.top_prompt_records:
        lines.append("")
        lines.append(f"最大输入调用 Top {min(top_n, len(analysis.top_prompt_records))}:")
        for idx, record in enumerate(analysis.top_prompt_records[:top_n], 1):
            ts = _format_ts(record.get("ts"))
            label = " / ".join(
                part
                for part in (
                    str(record.get("agent") or ""),
                    str(record.get("operation") or ""),
                    str(record.get("model") or ""),
                )
                if part
            )
            lines.append(
                f"{idx}. {record.get('prompt_tokens', 0):>8,} prompt · "
                f"{record.get('total_tokens', 0):>8,} total · "
                f"{record.get('kv_message_count', '-') } msgs · "
                f"{record.get('kv_tools_count', '-') } tools · "
                f"{ts} · {label}"
            )

    lines.append("")
    lines.append(
        "说明: token 精确值来自 provider；Prompt 构成按 kv_*_char_count "
        "对 prompt_tokens 做比例估算。Runtime 组件可能与 user/tool 角色字符重叠，"
        "用于定位 task_context、send_receipt、RAG 和工具 schema 噪声来源。"
    )
    return "\n".join(lines)


def percentile(values: list[int], ratio: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    ratio = max(0.0, min(1.0, ratio))
    idx = round((len(ordered) - 1) * ratio)
    return ordered[idx]


_PROMPT_SOURCE_FIELDS = (
    ("system_messages", "kv_system_char_count", None),
    ("user_messages", "kv_user_char_count", None),
    ("assistant_messages", "kv_assistant_char_count", None),
    ("tool_results", "kv_tool_char_count", None),
    ("tools_schema", "kv_tools_char_count", "kv_tools_count"),
)

_RUNTIME_COMPONENT_FIELDS = (
    ("task_context", "kv_task_context_char_count", "kv_task_context_block_count"),
    ("send_receipt", "kv_send_receipt_char_count", "kv_send_receipt_block_count"),
    ("rag", "kv_rag_char_count", "kv_rag_block_count"),
    ("tools_schema", "kv_tools_char_count", "kv_tools_count"),
    ("recent_group_messages", None, "kv_recent_group_message_line_count"),
)


def _prompt_component_breakdown(
    records: list[dict[str, Any]],
    fields: tuple[tuple[str, str | None, str | None], ...],
) -> list[PromptComponentBucket]:
    buckets: dict[str, PromptComponentBucket] = {}
    for record in records:
        denominator = _prompt_source_char_denominator(record)
        for key, char_field, unit_field in fields:
            chars = _int(record.get(char_field)) if char_field else 0
            units = _int(record.get(unit_field)) if unit_field else 0
            if chars <= 0 and units <= 0:
                continue
            estimated = _component_prompt_tokens(record, chars, denominator)
            bucket = buckets.setdefault(key, PromptComponentBucket(key))
            bucket.add(chars=chars, estimated_prompt_tokens=estimated, units=units)
    return sorted(
        buckets.values(),
        key=lambda item: (
            item.estimated_prompt_tokens,
            item.char_count,
            item.unit_count,
            item.request_count,
        ),
        reverse=True,
    )


def _prompt_source_char_denominator(record: dict[str, Any]) -> int:
    denominator = (
        _int(record.get("kv_system_char_count"))
        + _int(record.get("kv_user_char_count"))
        + _int(record.get("kv_assistant_char_count"))
        + _int(record.get("kv_tool_char_count"))
        + _int(record.get("kv_tools_char_count"))
    )
    if denominator > 0:
        return denominator
    return _int(record.get("kv_content_char_count")) + _int(
        record.get("kv_tools_char_count")
    )


def _component_prompt_tokens(
    record: dict[str, Any],
    chars: int,
    denominator: int,
) -> int:
    prompt_tokens = _int(record.get("prompt_tokens"))
    if prompt_tokens <= 0 or chars <= 0 or denominator <= 0:
        return 0
    return round(prompt_tokens * chars / denominator)


def _group(records: list[dict[str, Any]], key: str | Callable[[dict[str, Any]], str]) -> list[UsageBucket]:
    buckets: dict[str, UsageBucket] = {}
    for record in records:
        if callable(key):
            bucket_key = str(key(record))
        else:
            bucket_key = str(record.get(key) or "(empty)")
        bucket = buckets.setdefault(bucket_key, UsageBucket(bucket_key))
        bucket.add(record)
    return sorted(
        buckets.values(),
        key=lambda item: (item.total_tokens, item.prompt_tokens, item.request_count),
        reverse=True,
    )


def _kv_flags_key(record: dict[str, Any]) -> str:
    flags = []
    if record.get("kv_has_send_receipt"):
        flags.append("send_receipt")
    if record.get("kv_has_recent_group_messages"):
        flags.append("recent_group")
    if record.get("kv_has_rag"):
        flags.append("rag")
    if not flags:
        return "none"
    return "+".join(flags)


def _tool_schema_key(record: dict[str, Any]) -> str:
    count = record.get("kv_tools_count")
    hash_value = str(record.get("kv_tools_hash") or "")
    char_count = record.get("kv_tools_char_count")
    full_count = record.get("kv_tools_full_count")
    stub_count = record.get("kv_tools_stub_count")
    if count is None and char_count is None and not hash_value:
        return "no_tools_diag"
    suffix = hash_value[:8] if hash_value else "nohash"
    parts = [f"{_int(count)} tools"]
    if full_count is not None or stub_count is not None:
        parts.append(f"{_int(full_count)} full/{_int(stub_count)} stub")
    if char_count is not None:
        parts.append(f"{_int(char_count)} chars")
    return f"{', '.join(parts)} / {suffix}"


def _message_band_key(record: dict[str, Any]) -> str:
    count = _int(record.get("kv_message_count"))
    if count <= 0:
        return "unknown"
    if count <= 16:
        return "001-016"
    if count <= 64:
        return "017-064"
    if count <= 128:
        return "065-128"
    if count <= 256:
        return "129-256"
    if count <= 512:
        return "257-512"
    return "513+"


def _top_prompt_records(records: list[dict[str, Any]], *, top_n: int) -> list[dict[str, Any]]:
    return sorted(
        records,
        key=lambda record: (
            _int(record.get("prompt_tokens")),
            _int(record.get("total_tokens")),
        ),
        reverse=True,
    )[:top_n]


def _append_table(
    lines: list[str],
    title: str,
    buckets: list[UsageBucket],
    *,
    top_n: int,
) -> None:
    if not buckets:
        return
    lines.append("")
    lines.append(f"{title}:")
    lines.append("  名称 | 请求 | 总 token | 输入 | KV 命中率 | P50/P95 输入")
    for bucket in buckets[:top_n]:
        lines.append(
            f"  {bucket.key} | "
            f"{bucket.request_count:,} | "
            f"{bucket.total_tokens:,} | "
            f"{bucket.prompt_tokens:,} | "
            f"{_fmt_rate(bucket.cache_hit_rate)} | "
            f"{bucket.prompt_p50:,}/{bucket.prompt_p95:,}"
        )


def _append_component_table(
    lines: list[str],
    title: str,
    buckets: list[PromptComponentBucket],
    *,
    top_n: int,
) -> None:
    if not buckets:
        return
    lines.append("")
    lines.append(f"{title}:")
    lines.append("  名称 | 请求 | 估算输入 token | 字符 | 平均字符 | 单位数")
    for bucket in buckets[:top_n]:
        lines.append(
            f"  {bucket.key} | "
            f"{bucket.request_count:,} | "
            f"{bucket.estimated_prompt_tokens:,} | "
            f"{bucket.char_count:,} | "
            f"{bucket.avg_chars:,.0f} | "
            f"{bucket.unit_count:,}"
        )


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _fmt_rate(value: float) -> str:
    return f"{value * 100:.1f}%"


def _format_ts(value: Any) -> str:
    ts = _int(value)
    if ts <= 0:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze Debata model_usage.jsonl")
    parser.add_argument(
        "path",
        nargs="?",
        default="data/logs/model_usage.jsonl",
        help="Path to model_usage.jsonl",
    )
    parser.add_argument("--top", type=int, default=10, help="Rows per section")
    args = parser.parse_args(argv)
    analysis = analyze_usage_file(args.path, top_n=max(1, args.top))
    print(format_usage_analysis(analysis, top_n=max(1, args.top)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
