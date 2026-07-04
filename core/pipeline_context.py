"""Context construction helpers for MessagePipeline.

This module is a mechanical split from ``core.message_pipeline``. Keep behavior
equivalent; do not change prompt/context filtering logic while moving helpers.
"""

from __future__ import annotations

from typing import Any

from agents import build_task_context
from app_config.loader import ConfigError, get_config
from app_config.schema import ContextBudgetRecommendationConfig

from .pipeline_history import _record_timestamp


def _recommended_context_budget(model: str, context_length: int | None = None) -> int:
    """按配置规则推导工作上下文预算，不等于模型硬上限。"""
    try:
        rec = get_config().behavior.context.recommended_context_budget
    except ConfigError:
        rec = ContextBudgetRecommendationConfig()

    name = (model or "").lower()
    for pattern, budget in rec.model_name_budget_tokens.items():
        if pattern.lower() in name:
            return budget

    if context_length and context_length > 0:
        for rule in rec.context_length_rules:
            if context_length >= rule.min_context_length_tokens:
                return rule.budget_tokens
        scaled = int(context_length * rec.context_length_scale_percent / 100)
        return max(rec.min_scaled_budget_tokens, scaled)

    return rec.fallback_budget_tokens


def _make_task_context_record(
    task_context: str,
    *,
    conversation_id: str | None = None,
) -> dict[str, Any] | None:
    content = build_task_context(task_context)
    if not content:
        return None
    record: dict[str, Any] = {
        "role": "user",
        "content": content,
        "metadata": {"kind": "task_context_snapshot"},
    }
    if conversation_id:
        record["conversation_id"] = conversation_id
    return record


def _make_runtime_context_record(
    content: str,
    *,
    kind: str,
    tag: str,
    conversation_id: str | None = None,
) -> dict[str, Any] | None:
    content = content.strip()
    if not content:
        return None
    record: dict[str, Any] = {
        "role": "user",
        "content": (
            f"<{tag}>\n"
            "系统说明：以下内容由运行时系统提供，不是用户新发言。\n"
            f"{content}\n"
            f"</{tag}>"
        ),
        "metadata": {"kind": kind},
    }
    if conversation_id:
        record["conversation_id"] = conversation_id
    return record


def _earliest_record_ts(records: list[dict[str, Any]]) -> str | None:
    timestamps: list[str] = []
    for record in records:
        ts = _record_timestamp(record)
        if isinstance(ts, str) and ts:
            timestamps.append(ts)
    return min(timestamps) if timestamps else None


def _filter_tool_schemas(
    schemas: list[dict[str, Any]],
    denied_tools: set[str] | frozenset[str],
) -> list[dict[str, Any]]:
    # 保持工具 schema 名称集合稳定，避免不同系统事件触发时破坏 provider KV 缓存。
    # denied_tools 只在 executor 层拦截执行。
    return schemas
