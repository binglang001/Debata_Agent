"""sqlite archive 内部数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class _NormalizedRecord:
    role: str
    timestamp: str | None
    timestamp_unix: int | None
    date_key: str | None
    month_key: str | None
    conversation_id: str | None
    conversation_type: str
    target_id: str | None
    sender_id: str | None
    sender_name: str | None
    sender_role: str
    direction: str
    message_kind: str
    content: str
    content_search: str
    original_msg_id: str | None
    reply_to_msg_id: str | None
    metadata: dict[str, Any]
    record: dict[str, Any]


@dataclass(slots=True)
class _SqlTimeRange:
    start_ts: int | None
    end_ts: int | None
    needle: str | None


@dataclass(slots=True)
class _FilterSqlPlan:
    where_sql: str
    params: list[Any]
    has_python_residual_filter: bool
    fallback_where_sql: str | None = None
    fallback_params: list[Any] | None = None
