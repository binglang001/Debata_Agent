"""工具结果创建即定型压缩。

这里的规则只在工具结果刚返回时执行一次；之后写入 history 的字节不再回改，
避免破坏 KV 缓存前缀。
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any

from utils.token_budget import TokenEstimator

_LINE_TOOLS = {"get_forward_msg"}


@dataclass(frozen=True, slots=True)
class ToolBudget:
    inline: int
    artifact_threshold: int
    hard_cap: int


def shrink_tool_result(tool_name: str, result: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """按工具名做确定性精简，再做中央 hard cap 兜底。"""
    if not isinstance(result, dict):
        return {"ok": True, "value": result}

    budget = tool_budget(tool_name, ctx)
    soft_limit = budget.inline
    hard_cap = budget.hard_cap
    estimator = TokenEstimator()

    shrunk = copy.deepcopy(result)
    if tool_name == "web_search":
        shrunk = _shrink_web_search(shrunk, soft_limit, estimator)
    elif tool_name == "list_contacts":
        shrunk = _shrink_contacts(tool_name, shrunk)
    elif tool_name in _LINE_TOOLS:
        shrunk = _shrink_line_result(tool_name, shrunk, soft_limit, estimator)

    return _hard_cap(tool_name, shrunk, hard_cap, estimator)


def add_condensed_marker(result: dict[str, Any], *, reason: str, full: str) -> dict[str, Any]:
    marker = dict(result.get("_condensed") or {})
    marker.update({"reason": reason, "full": full})
    result["_condensed"] = marker
    return result


def tool_budget(tool_name: str, ctx: Any) -> ToolBudget:
    """读取单工具预算；新配置优先，旧 soft override 兼容兜底。"""
    configured = _configured_tool_budget(tool_name, ctx)
    if configured is not None:
        inline = _coerce_budget_value(
            _budget_field(configured, "inline_budget_tokens"),
            default=_default_inline(ctx),
            minimum=256,
        )
        artifact = _coerce_budget_value(
            _budget_field(configured, "artifact_threshold_tokens"),
            default=inline,
            minimum=256,
        )
        hard_cap = _coerce_budget_value(
            _budget_field(configured, "hard_cap_tokens"),
            default=_default_hard_cap(ctx),
            minimum=512,
        )
        return ToolBudget(
            inline=inline,
            artifact_threshold=artifact,
            hard_cap=max(inline, hard_cap),
        )

    legacy_override = _legacy_soft_override(tool_name, ctx)
    inline = legacy_override if legacy_override is not None else _default_inline(ctx)
    hard_cap = _default_hard_cap(ctx)
    return ToolBudget(
        inline=inline,
        artifact_threshold=inline,
        hard_cap=max(inline, hard_cap),
    )


def _configured_tool_budget(tool_name: str, ctx: Any) -> Any | None:
    budgets = getattr(ctx, "tool_result_budgets", None) or {}
    if not isinstance(budgets, dict):
        return None
    return budgets.get(tool_name)


def _budget_field(value: Any, field: str) -> Any:
    if isinstance(value, dict):
        return value.get(field)
    return getattr(value, field, None)


def _legacy_soft_override(tool_name: str, ctx: Any) -> int | None:
    overrides = getattr(ctx, "tool_result_soft_overrides", None) or {}
    if isinstance(overrides, dict):
        try:
            value = overrides.get(tool_name)
            if value:
                return max(64, int(value))
        except (TypeError, ValueError):
            pass
    return None


def _default_inline(ctx: Any) -> int:
    if _uses_legacy_budget(ctx):
        value = getattr(ctx, "tool_result_soft_limit_tokens", 800)
    else:
        value = getattr(ctx, "tool_result_default_budget_tokens", None)
        if value is None:
            value = getattr(ctx, "tool_result_soft_limit_tokens", 800)
    return _coerce_budget_value(value, default=800, minimum=64)


def _default_hard_cap(ctx: Any) -> int:
    if _uses_legacy_budget(ctx):
        value = getattr(ctx, "tool_result_hard_cap_tokens", 3000)
    else:
        value = getattr(ctx, "tool_result_default_hard_cap_tokens", None)
        if value is None:
            value = getattr(ctx, "tool_result_hard_cap_tokens", 3000)
    return _coerce_budget_value(value, default=3000, minimum=128)


def _uses_legacy_budget(ctx: Any) -> bool:
    budgets = getattr(ctx, "tool_result_budgets", None)
    return isinstance(budgets, dict) and not budgets


def _coerce_budget_value(value: Any, *, default: int, minimum: int) -> int:
    if value is None:
        return max(minimum, default)
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return max(minimum, default)


def _estimate_dict(result: dict[str, Any], estimator: TokenEstimator) -> int:
    return estimator.estimate_text(json.dumps(result, ensure_ascii=False, sort_keys=True))


def _shrink_web_search(
    result: dict[str, Any],
    soft_limit: int,
    estimator: TokenEstimator,
) -> dict[str, Any]:
    value = result.get("result")
    if not isinstance(value, str):
        return result

    if _estimate_dict(result, estimator) <= soft_limit:
        return result

    entries = _split_numbered_results(value)
    kept = entries[:5] if entries else value.splitlines()[:10]
    result["result"] = "\n\n".join(_shrink_search_entry(item) for item in kept)
    if _estimate_dict(result, estimator) > soft_limit and len(kept) > 3:
        kept = kept[:3]
        result["result"] = "\n\n".join(_shrink_search_entry(item) for item in kept)
    return add_condensed_marker(
        result,
        reason="搜索结果过长已保留高位结果摘要",
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


def _shrink_search_entry(entry: str) -> str:
    """保留搜索条目的标题和 URL，只截断摘要。"""
    lines = [line.strip() for line in entry.splitlines() if line.strip()]
    if not lines:
        return entry
    url = next((line for line in reversed(lines) if line.startswith(("http://", "https://"))), "")
    body_lines = lines[1:]
    if url:
        body_lines = [line for line in body_lines if line != url]
    body = _trim_chars(" ".join(body_lines), 120) if body_lines else ""
    parts = [lines[0]]
    if body:
        parts.append(body)
    if url:
        parts.append(url)
    return "\n".join(parts)


def _shrink_contacts(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    for key in ("friends", "groups", "members", "entries"):
        value = result.get(key)
        if isinstance(value, list) and len(value) > 50:
            result[key] = value[:50]
            retry_tool = "list_files" if tool_name == "list_files" else "list_contacts"
            return add_condensed_marker(
                result,
                reason=f"{key} 条目过多，仅保留前 50 条",
                full=f"如需完整列表，请缩小查询范围或重新调用 {retry_tool}。",
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
        if len(lines) <= 2:
            result[field] = _trim_head_tail(value, soft_limit, estimator)
            changed = True
            continue
        head = lines[:50]
        tail = lines[-20:] if len(lines) > 70 else []
        omitted = max(0, len(lines) - len(head) - len(tail))
        middle = [f"...（省略 {omitted} 行）..."] if omitted else []
        result[field] = "\n".join(head + middle + tail)
        if estimator.estimate_text(result[field]) > soft_limit:
            result[field] = _trim_head_tail(result[field], soft_limit, estimator)
        changed = True

    if changed:
        return add_condensed_marker(
            result,
            reason="工具输出过长已保留头尾",
            full="如需完整输出，请让工具把结果写入 workspace 文件后用 read_file 分页查看。",
        )
    return result


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
        "status": result.get("status", "truncated"),
        "tool": tool_name,
        "brief": result.get(
            "brief",
            "工具结果超过硬上限，已拒绝把不完整正文作为完整结果交给模型。",
        ),
    }
    if "artifact" in result:
        compact["artifact"] = result.get("artifact")
    if "path" in result:
        compact["path"] = result.get("path")
    if "count" in result:
        compact["count"] = result.get("count")
    if "data" in result:
        compact["data"] = result.get("data")
    if "next" in result:
        compact["next"] = result.get("next")
    if "artifact" not in compact:
        compact["preview"] = _trim_head_tail(
            json.dumps(result, ensure_ascii=False, sort_keys=True),
            hard_cap,
            estimator,
        )
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
