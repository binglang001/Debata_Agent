"""本地历史归档筛选和召回工具。"""

from __future__ import annotations

import json
import time
from typing import Any

from memory.archive import real_chat_archive_records
from utils.token_budget import TokenEstimator

from ..base import ToolContext, tool
from ..result_shrink import tool_budget
from ..schemas import FilterArchiveRecordsArgs, RecallHistoryArgs
from ..workspace import relative_to_workspace
from .forward import _SAFE_PATH_RE


@tool(
    name="filter_archive_records",
    description=(
        "从本地永久归档按关键词、时间、会话、发送者和消息类型筛选候选记录。"
        "返回短 ID 和轻量内容；需要完整上下文时再把 ID 交给 recall_history。"
    ),
    args_model=FilterArchiveRecordsArgs,
    category="platform",
    schema_mode="stub",
    short_description="低频归档筛选工具。先用 tool_search 查询参数摘要；需要完整 schema 时 detail=full。",
    search_tags=["platform", "archive", "history", "recall"],
)
async def filter_archive_records(args: FilterArchiveRecordsArgs, ctx: ToolContext) -> dict:
    if ctx.archive is None:
        return {"ok": False, "error": "未配置本地历史归档"}

    raw_result = await ctx.archive.filter_records(args)
    result = dict(raw_result)
    raw_results = raw_result.get("results") or []
    result["results"] = [
        _archive_filter_summary_result(record)
        for record in raw_results
        if isinstance(record, dict)
    ]
    result["status"] = "inline"
    result["brief"] = (
        f"筛出 {result.get('count', 0)} 条候选归档记录"
        f"（总命中 {result.get('total', 0)} 条）。"
    )
    result["data"] = {
        "count": result.get("count", 0),
        "total": result.get("total", 0),
        "limit": result.get("limit"),
        "offset": result.get("offset"),
        "order": result.get("order"),
    }
    result["next"] = (
        "默认只返回摘要和归档 ID；需要完整原文或前后文时，把 results[].id "
        "传给 recall_history 的 archive_ids，并按需设置 context_before/context_after。"
    )
    return result


@tool(
    name="recall_history",
    description=(
        "从本地永久归档和当前活跃历史中检索较早的对话原文。"
        "当你需要想起已被压缩出工作窗口的旧细节、旧 msg_id、旧约定时使用；"
        "conversation_id 可限制到当前私聊/群聊，不填则全局检索。"
    ),
    args_model=RecallHistoryArgs,
    category="platform",
)
async def recall_history(args: RecallHistoryArgs, ctx: ToolContext) -> dict:
    if ctx.archive is None:
        return {"ok": False, "error": "未配置本地历史归档"}

    missing_ids: list[str] = []
    if args.archive_ids:
        records = []
        seen: set[str] = set()
        for archive_id in args.archive_ids:
            context_records = await ctx.archive.context_around(
                archive_id,
                args.context_before,
                args.context_after,
            )
            if not context_records:
                missing_ids.append(archive_id)
                continue
            for record in context_records:
                key = str(record.get("archive_id") or id(record))
                if key in seen:
                    continue
                seen.add(key)
                records.append(record)
    else:
        records = await ctx.archive.search(
            conversation_id=args.conversation_id,
            keyword=args.keyword,
            time_range=args.time_range,
            limit=args.limit,
        )
    if not args.archive_ids and ctx.history is not None:
        for record in await ctx.history.records():
            for chat_record in real_chat_archive_records(record):
                if _history_record_matches(
                    chat_record,
                    conversation_id=args.conversation_id,
                    keyword=args.keyword,
                    time_range=args.time_range,
                ):
                    records.append(chat_record)
        records = records[-args.limit:]

    markdown = _format_history_recall_markdown(records)
    snippets = [_history_recall_snippet(record) for record in records]
    meta = {
        "count": len(records),
        "archive_ids": list(args.archive_ids),
        "missing_archive_ids": missing_ids,
        "context_before": args.context_before,
        "context_after": args.context_after,
        "conversation_id": args.conversation_id,
        "keyword": args.keyword,
        "time_range": args.time_range,
        "range": "archive_id_context" if args.archive_ids else "continuous_result_order",
    }
    inline_result = {
        "ok": True,
        "status": "inline",
        "brief": f"找到 {len(records)} 条本地历史记录。",
        "count": len(snippets),
        "content": markdown,
        "results": snippets,
        "data": meta,
    }
    budget = tool_budget("recall_history", ctx)
    estimator = TokenEstimator()
    if _estimate_result(inline_result, estimator) <= budget.inline:
        return inline_result

    if ctx.workspace_dir is None:
        return {
            "ok": False,
            "status": "failed",
            "brief": "历史记录超过 inline 预算，但 workspace 未配置，无法写出完整文件。",
            "error": "历史记录超过 inline 预算，但 workspace 未配置",
            "count": len(snippets),
            "results": snippets,
            "data": meta,
        }

    path = _write_history_recall_artifact(
        ctx,
        markdown=markdown,
        meta=meta,
    )
    return {
        "ok": True,
        "status": "artifact",
        "brief": f"找到 {len(records)} 条本地历史记录，完整 Markdown 已写入 {path}。",
        "path": path,
        "artifact": {
            "path": path,
            "type": "markdown",
            "count": len(records),
        },
        "count": len(snippets),
        "results": _compact_history_recall_snippets(snippets, limit=3),
        "data": meta,
        "next": "需要分析完整历史时，把 artifact.path 交给 start_agent_task 或用 read_file 分页读取。",
    }


def _history_record_matches(
    record: dict,
    *,
    conversation_id: str | None,
    keyword: str | None,
    time_range: str | None,
) -> bool:
    if conversation_id and record.get("conversation_id") != conversation_id:
        return False
    text = "\n".join(
        [
            str(record.get("content") or ""),
            str(record.get("metadata") or ""),
        ]
    )
    keyword = (keyword or "").strip()
    if keyword and keyword not in text:
        return False
    time_range = (time_range or "").strip()
    if time_range and time_range not in text:
        return False
    return True


def _archive_filter_summary_result(record: dict[str, Any]) -> dict[str, Any]:
    content = str(record.get("content") or record.get("summary") or "").strip()
    return {
        "id": record.get("id") or record.get("archive_id"),
        "time": record.get("time") or record.get("timestamp"),
        "conversation_id": record.get("conversation_id"),
        "sender": record.get("sender"),
        "sender_id": record.get("sender_id"),
        "sender_name": record.get("sender_name"),
        "direction": record.get("direction"),
        "kind": record.get("kind") or record.get("message_kind"),
        "snippet": _compact_archive_snippet(content),
    }


def _compact_archive_snippet(content: str, *, limit: int = 120) -> str:
    compacted = " ".join(content.split())
    if len(compacted) <= limit:
        return compacted
    return compacted[: limit - 1].rstrip() + "..."


def _format_history_recall_markdown(records: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for record in records:
        lines.extend(_format_history_recall_record(record))
    return "\n".join(lines)


def _format_history_recall_record(record: dict[str, Any]) -> list[str]:
    timestamp = _summary_timestamp(record) or "-"
    archive_id = str(record.get("archive_id") or record.get("id") or "").strip()
    id_part = f"#{archive_id} " if archive_id else ""
    conversation_id = str(record.get("conversation_id") or "-")
    role = str(record.get("role") or "-")
    sender = _history_recall_sender(record, role)
    content = str(record.get("content") or "").strip()
    if not content:
        content = "(空内容)"
    lines = [
        f"{timestamp} {id_part}[{conversation_id}] {sender}({role})：{line}"
        for line in content.splitlines()
    ]
    return lines or [f"{timestamp} {id_part}[{conversation_id}] {sender}({role})：(空内容)"]


def _history_recall_sender(record: dict[str, Any], role: str) -> str:
    meta = record.get("metadata")
    if isinstance(meta, dict):
        messages = meta.get("messages")
        if isinstance(messages, list) and messages:
            first = messages[0]
            if isinstance(first, dict):
                nickname = first.get("nickname")
                user_id = first.get("user_id")
                if nickname and user_id:
                    return f"{nickname}({user_id})"
                if nickname:
                    return str(nickname)
        if meta.get("nickname"):
            return str(meta.get("nickname"))
    if role == "assistant":
        return "assistant"
    if role == "system":
        return "system"
    if role == "tool":
        return "tool"
    return "user"


def _history_recall_snippet(record: dict[str, Any]) -> dict[str, Any]:
    content = str(record.get("content") or "")
    return {
        "id": record.get("archive_id") or record.get("id"),
        "role": record.get("role"),
        "conversation_id": record.get("conversation_id"),
        "timestamp": _summary_timestamp(record) or None,
        "sender": _history_recall_sender(record, str(record.get("role") or "")),
        "content": content[:160],
    }


def _compact_history_recall_snippets(
    snippets: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for item in snippets[:limit]:
        copied = dict(item)
        copied["content"] = str(copied.get("content") or "")[:80]
        compacted.append(copied)
    return compacted


def _write_history_recall_artifact(
    ctx: ToolContext,
    *,
    markdown: str,
    meta: dict[str, Any],
) -> str:
    if ctx.workspace_dir is None:
        raise RuntimeError("workspace 未配置")
    out_dir = ctx.workspace_dir / "runtime" / "history_recall"
    out_dir.mkdir(parents=True, exist_ok=True)
    scope = str(meta.get("conversation_id") or "global")
    safe_scope = _SAFE_PATH_RE.sub("_", scope).strip("._") or "history"
    path = out_dir / f"{safe_scope}_{int(time.time() * 1000)}.md"
    header = (
        "# 本地历史检索结果\n\n"
        f"- 记录数：{meta.get('count')}\n"
        f"- 会话：{meta.get('conversation_id') or '-'}\n"
        f"- 关键词：{meta.get('keyword') or '-'}\n"
        f"- 时间范围：{meta.get('time_range') or '-'}\n\n"
        "---\n\n"
    )
    path.write_text(header + markdown + ("\n" if markdown else ""), encoding="utf-8")
    return relative_to_workspace(path, ctx.workspace_dir)


def _summary_timestamp(record: dict[str, Any]) -> str:
    meta = record.get("metadata")
    if isinstance(meta, dict):
        if meta.get("timestamp") is not None:
            return str(meta.get("timestamp"))
        messages = meta.get("messages")
        if isinstance(messages, list) and messages:
            last = messages[-1]
            if isinstance(last, dict) and last.get("timestamp") is not None:
                return str(last.get("timestamp"))
    return ""


def _estimate_result(result: dict[str, Any], estimator: TokenEstimator) -> int:
    return estimator.estimate_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True)
    )
