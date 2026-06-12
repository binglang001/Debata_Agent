"""Shared history-record helpers for MessagePipeline."""

from __future__ import annotations

from typing import Any


def _record_timestamp(record: dict[str, Any]) -> Any:
    meta = record.get("metadata")
    if isinstance(meta, dict):
        if meta.get("timestamp") is not None:
            return meta.get("timestamp")
        messages = meta.get("messages")
        if isinstance(messages, list) and messages:
            last = messages[-1]
            if isinstance(last, dict):
                return last.get("timestamp")
    return None
