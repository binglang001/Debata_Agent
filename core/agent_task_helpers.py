"""Helper functions for MessagePipeline background agent tasks.

This module is a mechanical split from ``core.message_pipeline``. Keep behavior
equivalent; do not change agent task naming, timeout, path, or manifest logic
while moving helpers.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def _safe_agent_task_filename(name: str, *, default: str, suffix: str) -> str:
    """把模型给的输出文件名压成 workspace 内单文件名。"""
    raw = Path(name or default).name.strip() or default
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in raw)
    if not safe:
        safe = default
    if not safe.lower().endswith(suffix):
        safe += suffix
    return safe[:120]


def _clamp_agent_task_max_loops(value: Any, default: int) -> int:
    try:
        raw = int(value if value is not None else default)
    except (TypeError, ValueError):
        raw = default
    return min(60, max(5, raw))


def _agent_task_timeout_seconds(value: Any, *, max_loops: int, first_token_timeout: float) -> float:
    """后台任务总超时。

    正常终止仍由工具循环和 stop_after_tool 控制；这里只作为最后保险，
    避免模型/工具卡住后工具调用永远不返回。
    """
    try:
        raw = float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        raw = 0.0
    if raw > 0:
        return min(3600.0, max(60.0, raw))

    per_loop = max(30.0, float(first_token_timeout or 30.0) * 4 + 60.0)
    return min(3600.0, max(180.0, per_loop * max(1, max_loops) + 60.0))


def _stable_json_hash(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _agent_task_source_hash(sources: Any) -> str:
    return _stable_json_hash(sources if isinstance(sources, list) else [])


def _agent_task_prompt_hash(payload: dict[str, Any]) -> str:
    return _stable_json_hash(
        {
            "prompt": str(payload.get("prompt") or ""),
            "output_format": str(payload.get("output_format") or "markdown"),
        }
    )


def _agent_task_dedupe_key(
    *,
    source_hash: str,
    prompt_hash: str,
    output_name: str,
) -> str:
    return f"{source_hash}:{prompt_hash}:{output_name}"


def _summarize_agent_task_manifest(manifest: dict[str, Any]) -> dict[str, int]:
    summary = {
        "message_count": 0,
        "nested_forward_count": 0,
        "expired_forward_count": 0,
        "image_count": 0,
        "truncated_count": 0,
    }
    for item in manifest.get("sources") or []:
        if not isinstance(item, dict):
            continue
        for key in summary:
            summary[key] += int(item.get(key) or 0)
        if item.get("truncated") is True:
            summary["truncated_count"] += 1
        if item.get("error") and "截断" in str(item.get("error")):
            summary["truncated_count"] += 1
    return summary


def _agent_task_partial_text(
    *,
    task_id: str,
    prompt: str,
    result: Any,
    output_rel: str,
    max_loops: int,
) -> str:
    lines = [
        f"# 后台子 Agent 部分结果：{task_id}",
        "",
        f"- 状态：达到工具循环上限 {max_loops} 轮",
        f"- 目标输出文件：{output_rel}",
        f"- 已执行轮数：{getattr(result, 'loop_count', 0)}",
        "",
        "## 任务说明",
        "",
        prompt.strip() or "（无）",
        "",
    ]
    final_content = str(getattr(result, "final_content", "") or "").strip()
    if final_content:
        lines.extend(["## 最后一轮模型输出", "", final_content, ""])

    records = list(getattr(result, "records", []) or [])
    if records:
        lines.extend(["## 最近执行记录", ""])
        for record in records[-8:]:
            role = str(record.get("role") or "?")
            content = str(record.get("content") or "").strip()
            tool_calls = record.get("tool_calls") or []
            if content:
                lines.append(f"- {role}: {content[:500]}")
            elif tool_calls:
                names = []
                for tool_call in tool_calls:
                    func = tool_call.get("function") if isinstance(tool_call, dict) else None
                    if isinstance(func, dict):
                        names.append(str(func.get("name") or "?"))
                lines.append(f"- {role}: 调用工具 {', '.join(names) if names else '?'}")
        lines.append("")

    lines.append("任务没有失败，但还没有在轮数上限内完整收尾。可以提高 max_loops 或基于当前文件继续处理。")
    return "\n".join(lines)


def _resolve_agent_workspace_path(value: str, workspace_dir: Path | None) -> Path:
    from tools.workspace import resolve_in_workspace

    return resolve_in_workspace(value, workspace_dir)


def _workspace_rel(path: Path | None, workspace_dir: Path | None) -> str:
    if path is None or workspace_dir is None:
        return ""
    try:
        return str(path.resolve(strict=False).relative_to(workspace_dir.resolve(strict=False))).replace("\\", "/")
    except Exception:
        return str(path)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _agent_record_matches(
    record: dict[str, Any],
    *,
    conversation_id: str | None,
    keyword: str | None,
    time_range: str | None,
) -> bool:
    if conversation_id and record.get("conversation_id") != conversation_id:
        return False
    text = "\n".join([str(record.get("content") or ""), str(record.get("metadata") or "")])
    keyword = (keyword or "").strip()
    if keyword and keyword not in text:
        return False
    time_range = (time_range or "").strip()
    if time_range and time_range not in text:
        return False
    return True


def _record_has_message_id(record: dict[str, Any], message_id: str) -> bool:
    meta = record.get("metadata")
    if isinstance(meta, dict):
        messages = meta.get("messages")
        if isinstance(messages, list):
            for msg in messages:
                if isinstance(msg, dict) and str(msg.get("message_id") or msg.get("msg_id") or "") == message_id:
                    return True
    return message_id in str(record.get("content") or "")


def _file_head_tail_preview(path: Path, *, lines: int = 8) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    all_lines = text.splitlines()
    return {
        "bytes": path.stat().st_size,
        "line_count": len(all_lines),
        "head": "\n".join(all_lines[:lines]),
        "tail": "\n".join(all_lines[-lines:]) if len(all_lines) > lines else "",
    }


def _first_meaningful_line(text: str, *, max_chars: int = 160) -> str:
    for line in text.splitlines():
        stripped = line.strip().strip("#").strip()
        if stripped:
            return stripped[:max_chars]
    return ""
