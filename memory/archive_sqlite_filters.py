"""sqlite archive 过滤与 SQL helper。"""

from __future__ import annotations

import sqlite3
from difflib import SequenceMatcher
from typing import Any

from .archive_sqlite_models import _FilterSqlPlan, _SqlTimeRange
from .archive_sqlite_records import (
    _REAL_CHAT_DIRECTIONS,
    _REAL_CHAT_MESSAGE_KINDS,
    _REAL_CHAT_ROLES,
    _RUNTIME_CONTEXT_MARKERS,
    _RUNTIME_METADATA_KINDS,
    _clean_optional,
    _legacy_search_text,
    _string_list,
    _time_range_list,
    _timestamp_parts,
)


def _filter_sql_plan(query: dict[str, Any]) -> _FilterSqlPlan:
    base_clauses, base_params = _real_chat_sql_conditions()
    has_python_residual_filter = False

    archive_ids = _string_list(query.get("archive_ids"))
    if archive_ids:
        base_clauses.append(f"archive_id IN ({_placeholders(len(archive_ids))})")
        base_params.extend(archive_ids)

    conversation_ids = _string_list(query.get("conversation_ids"))
    conversation_match = str(query.get("conversation_match") or "exact")
    push_raw_conversation_ids = conversation_ids if conversation_match == "exact" else []
    if conversation_ids:
        has_python_residual_filter = True

    sender_ids = _string_list(query.get("sender_ids"))
    if sender_ids:
        base_clauses.append(f"sender_id IN ({_placeholders(len(sender_ids))})")
        base_params.extend(sender_ids)

    sender_names = _string_list(query.get("sender_names"))
    if sender_names:
        has_python_residual_filter = True

    message_kinds = _string_list(query.get("message_kinds"))
    if message_kinds:
        base_clauses.append(f"message_kind IN ({_placeholders(len(message_kinds))})")
        base_params.extend(message_kinds)

    time_ranges = _time_range_list(query.get("time_ranges"))
    if time_ranges:
        parsed_time_ranges = _sql_time_ranges(time_ranges)
        if parsed_time_ranges is None:
            has_python_residual_filter = True
        else:
            time_sql, time_params = _time_ranges_sql(parsed_time_ranges)
            base_clauses.append(time_sql)
            base_params.extend(time_params)

    if _string_list(query.get("keywords")):
        has_python_residual_filter = True

    clauses = list(base_clauses)
    params = list(base_params)
    fallback_clauses: list[str] | None = None
    fallback_params: list[Any] | None = None
    if push_raw_conversation_ids:
        placeholders = _placeholders(len(push_raw_conversation_ids))
        clauses.append(f"conversation_id IN ({placeholders})")
        params.extend(push_raw_conversation_ids)
        fallback_clauses = list(base_clauses)
        fallback_params = list(base_params)
        fallback_clauses.append(
            f"(conversation_id NOT IN ({placeholders}) OR conversation_id IS NULL)"
        )
        fallback_params.extend(push_raw_conversation_ids)

    return _FilterSqlPlan(
        where_sql=_join_filter_clauses(clauses),
        params=params,
        has_python_residual_filter=has_python_residual_filter,
        fallback_where_sql=(
            _join_filter_clauses(fallback_clauses)
            if fallback_clauses is not None
            else None
        ),
        fallback_params=fallback_params,
    )


def _real_chat_sql_conditions() -> tuple[list[str], list[Any]]:
    metadata_kind = _json_extract_sql("metadata_json", "$.kind")
    metadata_is_object = f"COALESCE(({_json_root_type_sql('metadata_json')} = 'object'), 0)"
    metadata_visible = _json_type_sql("metadata_json", "$.qq_visible") + " = 'true'"
    metadata_source = _json_extract_sql("metadata_json", "$.source")
    metadata_outbound_proof = (
        f"({metadata_is_object} AND {metadata_visible} AND {metadata_source} = ?)"
    )

    record_is_object = f"COALESCE(({_json_root_type_sql('record_json')} = 'object'), 0)"
    record_role = (
        f"COALESCE(NULLIF(CAST({_json_extract_sql('record_json', '$.role')} AS TEXT), ''), "
        "sender_role, '')"
    )
    record_visible = _json_type_sql("record_json", "$.qq_visible") + " = 'true'"
    record_source = _json_extract_sql("record_json", "$.source")
    record_outbound_proof = (
        f"({record_is_object} AND {record_visible} AND {record_source} = ?)"
    )
    outbound_proof = f"({record_outbound_proof} OR {metadata_outbound_proof})"
    record_object_ok = (
        f"{record_is_object} "
        f"AND {record_role} IN ({_placeholders(len(_REAL_CHAT_ROLES))}) "
        f"AND NOT ({_json_truthy_sql('record_json', '$.tool_calls')}) "
        f"AND NOT ({_json_truthy_sql('record_json', '$.reasoning_content')}) "
        f"AND NOT ({_json_truthy_sql('record_json', '$.reasoning_blocks')}) "
        f"AND ({record_role} <> ? OR {outbound_proof})"
    )
    record_non_object_ok = (
        f"NOT ({record_is_object}) "
        f"AND (COALESCE(sender_role, '') <> ? OR {metadata_outbound_proof})"
    )

    clauses = [
        f"COALESCE(sender_role, '') IN ({_placeholders(len(_REAL_CHAT_ROLES))})",
        f"direction IN ({_placeholders(len(_REAL_CHAT_DIRECTIONS))})",
        f"message_kind IN ({_placeholders(len(_REAL_CHAT_MESSAGE_KINDS))})",
        "TRIM(COALESCE(content, ''), char(9) || char(10) || char(11) || char(12) || char(13) || char(32)) <> ''",
        "LOWER(TRIM(COALESCE(conversation_id, ''))) NOT LIKE 'system:%'",
        "LOWER(TRIM(COALESCE(conversation_id, ''))) NOT LIKE 'runtime:%'",
        "LOWER(TRIM(COALESCE(conversation_id, ''))) NOT LIKE 'internal:%'",
        *["instr(COALESCE(content, ''), ?) = 0" for _ in _RUNTIME_CONTEXT_MARKERS],
        (
            "COALESCE("
            f"({metadata_is_object} AND {metadata_kind} "
            f"IN ({_placeholders(len(_RUNTIME_METADATA_KINDS))})), 0"
            ") = 0"
        ),
        f"(({record_object_ok}) OR ({record_non_object_ok}))",
    ]
    params: list[Any] = [
        *sorted(_REAL_CHAT_ROLES),
        *sorted(_REAL_CHAT_DIRECTIONS),
        *sorted(_REAL_CHAT_MESSAGE_KINDS),
        *_RUNTIME_CONTEXT_MARKERS,
        *sorted(_RUNTIME_METADATA_KINDS),
        *sorted(_REAL_CHAT_ROLES),
        "assistant",
        "send_result",
        "send_result",
        "assistant",
        "send_result",
    ]
    return clauses, params


def _sql_time_ranges(ranges: list[dict[str, Any]]) -> list[_SqlTimeRange] | None:
    result: list[_SqlTimeRange] = []
    for item in ranges:
        start_raw = _clean_optional(item.get("start"))
        end_raw = _clean_optional(item.get("end"))
        start_ts = _timestamp_parts(start_raw)[0] if start_raw else None
        end_ts = _timestamp_parts(end_raw)[0] if end_raw else None
        if (start_raw and start_ts is None) or (end_raw and end_ts is None):
            return None
        if start_ts is None and end_ts is None:
            return None
        needle = " ".join(value for value in (start_raw, end_raw) if value) or None
        result.append(_SqlTimeRange(start_ts=start_ts, end_ts=end_ts, needle=needle))
    return result


def _time_ranges_sql(ranges: list[_SqlTimeRange]) -> tuple[str, list[Any]]:
    range_clauses: list[str] = []
    params: list[Any] = []
    for item in ranges:
        timestamp_clauses = ["timestamp_unix IS NOT NULL"]
        if item.start_ts is not None:
            timestamp_clauses.append("timestamp_unix >= ?")
            params.append(item.start_ts)
        if item.end_ts is not None:
            timestamp_clauses.append("timestamp_unix <= ?")
            params.append(item.end_ts)
        range_clause = "(" + " AND ".join(timestamp_clauses) + ")"
        if item.needle:
            range_clause = (
                f"({range_clause} OR "
                f"(timestamp_unix IS NULL AND {_legacy_search_text_sql()}))"
            )
            params.append(item.needle)
        range_clauses.append(range_clause)
    return "(" + " OR ".join(range_clauses) + ")", params


def _legacy_search_text_sql() -> str:
    fields = (
        "timestamp",
        "date_key",
        "month_key",
        "conversation_id",
        "sender_id",
        "sender_name",
        "content_search",
        "original_msg_id",
        "reply_to_msg_id",
    )
    expression = " || char(10) || ".join(f"COALESCE({field}, '')" for field in fields)
    return f"instr({expression}, ?) > 0"


def _filter_order_sql(reverse: bool) -> str:
    direction = "DESC" if reverse else "ASC"
    return f"COALESCE(timestamp_unix, -1) {direction}, rowid {direction}"


def _filter_row_sort_key(row: sqlite3.Row) -> tuple[int, int]:
    return (
        row["timestamp_unix"] if row["timestamp_unix"] is not None else -1,
        row["rowid"],
    )


def _unique_rows_by_rowid(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    seen: set[int] = set()
    result: list[sqlite3.Row] = []
    for row in rows:
        rowid = int(row["rowid"])
        if rowid in seen:
            continue
        seen.add(rowid)
        result.append(row)
    return result


def _json_extract_sql(column: str, path: str) -> str:
    return (
        f"CASE WHEN json_valid({column}) THEN json_extract({column}, '{path}') ELSE NULL END"
    )


def _json_type_sql(column: str, path: str) -> str:
    return f"CASE WHEN json_valid({column}) THEN json_type({column}, '{path}') ELSE NULL END"


def _json_root_type_sql(column: str) -> str:
    return f"CASE WHEN json_valid({column}) THEN json_type({column}) ELSE NULL END"


def _json_truthy_sql(column: str, path: str) -> str:
    value = _json_extract_sql(column, path)
    value_type = _json_type_sql(column, path)
    return (
        "CASE "
        f"WHEN {value_type} = 'true' THEN 1 "
        f"WHEN {value_type} IN ('integer', 'real') THEN {value} != 0 "
        f"WHEN {value_type} = 'text' THEN COALESCE(CAST({value} AS TEXT), '') != '' "
        f"WHEN {value_type} = 'array' THEN COALESCE(json_array_length({column}, '{path}'), 0) > 0 "
        f"WHEN {value_type} = 'object' THEN EXISTS (SELECT 1 FROM json_each({column}, '{path}')) "
        "ELSE 0 END"
    )


def _placeholders(count: int) -> str:
    return ",".join("?" for _ in range(count))


def _join_filter_clauses(clauses: list[str]) -> str:
    return " AND ".join(f"({clause})" for clause in clauses)


def _row_matches_filter(row: sqlite3.Row, query: dict[str, Any]) -> bool:
    archive_ids = _string_list(query.get("archive_ids"))
    if archive_ids and row["archive_id"] not in archive_ids:
        return False
    conversation_ids = _string_list(query.get("conversation_ids"))
    if conversation_ids and not _matches_values(
        str(row["conversation_id"] or ""),
        conversation_ids,
        str(query.get("conversation_match") or "exact"),
    ):
        return False
    sender_ids = _string_list(query.get("sender_ids"))
    if sender_ids and str(row["sender_id"] or "") not in sender_ids:
        return False
    sender_names = _string_list(query.get("sender_names"))
    if sender_names and not _matches_values(
        str(row["sender_name"] or ""),
        sender_names,
        str(query.get("sender_match") or "exact"),
    ):
        return False
    message_kinds = _string_list(query.get("message_kinds"))
    if message_kinds and row["message_kind"] not in message_kinds:
        return False
    if not _matches_time_ranges(row, query.get("time_ranges")):
        return False
    keywords = _string_list(query.get("keywords"))
    if keywords and not _matches_keywords(
        str(row["content_search"] or ""),
        keywords,
        str(query.get("keyword_match") or "contains"),
        str(query.get("keyword_operator") or "all"),
    ):
        return False
    return True


def _matches_values(text: str, values: list[str], mode: str) -> bool:
    if not values:
        return True
    text_norm = text.lower()
    for value in values:
        value_norm = value.lower()
        if mode == "exact" and text_norm == value_norm:
            return True
        if mode == "contains" and value_norm in text_norm:
            return True
        if mode == "fuzzy" and _fuzzy_match(text_norm, value_norm):
            return True
    return False


def _matches_keywords(text: str, keywords: list[str], mode: str, operator: str) -> bool:
    checks = [_matches_values(text, [keyword], mode) for keyword in keywords]
    if operator == "any":
        return any(checks)
    return all(checks)


def _matches_time_ranges(row: sqlite3.Row, raw_ranges: Any) -> bool:
    ranges = _time_range_list(raw_ranges)
    if not ranges:
        return True
    ts = row["timestamp_unix"]
    text = _legacy_search_text(row)
    for item in ranges:
        start_raw = item.get("start")
        end_raw = item.get("end")
        start_ts = _timestamp_parts(_clean_optional(start_raw))[0] if start_raw else None
        end_ts = _timestamp_parts(_clean_optional(end_raw))[0] if end_raw else None
        if ts is not None and (start_ts is not None or end_ts is not None):
            if start_ts is not None and ts < start_ts:
                continue
            if end_ts is not None and ts > end_ts:
                continue
            return True
        needle = " ".join(
            value for value in (_clean_optional(start_raw), _clean_optional(end_raw)) if value
        )
        if needle and needle in text:
            return True
    return False


def _fuzzy_match(text: str, needle: str) -> bool:
    if not needle:
        return True
    if needle in text:
        return True
    if not text:
        return False
    if SequenceMatcher(None, text, needle).ratio() >= 0.62:
        return True
    size = len(needle)
    if size <= 1:
        return False
    for width in range(max(1, size - 2), min(len(text), size + 6) + 1):
        for start in range(0, max(1, len(text) - width + 1)):
            if SequenceMatcher(None, text[start:start + width], needle).ratio() >= 0.72:
                return True
    return False
