"""潜意识后台 Agent。

当前实现以程序打分为主，保留 provider/cfg 构造参数，方便后续接入 LLM 评估。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from contextlib import suppress
from typing import Any

from .base import StatusCallback

logger = logging.getLogger(__name__)

_STRONG_EMOTION_WORDS = (
    "救命",
    "崩溃",
    "难受",
    "害怕",
    "气死",
    "痛苦",
    "孤独",
    "想死",
    "焦虑",
    "绝望",
    "震惊",
    "激动",
    "sad",
    "angry",
    "scared",
    "panic",
    "afraid",
    "upset",
    "cry",
)


class SubconsciousAgent:
    """后台合并消息并按程序分数决定是否唤醒人格系统。"""

    def __init__(
        self,
        provider,
        cfg,
        *,
        persona_agent=None,
        wake_callback=None,
        status_callback: StatusCallback | None = None,
    ) -> None:
        self.provider = provider
        self.cfg = cfg
        self.persona_agent = persona_agent
        self.wake_callback = wake_callback
        self.status_callback = status_callback
        self._active = False
        self._state_snapshot: Any = None
        self._trigger_event: Any = None
        self._buffer: list[dict[str, Any]] = []
        self._first_msg_at: float | None = None
        self._timer_task: asyncio.Task[None] | None = None

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self, state_snapshot, trigger_event=None) -> None:
        await self._cancel_timer()
        self._state_snapshot = state_snapshot
        self._trigger_event = trigger_event
        # 潜意识 buffer 只做当前运行期合并；消息持久化由 history/event 层负责。
        self._buffer.clear()
        self._first_msg_at = None
        self._active = True
        self._emit_status("idle", "潜意识监听中")

    async def stop(self) -> None:
        self._active = False
        await self._cancel_timer()
        self._buffer.clear()
        self._trigger_event = None
        self._first_msg_at = None
        self._emit_status("idle", "潜意识已停止")

    async def on_message(self, text, sender_id, profile_affinity) -> None:
        if not self._active:
            return

        now = time.monotonic()
        if not self._buffer:
            self._first_msg_at = now
        self._buffer.append(
            {
                "text": str(text or ""),
                "sender_id": str(sender_id or ""),
                "profile_affinity": _coerce_float(profile_affinity, default=0.0),
                "timestamp": now,
            }
        )

        first_msg_at = self._first_msg_at if self._first_msg_at is not None else now
        if now - first_msg_at >= self._max_window_seconds:
            await self._cancel_timer()
            await self._evaluate_buffer()
            return

        await self._reset_timer()

    @property
    def _merge_window_seconds(self) -> float:
        return max(0.0, _coerce_float(getattr(self.cfg, "merge_window_seconds", 30.0), default=30.0))

    @property
    def _max_window_seconds(self) -> float:
        return max(0.0, _coerce_float(getattr(self.cfg, "max_window_seconds", 300.0), default=300.0))

    @property
    def _min_wake_score(self) -> float:
        return max(0.0, _coerce_float(getattr(self.cfg, "min_wake_score", 0.5), default=0.5))

    async def _reset_timer(self) -> None:
        await self._cancel_timer()
        self._timer_task = asyncio.create_task(self._timer_wait())

    async def _cancel_timer(self) -> None:
        task = self._timer_task
        self._timer_task = None
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _timer_wait(self) -> None:
        await asyncio.sleep(self._merge_window_seconds)
        self._timer_task = None
        if self._active:
            await self._evaluate_buffer()

    async def _evaluate_buffer(self) -> None:
        if not self._buffer:
            return

        buffered_messages = list(self._buffer)
        self._buffer.clear()
        self._first_msg_at = None

        score, reason = self._score_messages(buffered_messages)
        if score < self._min_wake_score:
            self._emit_status("idle", "潜意识继续睡眠")
            return

        self._emit_status("thinking", "潜意识触发唤醒")
        await self._notify_persona_wakeup(reason)
        await self._notify_wake_callback(buffered_messages, reason)
        self._emit_status("idle", "潜意识唤醒完成")

    def _score_messages(self, messages: list[dict[str, Any]]) -> tuple[float, str]:
        joined = "\n".join(str(item.get("text") or "") for item in messages)
        lowered = joined.lower()
        reasons: list[str] = []
        score = 0.0

        matched_keywords = [
            keyword
            for keyword in self._wake_keywords
            if keyword and keyword.lower() in lowered
        ]
        if matched_keywords:
            score += 0.6
            reasons.append("关键词:" + ",".join(matched_keywords[:3]))

        if any(float(item.get("profile_affinity") or 0.0) > 70 for item in messages):
            score += 0.6
            reasons.append("高亲密度")

        matched_emotions = [word for word in _STRONG_EMOTION_WORDS if word.lower() in lowered]
        if matched_emotions:
            score += 0.45
            reasons.append("强情绪:" + ",".join(matched_emotions[:3]))

        if not reasons:
            reasons.append("低优先级消息")
        return min(score, 1.0), "；".join(reasons)

    @property
    def _wake_keywords(self) -> list[str]:
        value = getattr(self.cfg, "wake_keywords", None)
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list | tuple | set):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    async def _notify_persona_wakeup(self, reason: str) -> None:
        if self.persona_agent is None:
            return
        callback = getattr(self.persona_agent, "on_wakeup", None)
        if callback is None:
            return
        try:
            result = callback(reason)
            await _maybe_await(result)
        except Exception:
            logger.debug("通知人格唤醒失败", exc_info=True)

    async def _notify_wake_callback(
        self,
        buffered_messages: list[dict[str, Any]],
        reason: str,
    ) -> None:
        if self.wake_callback is None:
            return
        try:
            result = self.wake_callback(buffered_messages, reason)
            await _maybe_await(result)
        except Exception:
            logger.debug("执行潜意识唤醒回调失败", exc_info=True)

    def _emit_status(self, state: str, text: str) -> None:
        if self.status_callback is None:
            return
        try:
            self.status_callback(
                {
                    "state": state,
                    "text": text,
                    "model": str(getattr(self.cfg, "model", "") or ""),
                    "agent": "潜意识",
                }
            )
        except Exception:
            logger.debug("更新潜意识状态失败", exc_info=True)


def _coerce_float(value: Any, *, default: float) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value
