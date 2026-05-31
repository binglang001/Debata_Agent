"""主动思考定时循环 —— 让 AI 在空闲时主动发消息。

迁移自旧 handler.greeting_loop。流程：
    1. 每 GREETING_INTERVAL 秒触发一次
    2. 若当前有待处理消息（batch 非空），跳过本次
    3. 调 ProactiveRouterAgent 判断是否需要主动行动
    4. 若需要，调主 ChatAgent 跑一轮（通过 pipeline.run_one_turn）

设计：
    - ProactiveRouterAgent 用小模型判断，避免每次都跑 Pro 模型
    - 主动思考期间用 reply_lock 防止与被动消息处理打架
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from types import SimpleNamespace
from typing import Any

from agents import ProactiveRouterAgent, build_messages
from app_config.schema import BehaviorConfig
from tools.result_shrink import shrink_tool_result
from utils import get_time
from utils.token_budget import TokenEstimator

from .message_pipeline import MessagePipeline

logger = logging.getLogger(__name__)

_ROUTER_TOOL_SHRINK_CTX = SimpleNamespace(
    tool_result_soft_limit_tokens=96,
    tool_result_hard_cap_tokens=160,
    tool_result_soft_overrides={},
)
_ROUTER_TEXT_LIMIT_TOKENS = 256
_ROUTER_TOOL_LIMIT_TOKENS = 96
_INTERNAL_ID_KEYS = {
    "msg_id",
    "msg_ids",
    "message_id",
    "message_ids",
    "send_id",
    "forward_id",
    "tool_call_id",
}
_INTERNAL_ID_PATTERN = re.compile(
    r"\s*(?:msg_id|message_id|send_id|forward_id)=[^\s\]】,，]+"
)
_ROLE_LABELS = {
    "user": "用户消息",
    "assistant": "机器人回复",
}
_ROUTER_SUMMARY_DROP_MARKERS = (
    "<send_receipt",
    "</send_receipt>",
    "错误：未调用工具",
    "send_private_messages",
    "send_group_message",
    "no_action",
)
_ROLE_PREFIX_PATTERN = re.compile(r"\[(?:assistant|tool|system)\]\s*", re.IGNORECASE)


def _format_proactive_router_history(records: list[dict[str, Any]]) -> str:
    """把主动路由的小窗口历史折成纯文本，避免 assistant/tool 角色污染路由器。"""
    lines: list[str] = []
    tool_names: dict[str, str] = {}
    for record in records:
        role = str(record.get("role") or "unknown")

        if role == "assistant":
            for tool_call in record.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                call_id = str(tool_call.get("id") or "")
                function = tool_call.get("function")
                name = ""
                if isinstance(function, dict):
                    name = str(function.get("name") or "")
                if call_id and name:
                    tool_names[call_id] = name

        if role == "tool":
            tool_call_id = str(record.get("tool_call_id") or "")
            summary = _summarize_router_tool_result(
                tool_names.get(tool_call_id, "unknown_tool"),
                str(record.get("content") or ""),
            )
            if summary:
                lines.append(f"[内部结果摘要] {summary}")
            continue

        if role == "system":
            continue

        content = str(record.get("content") or "").strip()
        if not content:
            continue

        label = _ROLE_LABELS.get(role, "上下文记录")
        cleaned = _trim_router_text(_clean_router_text(content), _ROUTER_TEXT_LIMIT_TOKENS)
        if cleaned:
            lines.append(f"[{label}] {cleaned}")

    if not lines:
        return ""
    return (
        "<recent_context priority=\"reference\">\n"
        "以下是近期上下文的纯文本摘录，仅用于判断是否需要主动行动：\n"
        + "\n".join(lines)
        + "\n</recent_context>"
    )


def _summarize_router_tool_result(tool_name: str, content: str) -> str:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return _trim_router_text(_clean_router_text(content), _ROUTER_TOOL_LIMIT_TOKENS)

    if not isinstance(parsed, dict):
        return _trim_router_text(
            _clean_router_text(json.dumps(parsed, ensure_ascii=False)),
            _ROUTER_TOOL_LIMIT_TOKENS,
        )

    cleaned = _drop_internal_ids(parsed)
    if isinstance(cleaned, dict):
        shrunk = shrink_tool_result(tool_name, cleaned, _ROUTER_TOOL_SHRINK_CTX)
    else:
        shrunk = cleaned
    return _compact_router_tool_summary(shrunk)


def _compact_router_tool_summary(value: Any) -> str:
    if not isinstance(value, dict):
        return _trim_router_text(
            _clean_router_text(json.dumps(value, ensure_ascii=False)),
            _ROUTER_TOOL_LIMIT_TOKENS,
        )

    parts: list[str] = []
    ok = value.get("ok")
    if ok is False:
        error = _clean_router_text(str(value.get("error") or "执行失败"))
        parts.append(f"失败：{error}")
    elif ok is not None:
        parts.append("成功" if ok else "失败")

    for key in ("summary", "description", "status", "note"):
        field = value.get(key)
        if isinstance(field, str) and field.strip():
            parts.append(_clean_router_text(field))
            break

    condensed = value.get("_condensed")
    if isinstance(condensed, dict) and condensed.get("reason"):
        parts.append(_clean_router_text(str(condensed["reason"])))

    for key, label in (("sent", "已发送"), ("unsent", "未发送"), ("results", "结果")):
        field = value.get(key)
        if isinstance(field, list):
            parts.append(f"{label}{len(field)}条")

    if not parts:
        keys = [
            str(key)
            for key in value.keys()
            if key not in {"ok", "_condensed"} and not str(key).startswith("_")
        ]
        if keys:
            parts.append("字段：" + "、".join(keys[:6]))
        else:
            parts.append("无可用摘要")

    return _trim_router_text("；".join(dict.fromkeys(parts)), _ROUTER_TOOL_LIMIT_TOKENS)


def _drop_internal_ids(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in _INTERNAL_ID_KEYS:
                continue
            result[key] = _drop_internal_ids(item)
        return result
    if isinstance(value, list):
        return [_drop_internal_ids(item) for item in value]
    return value


def _clean_router_text(text: str) -> str:
    return _INTERNAL_ID_PATTERN.sub("", text).strip()


def _clean_router_summary(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        if any(marker in line for marker in _ROUTER_SUMMARY_DROP_MARKERS):
            continue
        cleaned = _ROLE_PREFIX_PATTERN.sub("[上下文] ", line)
        cleaned = _clean_router_text(cleaned)
        if cleaned:
            lines.append(cleaned)
    return _trim_router_text("\n".join(lines), 1024)


def _trim_router_text(text: str, limit_tokens: int) -> str:
    if not text:
        return ""
    estimator = TokenEstimator()
    if estimator.estimate_text(text) <= limit_tokens:
        return text

    marker = "\n...[已截断]...\n"
    marker_cost = estimator.estimate_text(marker)
    if limit_tokens <= marker_cost + 8:
        return text[: max(1, limit_tokens * 2)]

    head_budget = max(1, (limit_tokens - marker_cost) // 2)
    tail_budget = max(1, limit_tokens - marker_cost - head_budget)
    return (
        _fit_router_prefix(text, head_budget, estimator)
        + marker
        + _fit_router_suffix(text, tail_budget, estimator)
    )


def _fit_router_prefix(text: str, limit: int, estimator: TokenEstimator) -> str:
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


def _fit_router_suffix(text: str, limit: int, estimator: TokenEstimator) -> str:
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


class ProactiveLoop:
    """主动思考循环。"""

    def __init__(
        self,
        *,
        pipeline: MessagePipeline,
        proactive_agent: ProactiveRouterAgent | None,
        behavior_cfg: BehaviorConfig,
    ) -> None:
        self.pipeline = pipeline
        self.proactive_agent = proactive_agent
        self.behavior_cfg = behavior_cfg
        self._task: asyncio.Task | None = None
        self._stopping = False

    async def start(self) -> None:
        """启动循环（创建 background task）。"""
        if self.proactive_agent is None:
            logger.info("ProactiveLoop 未启用（proactive agent 未配置）")
            return
        self._task = asyncio.create_task(self._loop())
        logger.info(
            f"ProactiveLoop 已启动，间隔 {self.behavior_cfg.proactive_think_interval_seconds}s"
        )

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                idle = self.pipeline.idle_seconds()
                interval = self.behavior_cfg.proactive_think_interval_seconds
                if idle < interval:
                    await asyncio.sleep(max(0.5, min(interval - idle, 5.0)))
                    continue

                await self._maybe_act()
                await asyncio.sleep(min(interval, 5.0))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"主动思考异常: {e}")

            if self._stopping:
                break

    async def _maybe_act(self) -> None:
        """单次判定 + 行动。

        先用小模型 ProactiveRouterAgent.should_act 判定，True 才跑主 ChatAgent。
        这是主动思考省 token 的关键路径：大部分时段都应该被路由器拦下。
        """
        interval = self.behavior_cfg.proactive_think_interval_seconds
        idle = self.pipeline.idle_seconds()
        if idle < interval:
            logger.debug("主动思考：idle %.1fs < %.1fs，跳过", idle, interval)
            return

        if not self.pipeline.batch.is_empty_unsafe():
            logger.info("主动思考：有待处理消息，跳过")
            return
        if self.pipeline.reply_lock.locked():
            logger.info("主动思考：回复锁忙，跳过")
            return

        await self.pipeline.reply_lock.acquire()
        try:
            # 拿到锁后复检，避免“判断为空→等待锁期间新消息入队”的漂移。
            idle = self.pipeline.idle_seconds()
            if idle < interval:
                logger.debug("主动思考：锁内 idle %.1fs < %.1fs，跳过", idle, interval)
                return
            if not self.pipeline.batch.is_empty_unsafe():
                logger.info("主动思考：锁内发现待处理消息，跳过")
                return

            now = get_time()

            if self.proactive_agent is not None:
                try:
                    router_history = await self.pipeline._select_proactive_router_history()
                    router_context_parts: list[str] = []
                    important_memory = self.pipeline.important.text_for_context(
                        None,
                        token_budget=2048,
                        estimator=self.pipeline._token_estimator(),
                    )
                    if important_memory:
                        router_context_parts.append(
                            '<long_term_memory priority="medium">\n'
                            f"{_clean_router_text(important_memory)}\n"
                            "</long_term_memory>"
                        )
                    rolling_summary = self.pipeline._rolling_summary_text()
                    if rolling_summary:
                        rolling_summary = _clean_router_summary(rolling_summary)
                    if rolling_summary:
                        router_context_parts.append(
                            '<rolling_conversation_summary priority="medium">\n'
                            f"{rolling_summary.strip()}\n"
                            "</rolling_conversation_summary>"
                        )
                    router_history_text = _format_proactive_router_history(router_history)
                    if router_history_text:
                        router_context_parts.append(router_history_text)
                    router_context_parts.append(

                            f"现在是{now}。后台主动思考触发。"
                            "如果最近用户明确要求你在“下次主动思考”时发消息、提醒用户或执行明确操作，"
                            "本轮就是那个时机。"

                    )
                    router_messages = build_messages(
                        persona=self.pipeline.persona,
                        history=[],
                        important_memory_text="",
                        current_context="\n\n".join(router_context_parts),
                        system_override=(
                            "<proactive_router_system priority=\"critical\">\n"
                            "你是后台主动思考路由器，只判断是否需要启动一次主动行动轮。\n"
                            "本调用不执行聊天工具，也不直接回复用户。\n"
                            "</proactive_router_system>"
                        ),
                        memory_mode=self.pipeline.features_cfg.long_term_memory.mode,
                    )
                    needs_action = await self.proactive_agent.should_act(router_messages)
                except Exception as e:
                    logger.exception(f"主动路由判断失败: {e}，本次不主动发言")
                    self.pipeline.mark_activity()
                    return
                if not needs_action:
                    logger.info("主动路由：本次跳过")
                    self.pipeline.mark_activity()
                    return

            task_context = (
                "<proactive_turn priority=\"critical\">\n"
                f"现在是{now}。本轮由系统后台主动思考触发，不是用户刚发来的新消息。\n"
                "请根据近期上下文、重要记忆、滚动摘要和未完成事项判断是否需要主动行动。\n"
                "如果有人要求你在之后、下次空闲或下次主动思考时执行某事，本轮就是可执行时机；"
                "只处理仍未完成且仍有意义的事项。\n"
                "需要联系用户就调用发送工具；没有需要执行的事就调用 no_action。\n"
                "</proactive_turn>"
            )
            await self.pipeline.run_one_turn(task_context, lock_already_held=True)
        finally:
            self.pipeline.reply_lock.release()

    async def shutdown(self) -> None:
        self._stopping = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"ProactiveLoop 停止异常: {e}")
        logger.info("ProactiveLoop 已停止")
