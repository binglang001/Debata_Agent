"""工具结果创建即定型压缩。

这里的规则只在工具结果刚返回时执行一次；之后写入 history 的字节不再回改，
避免破坏 KV 缓存前缀。
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from utils.token_budget import TokenEstimator


_LINE_TOOLS = {"run_python", "get_forward_msg"}
_SUMMARY_TOOLS = {"summarize_chat_history"}


def shrink_tool_result(tool_name: str, result: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """按工具名做确定性精简，再做中央 hard cap 兜底。"""
    if not isinstance(result, dict):
        return {"ok": True, "value": result}

    soft_limit = _soft_limit(tool_name, ctx)
    hard_cap = max(soft_limit, int(getattr(ctx, "tool_result_hard_cap_tokens", 1500) or 1500))
    estimator = TokenEstimator()

    shrunk = copy.deepcopy(result)
    if tool_name == "web_search":
        shrunk = _shrink_web_search(shrunk)
    elif tool_name in {"list_contacts", "list_files"}:
        shrunk = _shrink_contacts(shrunk)
    elif tool_name in _LINE_TOOLS:
        shrunk = _shrink_line_result(tool_name, shrunk, soft_limit, estimator)
    elif tool_name in _SUMMARY_TOOLS:
        shrunk = _shrink_text_field(
            shrunk,
            field="summary",
            limit_tokens=soft_limit,
            reason="摘要过长已精简",
            full="如需更细节，请缩小总结范围或重新调用工具。",
            estimator=estimator,
        )

    return _hard_cap(tool_name, shrunk, hard_cap, estimator)


def add_condensed_marker(result: dict[str, Any], *, reason: str, full: str) -> dict[str, Any]:
    marker = dict(result.get("_condensed") or {})
    marker.update({"reason": reason, "full": full})
    result["_condensed"] = marker
    return result


def _soft_limit(tool_name: str, ctx: Any) -> int:
    overrides = getattr(ctx, "tool_result_soft_overrides", None) or {}
    if isinstance(overrides, dict):
        try:
            value = overrides.get(tool_name)
            if value:
                return max(64, int(value))
        except (TypeError, ValueError):
            pass
    return max(64, int(getattr(ctx, "tool_result_soft_limit_tokens", 600) or 600))


def _estimate_dict(result: dict[str, Any], estimator: TokenEstimator) -> int:
    return estimator.estimate_text(json.dumps(result, ensure_ascii=False, sort_keys=True))


def _shrink_web_search(result: dict[str, Any]) -> dict[str, Any]:
    value = result.get("result")
    if not isinstance(value, str):
        return result

    entries = _split_numbered_results(value)
    if len(entries) <= 5 and len(value) <= 5000:
        return result
    kept = entries[:5] if entries else value.splitlines()[:20]
    result["result"] = "\n\n".join(_trim_chars(item, 360) for item in kept)
    return add_condensed_marker(
        result,
        reason="搜索结果过长已保留前 5 条",
        full="可用更具体 query 重新搜索，或打开结果 URL 获取完整内容。",
    )


def _split_numbered_results(text: str) -> list[str]:
    entries: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^\d+\.\s+", stripped):
            if current:
                entries.append("\n".join(current).strip())
            current = [line]
        elif current:
            current.append(line)
    if current:
        entries.append("\n".join(current).strip())
    return [item for item in entries if item]


def _shrink_contacts(result: dict[str, Any]) -> dict[str, Any]:
    for key in ("friends", "groups", "members", "entries"):
        value = result.get(key)
        if isinstance(value, list) and len(value) > 50:
            result[key] = value[:50]
            return add_condensed_marker(
                result,
                reason=f"{key} 条目过多，仅保留前 50 条",
                full="如需完整列表，请缩小查询范围或重新调用 list_contacts。",
            )
    return result


def _shrink_line_result(
    tool_name: str,
    result: dict[str, Any],
    soft_limit: int,
    estimator: TokenEstimator,
) -> dict[str, Any]:
    if _estimate_dict(result, estimator) <= soft_limit:
        return result

    fields = ("stdout", "stderr") if tool_name == "run_python" else ("content",)
    changed = False
    for field in fields:
        value = result.get(field)
        if not isinstance(value, str) or not value:
            continue
        lines = value.splitlines()
        if len(lines) <= 80 and estimator.estimate_text(value) <= soft_limit:
            continue
        head = lines[:50]
        tail = lines[-20:] if len(lines) > 70 else []
        omitted = max(0, len(lines) - len(head) - len(tail))
        middle = [f"...（省略 {omitted} 行）..."] if omitted else []
        result[field] = "\n".join(head + middle + tail)
        changed = True

    if changed:
        return add_condensed_marker(
            result,
            reason="工具输出过长已保留头尾",
            full="如需完整输出，请让工具把结果写入 workspace 文件后用 read_file 分页查看。",
        )
    return result


def _shrink_text_field(
    result: dict[str, Any],
    *,
    field: str,
    limit_tokens: int,
    reason: str,
    full: str,
    estimator: TokenEstimator,
) -> dict[str, Any]:
    value = result.get(field)
    if not isinstance(value, str) or estimator.estimate_text(value) <= limit_tokens:
        return result
    result[field] = _trim_head_tail(value, limit_tokens, estimator)
    return add_condensed_marker(result, reason=reason, full=full)


def _hard_cap(
    tool_name: str,
    result: dict[str, Any],
    hard_cap: int,
    estimator: TokenEstimator,
) -> dict[str, Any]:
    if _estimate_dict(result, estimator) <= hard_cap:
        return result

    compact = {
        "ok": result.get("ok", True),
        "tool": tool_name,
        "preview": _trim_head_tail(
            json.dumps(result, ensure_ascii=False, sort_keys=True),
            hard_cap,
            estimator,
        ),
    }
    return add_condensed_marker(
        compact,
        reason="工具结果超过中央 hard cap，已通用截断",
        full="请使用更窄参数重调工具，或让工具把完整结果写入 workspace 文件。",
    )


def _trim_chars(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n...（已截断）"


def _trim_head_tail(text: str, limit_tokens: int, estimator: TokenEstimator) -> str:
    if estimator.estimate_text(text) <= limit_tokens:
        return text
    marker = "\n...[已按 token 预算截断]...\n"
    marker_cost = estimator.estimate_text(marker)
    if limit_tokens <= marker_cost + 16:
        return text[: max(1, limit_tokens * 2)]

    head_budget = max(1, (limit_tokens - marker_cost) // 2)
    tail_budget = max(1, limit_tokens - marker_cost - head_budget)
    return f"{_fit_prefix(text, head_budget, estimator)}{marker}{_fit_suffix(text, tail_budget, estimator)}"


def _fit_prefix(text: str, limit: int, estimator: TokenEstimator) -> str:
    lo, hi = 0, len(text)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = text[:mid]
        if estimator.estimate_text(candidate) <= limit:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best.rstrip()


def _fit_suffix(text: str, limit: int, estimator: TokenEstimator) -> str:
    lo, hi = 0, len(text)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = text[len(text) - mid :]
        if estimator.estimate_text(candidate) <= limit:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best.lstrip()
