"""人格状态管理 Agent。"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import time
from dataclasses import asdict, replace
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from mind import Cue, DecayEngine, Effect, PersonaState, Todo, UserProfile, clamp_percent
from providers.base import ProviderError, ReasoningConfig

from .base import StatusCallback, UsageRecorder
from .persona.helpers import (
    _ACTION_TODO_EXCLUDE as _ACTION_TODO_EXCLUDE,
)
from .persona.helpers import (
    _ACTION_TODO_INCLUDE_BY_TYPE as _ACTION_TODO_INCLUDE_BY_TYPE,
)
from .persona.helpers import (
    _ACTION_TODO_MEDICINE_EXCLUDE as _ACTION_TODO_MEDICINE_EXCLUDE,
)
from .persona.helpers import (
    _ACTION_TODO_SCOPE_BY_TYPE as _ACTION_TODO_SCOPE_BY_TYPE,
)
from .persona.helpers import (
    _ACTION_TODO_TITLE_EXCLUDE as _ACTION_TODO_TITLE_EXCLUDE,
)
from .persona.helpers import (
    _action_until_expired as _action_until_expired,
)
from .persona.helpers import (
    _append_field_change as _append_field_change,
)
from .persona.helpers import (
    _audit_summary as _audit_summary,
)
from .persona.helpers import (
    _bounded_duration as _bounded_duration,
)
from .persona.helpers import (
    _call_subconscious_starter as _call_subconscious_starter,
)
from .persona.helpers import (
    _call_subconscious_starter_fallback as _call_subconscious_starter_fallback,
)
from .persona.helpers import (
    _coerce_cue as _coerce_cue,
)
from .persona.helpers import (
    _coerce_dataclass as _coerce_dataclass,
)
from .persona.helpers import (
    _coerce_effect as _coerce_effect,
)
from .persona.helpers import (
    _coerce_profile as _coerce_profile,
)
from .persona.helpers import (
    _coerce_state as _coerce_state,
)
from .persona.helpers import (
    _coerce_todo as _coerce_todo,
)
from .persona.helpers import (
    _consolidation_monologue as _consolidation_monologue,
)
from .persona.helpers import (
    _date_key_from_value as _date_key_from_value,
)
from .persona.helpers import (
    _elapsed_hours as _elapsed_hours,
)
from .persona.helpers import (
    _filter_dataclass_fields as _filter_dataclass_fields,
)
from .persona.helpers import (
    _format_action_context as _format_action_context,
)
from .persona.helpers import (
    _format_recent_audits_section as _format_recent_audits_section,
)
from .persona.helpers import (
    _format_relevant_long_term_memory as _format_relevant_long_term_memory,
)
from .persona.helpers import (
    _hour_of_day as _hour_of_day,
)
from .persona.helpers import (
    _hours_since as _hours_since,
)
from .persona.helpers import (
    _infer_user_id as _infer_user_id,
)
from .persona.helpers import (
    _is_substantial_meal as _is_substantial_meal,
)
from .persona.helpers import (
    _iter_records as _iter_records,
)
from .persona.helpers import (
    _maybe_await_call as _maybe_await_call,
)
from .persona.helpers import (
    _meal_satiety_target as _meal_satiety_target,
)
from .persona.helpers import (
    _next_profile_interaction_count as _next_profile_interaction_count,
)
from .persona.helpers import (
    _parse_json_object as _parse_json_object,
)
from .persona.helpers import (
    _participant_display_name as _participant_display_name,
)
from .persona.helpers import (
    _participant_user_id as _participant_user_id,
)
from .persona.helpers import (
    _profile_snapshot as _profile_snapshot,
)
from .persona.helpers import (
    _read_field as _read_field,
)
from .persona.helpers import (
    _read_number as _read_number,
)
from .persona.helpers import (
    _record_date_key as _record_date_key,
)
from .persona.helpers import (
    _record_field as _record_field,
)
from .persona.helpers import (
    _record_sleep_type as _record_sleep_type,
)
from .persona.helpers import (
    _recovery_estimate_payload as _recovery_estimate_payload,
)
from .persona.helpers import (
    _todo_matches_current_action_start as _todo_matches_current_action_start,
)
from .persona.helpers import (
    _user_id_from_conversation as _user_id_from_conversation,
)
from .persona.update_helpers import (
    _CLOSE_OPERATIONS as _CLOSE_OPERATIONS,
)
from .persona.update_helpers import (
    _TODO_CLOSED_STATUS_VALUES as _TODO_CLOSED_STATUS_VALUES,
)
from .persona.update_helpers import (
    _TODO_OPEN_STATUS_VALUES as _TODO_OPEN_STATUS_VALUES,
)
from .persona.update_helpers import (
    _UPDATE_OPERATIONS as _UPDATE_OPERATIONS,
)
from .persona.update_helpers import (
    _coerce_level_value as _coerce_level_value,
)
from .persona.update_helpers import (
    _coerce_model_timestamp as _coerce_model_timestamp,
)
from .persona.update_helpers import (
    _cue_from_update as _cue_from_update,
)
from .persona.update_helpers import (
    _cue_patch_text as _cue_patch_text,
)
from .persona.update_helpers import (
    _effect_from_update as _effect_from_update,
)
from .persona.update_helpers import (
    _effect_patch_text as _effect_patch_text,
)
from .persona.update_helpers import (
    _int_sort_value as _int_sort_value,
)
from .persona.update_helpers import (
    _normalize_operation as _normalize_operation,
)
from .persona.update_helpers import (
    _normalize_todo_status as _normalize_todo_status,
)
from .persona.update_helpers import (
    _operation_requires_existing as _operation_requires_existing,
)
from .persona.update_helpers import (
    _optional_float as _optional_float,
)
from .persona.update_helpers import (
    _time_sort_value as _time_sort_value,
)
from .persona.update_helpers import (
    _todo_dedupe_key as _todo_dedupe_key,
)
from .persona.update_helpers import (
    _todo_from_update as _todo_from_update,
)
from .persona.update_helpers import (
    _todo_is_expired as _todo_is_expired,
)
from .persona.update_helpers import (
    _todo_patch_text as _todo_patch_text,
)
from .persona.update_helpers import (
    _todo_record_from_update as _todo_record_from_update,
)
from .persona.update_helpers import (
    _todo_record_is_closed as _todo_record_is_closed,
)
from .persona.update_helpers import (
    _todo_sort_key as _todo_sort_key,
)
from .persona.update_helpers import (
    _todo_update_requires_existing as _todo_update_requires_existing,
)
from .persona.update_helpers import (
    _todo_update_status as _todo_update_status,
)
from .persona.update_helpers import (
    _truthy_todo_state as _truthy_todo_state,
)
from .persona.update_models import (
    _append_singular as _append_singular,
)
from .persona.update_models import (
    _CueUpdate as _CueUpdate,
)
from .persona.update_models import (
    _EffectUpdate as _EffectUpdate,
)
from .persona.update_models import (
    _normalize_traits_field as _normalize_traits_field,
)
from .persona.update_models import (
    _normalize_update_list_field as _normalize_update_list_field,
)
from .persona.update_models import (
    _ProfileUpdate as _ProfileUpdate,
)
from .persona.update_models import (
    _RecoveryEstimate as _RecoveryEstimate,
)
from .persona.update_models import (
    _RecoveryResult as _RecoveryResult,
)
from .persona.update_models import (
    _RelationshipUpdate as _RelationshipUpdate,
)
from .persona.update_models import (
    _TodoUpdate as _TodoUpdate,
)
from .persona.update_models import (
    _TurnUpdate as _TurnUpdate,
)
from .persona_context_view import PersonaContextView

logger = logging.getLogger(__name__)

_RESTING_ACTIONS = {"sleeping", "eating", "collapsing"}

_SOCIAL_NEED_GUIDANCE = (
    "social_need 表示社交未满足度，不是亲密度、兴奋度或想继续聊天的程度："
    "高=缺社交、孤独、需要陪伴；低=当前社交被满足。"
    "数值锚点：0-5 非常罕见，只在刚完成高质量陪伴、当前确实不需要继续互动、"
    "且没有悬而未决的社交期待时使用；10-25 是被回应、被关心、聊天满足后的常见低位；"
    "30-60 是普通稳定区间；70+ 表示孤独、被忽视或长时间缺少互动。"
    "单轮普通亲密互动通常只小幅下降，避免连续把 social_need 压到 0；"
    "想继续聊、害羞、上头、亲近余韵不要挤到 social_need，应使用 effects/relationship 表达。\n"
)

_SHORT_TERM_UPDATE_GUIDANCE = (
    "effects[] 用于会持续一段时间的临时情绪、身体感、语气倾向、行动倾向或关系余韵；"
    "当本轮互动留下明显短期余韵时，应创建或更新 effect，而不是只写进 latest_monologue。"
    "例如被反复关心后的柔软/害羞、晚安后的温暖余韵、被误解后的防备、"
    "想暂时收尾休息的倾向。已有同类短期影响时优先用真实 id update 或 close；"
    "不要把短期余韵塞进长期 profile。\n"
    "cues[] 用于对当前会话或近期互动有用、但不适合进长期画像、也不构成可执行待办的短期线索；"
    "当对方正在引导、某个话题还没完全收束、近期需要接续某个聊天意图时，应创建或更新 cue，"
    "不要因为它不是长期记忆就丢掉。\n"
    "todos[] 用于之后需要执行、检查、提醒或收尾的具体事项；"
    "当用户明确要求角色之后做某事，且角色答应、接受、或当前生理状态确实需要时，应创建 todo。"
    "例如用户说“你一定要去睡哦”且角色接受时，可建“准备睡觉/尽快入睡”类 todo。"
    "todo 必须有可执行标题、scope、priority、触发或过期信息；避免“关注/记得/继续观察/留意一下”"
    "这类泛泛标题。\n"
)


class PersonaAgent:
    """维护人格运行时状态、短期效果和后台线索。"""

    def __init__(
        self,
        db: Any,
        provider: Any,
        cfg: Any,
        pm_cfg: Any,
        age_profile: Any,
        decay: DecayEngine,
        consolidation: Any,
        persona: Any,
        *,
        usage_recorder: UsageRecorder | None = None,
        status_callback: StatusCallback | None = None,
        subconscious_starter: Any = None,
    ) -> None:
        self.db = db
        self.provider = provider
        self.cfg = cfg
        self.pm_cfg = pm_cfg
        self.age_profile = age_profile
        self.decay = decay
        self.consolidation = consolidation
        self.persona = persona
        self.usage_recorder = usage_recorder
        self.status_callback = status_callback
        self.subconscious_starter = subconscious_starter

        self._state = PersonaState()
        self._effects: list[Effect] = []
        self._todos: list[Todo] = []
        self._cues: list[Cue] = []
        self._profiles: dict[str, UserProfile] = {}
        self._context_view = PersonaContextView()
        self._state_lock = asyncio.Lock()
        self._timer_task: asyncio.Task[None] | None = None
        self._active_sleep_record_id: str | None = None
        self._active_sleep_record: dict[str, Any] | None = None
        self._active_eat_record: dict[str, Any] | None = None
        self._last_daily_consolidation_date: str | None = None

    async def start(self) -> None:
        """加载人格状态并启动定时维护任务。"""

        async with self._state_lock:
            await _maybe_await_call(self.db, "load")
            loaded = await _maybe_await_call(self.db, "get_state", default=None)
            self._state = _coerce_state(loaded)
            self._active_sleep_record_id = self._state.active_sleep_record_id
            now = time.time()
            await self._mark_expired_todos_missed_locked(now)
            self._effects = await self._load_effects(now)
            self._todos = await self._load_todos()
            self._cues = await self._load_cues(now)
            self._profiles = await self._load_profiles()
            startup_previous = replace(self._state)
            await self._reconcile_physiology_locked(
                now,
                recovery_source_hint="offline_reconcile",
            )
            await self._append_startup_reconcile_log_locked(startup_previous, now)
            await self._complete_active_resting_action_todos_locked(now)
            await self._save_state_locked()

        if self._timer_task is None or self._timer_task.done():
            self._timer_task = asyncio.create_task(self._timer_loop())

    async def shutdown(self) -> None:
        """停止定时任务并保存最后的人格状态。"""

        task = self._timer_task
        self._timer_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        async with self._state_lock:
            await self._save_state_locked()

    def get_state_snapshot(self) -> PersonaState:
        """返回当前人格状态的同步拷贝。"""

        return replace(self._state)

    def get_context_for_chat(self, conversation_id: str) -> str:
        """构造注入聊天 Agent 的人格状态上下文。"""

        state = self.get_state_snapshot()
        lines = [
            "<人格状态>",
            f"- 心情: {state.mood:.1f}/100",
            f"- 社交需求: {state.social_need:.1f}/100",
        ]
        if self._energy_mode() == "tool":
            energy_line = f"- 精力: {state.energy:.1f}/100"
            if state.energy <= 30:
                energy_line += "。精力偏低，可能会累。"
            lines.append(energy_line)
        if self._satiety_mode() == "tool":
            satiety_line = f"- 饱腹: {state.satiety:.1f}/100"
            if state.satiety <= 30:
                satiety_line += "。饱腹偏低，可能会饿。"
            lines.append(satiety_line)
        if self.age_profile is not None:
            lines.extend(self._format_age_lines())
        action_context = _format_action_context(state, time.time())
        if action_context:
            lines.append(action_context)
        if state.latest_monologue:
            lines.append(f"- 最近内心独白: {state.latest_monologue}")

        hints = [effect.prompt_hint for effect in self._effects if effect.prompt_hint]
        if hints:
            lines.append("- 当前短期影响: " + "；".join(hints[:5]))

        profile = self._profile_for_conversation(conversation_id)
        if profile is not None:
            profile_text = profile.summary or profile.display_name
            if profile_text:
                lines.append(f"- 当前对象画像: {profile_text}")

        open_todos = self.get_todos_for_proactive()
        if open_todos:
            lines.append("- 待办: " + "；".join(todo.title for todo in open_todos[:5]))

        cues = self.get_cues()
        if cues:
            lines.append("- 线索: " + "；".join(cue.summary for cue in cues[:5]))
        lines.append("</人格状态>")
        return "\n".join(lines)

    def get_todos_for_proactive(self) -> list[Todo]:
        """返回可供主动行为消费的待办拷贝。"""

        now = time.time()
        self._drop_expired_todo_cache(now)
        open_todos = list(self._todos)
        return [replace(todo) for todo in sorted(open_todos, key=_todo_sort_key)]

    def get_cues(self) -> list[Cue]:
        """返回当前有效线索拷贝。"""

        return [replace(cue) for cue in self._cues]

    def is_resting(self) -> bool:
        if self._state.current_action not in _RESTING_ACTIONS:
            return False
        action_until = _optional_float(self._state.action_until)
        return action_until is not None and action_until > time.time()

    @property
    def physiology_energy_mode(self) -> str:
        return self._energy_mode()

    @property
    def physiology_satiety_mode(self) -> str:
        return self._satiety_mode()

    async def after_turn(
        self,
        conversation_id: str,
        participants: Any,
        chat_summary: str,
        eat_event: Any = None,
    ) -> None:
        """在一轮对话后异步维护人格状态。调用方可 fire-and-forget。"""

        fallback = False
        try:
            async with self._state_lock:
                if self.is_resting():
                    await self._append_state_log_locked(
                        {
                            "event": "after_turn",
                            "conversation_id": conversation_id,
                            "skipped": True,
                            "skip_reason": "persona_resting",
                        }
                    )
                    await self._save_state_locked()
                    self._emit_status("idle", "人格休息中，跳过普通状态更新")
                    return
                update = await self._llm_turn_update(
                    conversation_id,
                    participants,
                    chat_summary,
                    eat_event,
                )
                fallback = update is None
                if update is None:
                    update = _TurnUpdate()
                await self._apply_turn_update_locked(
                    update,
                    conversation_id,
                    participants,
                )
                await self._append_state_log_locked(
                    {
                        "event": "after_turn",
                        "conversation_id": conversation_id,
                        "fallback": fallback,
                    }
                )
                await self._save_state_locked()
            self._emit_status(
                "idle",
                "人格状态更新跳过" if fallback else "人格状态更新完成",
            )
        except Exception:
            logger.exception("人格管理 after_turn 异常")

    async def periodic_tick(self) -> None:
        """推进定时衰减、过期清理和兜底整理。"""

        fallback = False
        try:
            async with self._state_lock:
                now = time.time()
                await self._expire_runtime_records_locked(now)
                await self._mark_expired_todos_missed_locked(now)
                mood_previous_tick_at = self._state.last_tick_at
                finished_action = await self._reconcile_physiology_locked(now)
                update = await self._llm_periodic_update(now)
                fallback = update is None
                if update is None:
                    update = _TurnUpdate()
                await self._apply_turn_update_locked(
                    update,
                    "system:periodic",
                    [],
                    touch_profile=False,
                    trigger="periodic_tick",
                )
                if (
                    finished_action is None
                    and self._state.current_action == "awake"
                    and update.mood is None
                    and update.mood_delta is None
                ):
                    self._apply_mood_baseline_drift_locked(now, mood_previous_tick_at)
                await self._maybe_daily_consolidation(now)
                await self._append_state_log_locked(
                    {
                        "event": "periodic_tick",
                        "fallback": fallback,
                    }
                )
                await self._save_state_locked()
            self._emit_status(
                "idle",
                "人格定时维护跳过" if fallback else "人格定时维护完成",
            )
        except Exception:
            logger.exception("人格管理定时维护异常")
            self._emit_status("idle", "人格定时维护异常")

    async def on_sleep_start(self, duration_minutes: float) -> dict[str, Any]:
        """记录开始睡眠。仅精力工具模式生效。"""

        if self._energy_mode() != "tool":
            return {"status": "disabled"}

        now = time.time()
        duration = _bounded_duration(
            duration_minutes,
            _read_number(self.pm_cfg, "physiology", "energy", "max_sleep_minutes", default=720),
        )
        action_until = now + duration * 60.0
        record = {
            "id": f"sleep_{uuid4().hex}",
            "started_at": now,
            "planned_duration_minutes": duration,
            "action_until": action_until,
            "status": "active",
        }
        async with self._state_lock:
            await self._settle_current_action_for_replacement_locked(now)
            self._state.current_action = "sleeping"
            self._state.action_until = action_until
            self._state.last_sleep_at = now
            self._active_sleep_record_id = await _maybe_await_call(
                self.db,
                "add_sleep_record",
                record,
                default=record["id"],
            )
            self._active_sleep_record = dict(record)
            self._state.active_sleep_record_id = self._active_sleep_record_id
            self._state.active_eat_record_id = None
            self._active_eat_record = None
            await self._complete_current_action_todos_locked("sleep", now)
            await self._append_state_log_locked({"event": "sleep_start", **record})
            await self._save_state_locked()

        threshold = _read_number(
            self.pm_cfg,
            "physiology",
            "energy",
            "long_sleep_threshold_minutes",
            default=120,
        )
        if duration >= threshold:
            current_monologue = self._state.latest_monologue
            current_monologue_at = self._state.last_monologue_at
            consolidation_result = await self._run_consolidation("long_sleep")
            await self._restore_current_monologue_after_sleep_consolidation(
                current_monologue,
                current_monologue_at,
                consolidation_result,
            )
            await self._start_subconscious(
                {
                    "event": "sleep_start",
                    "sleep_type": "long_sleep",
                    "duration_minutes": duration,
                }
            )

        return {
            "status": "started",
            "duration_minutes": duration,
            "action_until": action_until,
            "record_id": self._active_sleep_record_id,
        }

    async def on_wakeup(self, reason: str) -> PersonaState:
        """从睡眠或休息状态醒来，并按实际睡眠时间恢复基础精力。"""

        now = time.time()
        async with self._state_lock:
            state = self._state
            await self._reconcile_physiology_locked(now, allow_collapse=False)
            previous_action = state.current_action
            state.current_action = "awake"
            state.action_until = None
            state.last_tick_at = now
            await self._finish_active_record_locked(
                previous_action,
                now,
                {"wakeup_reason": reason},
            )
            await self._append_state_log_locked(
                {"event": "wakeup", "reason": reason, "previous_action": previous_action}
            )
            await self._save_state_locked()
            return replace(state)

    async def on_eat_start(
        self,
        meal_type: str,
        duration_minutes: float,
        description: str,
    ) -> dict[str, Any]:
        """记录开始进食。仅饱腹工具模式生效。"""

        if self._satiety_mode() != "tool":
            return {"status": "disabled"}

        now = time.time()
        duration = _bounded_duration(
            duration_minutes,
            _read_number(self.pm_cfg, "physiology", "satiety", "max_eat_minutes", default=60),
        )
        action_until = now + duration * 60.0
        record = {
            "id": f"eat_{uuid4().hex}",
            "meal_type": str(meal_type or ""),
            "description": str(description or ""),
            "started_at": now,
            "duration_minutes": duration,
            "action_until": action_until,
            "status": "active",
        }
        async with self._state_lock:
            await self._settle_current_action_for_replacement_locked(now)
            self._state.current_action = "eating"
            self._state.action_until = action_until
            self._state.last_eat_at = now
            self._state.active_sleep_record_id = None
            self._active_sleep_record = None
            self._state.active_eat_record_id = record["id"]
            self._active_eat_record = dict(record)
            row_id = await _maybe_await_call(
                self.db,
                "add_eat_record",
                record,
                default=None,
            )
            await self._append_state_log_locked({"event": "eat_start", **record})
            await self._complete_current_action_todos_locked("eat", now)
            await self._save_state_locked()

        return {
            "status": "started",
            "duration_minutes": duration,
            "action_until": action_until,
            "record_id": record["id"],
            "row_id": row_id,
        }

    async def trigger_collapse(self) -> dict[str, Any]:
        """立即触发精力耗尽昏睡。"""

        if self._energy_mode() != "tool":
            return {"status": "disabled"}
        async with self._state_lock:
            now = time.time()
            await self._settle_current_action_for_replacement_locked(now)
            self._trigger_collapse_locked(now)
            await self._append_state_log_locked({"event": "collapse"})
            await self._save_state_locked()
            return {
                "status": "collapsing",
                "action_until": self._state.action_until,
            }

    async def _timer_loop(self) -> None:
        interval_seconds = max(
            1.0,
            _read_number(
                self.pm_cfg,
                "persona_agent",
                "timer_interval_minutes",
                default=30,
            )
            * 60.0,
        )
        while True:
            await asyncio.sleep(interval_seconds)
            await self.periodic_tick()

    async def _llm_turn_update(
        self,
        conversation_id: str,
        participants: Any,
        chat_summary: str,
        eat_event: Any,
    ) -> _TurnUpdate | None:
        for attempt in range(2):
            try:
                self._emit_status("thinking", "人格状态更新中")
                messages = await self._build_turn_messages(
                    conversation_id,
                    participants,
                    chat_summary,
                    eat_event,
                    retry=attempt > 0,
                )
                result = await self.provider.chat_completion(
                    messages,
                    model=self.cfg.model,
                    tools=None,
                    temperature=self.cfg.temperature,
                    top_p=self.cfg.top_p,
                    max_tokens=self.cfg.max_tokens,
                    reasoning=self._to_provider_reasoning(),
                    stream=True,
                    timeout=self.cfg.first_token_timeout_seconds * 6 + 60.0,
                    first_token_timeout=self.cfg.first_token_timeout_seconds * 2,
                )
                await self._record_usage(
                    _read_field(result, "usage"),
                    operation="persona_after_turn",
                )
            except ProviderError as exc:
                logger.warning("人格状态更新模型调用失败: %s", exc)
                self._emit_status("error", "人格状态更新失败")
                continue
            except Exception:
                logger.exception("人格状态更新模型调用异常")
                self._emit_status("error", "人格状态更新异常")
                continue

            parsed = _parse_json_object(str(_read_field(result, "content") or ""))
            if parsed is None:
                logger.warning("人格状态更新返回无有效 JSON")
                continue
            try:
                return _TurnUpdate.model_validate(parsed)
            except ValidationError:
                logger.warning("人格状态更新 JSON 校验失败", exc_info=True)
                continue
        return None

    async def _llm_periodic_update(self, now: float) -> _TurnUpdate | None:
        for attempt in range(2):
            try:
                self._emit_status("thinking", "人格定时维护中")
                result = await self.provider.chat_completion(
                    self._build_periodic_messages(now, retry=attempt > 0),
                    model=self.cfg.model,
                    tools=None,
                    temperature=self.cfg.temperature,
                    top_p=self.cfg.top_p,
                    max_tokens=self.cfg.max_tokens,
                    reasoning=self._to_provider_reasoning(),
                    stream=True,
                    timeout=self.cfg.first_token_timeout_seconds * 6 + 60.0,
                    first_token_timeout=self.cfg.first_token_timeout_seconds * 2,
                )
                await self._record_usage(
                    _read_field(result, "usage"),
                    operation="persona_periodic",
                )
            except ProviderError as exc:
                logger.warning("人格定时维护模型调用失败: %s", exc)
                self._emit_status("error", "人格定时维护失败")
                continue
            except Exception:
                logger.exception("人格定时维护模型调用异常")
                self._emit_status("error", "人格定时维护异常")
                continue

            parsed = _parse_json_object(str(_read_field(result, "content") or ""))
            if parsed is None:
                logger.warning("人格定时维护返回无有效 JSON")
                continue
            try:
                return _TurnUpdate.model_validate(parsed)
            except ValidationError:
                logger.warning("人格定时维护 JSON 校验失败", exc_info=True)
                continue
        return None

    async def _llm_recovery_estimate(
        self,
        action: str,
        ended_at: float,
        extra_updates: dict[str, Any],
    ) -> _RecoveryEstimate | None:
        for attempt in range(2):
            try:
                self._emit_status("thinking", "人格恢复评估中")
                result = await self.provider.chat_completion(
                    self._build_recovery_messages(
                        action,
                        ended_at,
                        extra_updates,
                        retry=attempt > 0,
                    ),
                    model=self.cfg.model,
                    tools=None,
                    temperature=self.cfg.temperature,
                    top_p=self.cfg.top_p,
                    max_tokens=self.cfg.max_tokens,
                    reasoning=self._to_provider_reasoning(),
                    stream=True,
                    timeout=self.cfg.first_token_timeout_seconds * 6 + 60.0,
                    first_token_timeout=self.cfg.first_token_timeout_seconds * 2,
                )
                await self._record_usage(
                    _read_field(result, "usage"),
                    operation="persona_recovery_eval",
                    action=action,
                )
            except ProviderError as exc:
                logger.warning("人格恢复评估模型调用失败: %s", exc)
                self._emit_status("error", "人格恢复评估失败")
                continue
            except Exception:
                logger.exception("人格恢复评估模型调用异常")
                self._emit_status("error", "人格恢复评估异常")
                continue

            parsed = _parse_json_object(str(_read_field(result, "content") or ""))
            if parsed is None:
                logger.warning("人格恢复评估返回无有效 JSON")
                continue
            try:
                return _RecoveryEstimate.model_validate(parsed)
            except ValidationError:
                logger.warning("人格恢复评估 JSON 校验失败", exc_info=True)
                continue
        return None

    def _build_recovery_messages(
        self,
        action: str,
        ended_at: float,
        extra_updates: dict[str, Any],
        *,
        retry: bool,
    ) -> list[dict[str, Any]]:
        retry_text = "上一次输出无法解析，请只返回一个 JSON 对象。" if retry else ""
        prompt = (
            "请评估角色完成一次进食、睡眠或昏睡恢复后的状态。\n"
            "只返回 JSON 对象，不要解释。可用字段："
            "energy、satiety、mood、social_need、latest_monologue、reason。\n"
            "energy、satiety、mood、social_need 都是 0-100 的目标值；"
            f"{_SOCIAL_NEED_GUIDANCE}"
            "只在你能根据时长、动作和当前状态判断时填写。"
            "不确定的字段省略，系统会保留公式兜底结果。\n"
            "latest_monologue 写醒来或吃完后的短句内心状态；不要编造外部事件。\n"
            f"{retry_text}\n\n"
            f"<action>{action}</action>\n"
            f"<ended_at>{ended_at}</ended_at>\n"
            f"<finish_updates>{json.dumps(extra_updates, ensure_ascii=False, default=str)}</finish_updates>\n"
            f"<当前状态>\n{json.dumps(asdict(self._state), ensure_ascii=False)}\n</当前状态>"
        )
        return [
            {
                "role": "system",
                "content": "你是当前角色的生理恢复评估系统，只评估恢复后的状态。",
            },
            {"role": "user", "content": prompt},
        ]

    def _build_periodic_messages(self, now: float, *, retry: bool) -> list[dict[str, Any]]:
        retry_text = "上一次输出无法解析，请只返回一个 JSON 对象。" if retry else ""
        prompt = (
            "请根据当前人格状态做一次后台定时维护。\n"
            "这不是用户新消息，不要编造新的外部事件；只处理自然状态变化、过期线索、必要待办和简短内心独白。\n"
            "只返回 JSON 对象，不要解释。可用字段同 after_turn："
            "mood、mood_delta、social_need、social_need_delta、latest_monologue、effects、profiles、relationships、todos、cues。\n"
            "effects、profiles、relationships、todos、cues 必须是 JSON 数组；没有内容用 []，不要用 {}、null 或空字符串。\n"
            "profiles[].traits 和 relationships[].traits 必须是 JSON 字符串数组；没有内容用 []，不要用逗号分隔字符串。\n"
            f"{_SOCIAL_NEED_GUIDANCE}"
            "如果没有明确变化，返回空对象 {}。\n"
            "latest_monologue 只写此刻内心或身体状态；不要写成刚收到用户消息。\n"
            f"{_SHORT_TERM_UPDATE_GUIDANCE}"
            "effects[] 需要 name/effect_type/intensity/prompt_hint/source_detail/"
            "duration_minutes 或 expires_at；intensity 必须是 0-100 数字，不要写 low/medium/high。\n"
            "todos[] 需要 title，可选 reason/priority/scope/expires_at；已有 todo 可用 id 加 status/completed/done/cancelled 标记完成或取消。"
            "cues[] 需要 summary，可选 cue_type/conversation_id/expires_at。"
            "priority 必须是 0-10 整数；时间必须是 unix timestamp 数字；已有项操作必须带上下文里出现过的真实 id。\n"
            f"{retry_text}\n\n"
            f"<now>{now}</now>\n"
            f"<当前状态>\n{json.dumps(asdict(self._state), ensure_ascii=False)}\n</当前状态>\n"
            f"<待办>\n{json.dumps([asdict(todo) for todo in self._todos], ensure_ascii=False, default=str)}\n</待办>\n"
            f"<线索>\n{json.dumps([asdict(cue) for cue in self._cues], ensure_ascii=False, default=str)}\n</线索>\n"
            f"<短期影响>\n{json.dumps([asdict(effect) for effect in self._effects], ensure_ascii=False, default=str)}\n</短期影响>"
        )
        return [
            {
                "role": "system",
                "content": "你是当前角色的人格定时维护系统，只维护后台状态，不进行聊天。",
            },
            {"role": "user", "content": prompt},
        ]

    async def _build_turn_messages(
        self,
        conversation_id: str,
        participants: Any,
        chat_summary: str,
        eat_event: Any,
        *,
        retry: bool,
    ) -> list[dict[str, Any]]:
        now = time.time()
        inferred_user_id = _infer_user_id(conversation_id, participants)
        profile = self._profiles.get(inferred_user_id or "") if inferred_user_id else None
        recent_audits = await self._recent_update_audits_for_context(
            conversation_id,
            inferred_user_id,
        )
        long_term_memory_text = await self._long_term_memory_text_for_context(
            conversation_id,
            inferred_user_id,
        )
        context_text = self._context_view.build_text(
            {
                "event": {
                    "trigger_type": "after_turn",
                    "conversation_id": conversation_id,
                    "turn_new": chat_summary or "",
                    "summary": chat_summary or "",
                    "participants": participants,
                    "eat_event": eat_event,
                },
                "state": asdict(self._state),
                "profile": profile,
                "profile_audits": recent_audits,
                "effects": [asdict(effect) for effect in self._effects],
                "cues": [asdict(cue) for cue in self._cues],
                "todos": [asdict(todo) for todo in self._todos],
                "long_term_memory_text": long_term_memory_text,
            },
            long_term_memory_text=long_term_memory_text,
            now=now,
            append_episode=not retry,
        )
        audit_section = _format_recent_audits_section(recent_audits)
        if audit_section:
            context_text = f"{context_text}\n\n{audit_section}"
        retry_text = "上一次输出无法解析，请只返回一个 JSON 对象。" if retry else ""
        prompt = (
            "请根据上下文对角色人格运行状态做增量维护。\n"
            "只返回 JSON 对象，不要解释。\n"
            "可用字段：mood、mood_delta、social_need、social_need_delta、"
            "latest_monologue、effects、profile/profiles、relationship/relationships、todos、cues。\n"
            "effects、profiles、relationships、todos、cues 必须是 JSON 数组；没有内容用 []，不要用 {}、null 或空字符串。\n"
            "profiles[].traits 和 relationships[].traits 必须是 JSON 字符串数组；没有内容用 []，不要用逗号分隔字符串。\n"
            "兼容字段 profile、relationship、effect、todo、cue 只用于单条更新；复数字段始终返回数组。\n"
            "mood、social_need、affinity 都是 0-100 分；delta 是在当前值上的增减。\n"
            f"{_SOCIAL_NEED_GUIDANCE}"
            "归因规则：助手、assistant、当前回复、角色刚说的话，都是当前人格自己的发言；"
            "只有用户消息或明确外部事件才算别人输入。\n"
            "latest_monologue 必须是一人称内心状态，只写我此刻的感受、念头或身体状态；"
            "不得把助手刚说的话改写成用户、对方或外部人物的经历。\n"
            f"{_SHORT_TERM_UPDATE_GUIDANCE}"
            "effects[] 需要 name/effect_type/intensity/prompt_hint/source_detail/"
            "duration_minutes 或 expires_at；intensity 必须是 0-100 数字，不要写 low/medium/high。\n"
            "已有项操作必须带上下文里出现过的真实 id，不能编造 id；unknown id 不会创建新项。\n"
            "operation 可用 create/update/close/delete/cancel/complete/noop；"
            "close/delete/cancel/complete 表示关闭或移除已有项，noop 表示不改。\n"
            "当用户透露稳定偏好、称呼、长期习惯、关系变化、新印象或可复用的相处信息时，使用 profile/profiles 实时更新用户画像；"
            "profile 字段包括 user_id、display_name、summary、traits、affinity。私聊可省略 user_id 由系统推断，群聊必须带 user_id；"
            "affinity 是 0-100 的绝对亲近度，不是 0-1，也不是 1-10；"
            "刻度锚点：0=陌生/排斥，30=疏离，50=普通熟人，70=信任友好，85=亲近在意，95=核心亲密关系。"
            "profile 事实仍不要塞短期情绪、一次性事件或临时状态。\n"
            "当一轮互动让关系变好/变坏、有新印象、亲近度变化时，使用 relationship/relationships；"
            "relationship 可含 user_id、display_name、summary、traits、affinity 或 affinity_delta、reason。"
            "relationship/affinity_delta 是本轮增减分，普通一轮互动优先使用 affinity_delta，优先用 affinity_delta；普通一轮互动通常 -5 到 +5；强烈事件可更大，但要写 reason。"
            "绝对 affinity 只用于首次建档或明确校准；若已有关系只是本轮变好/变坏，不要随意用低 absolute affinity 覆盖。"
            "这类关系更新不要求同时写长期画像事实；私聊可省略 user_id 由系统推断。\n"
            "todos[] 需要 title，可选 reason/priority/scope/expires_at；已有 todo 可用 id 加 status/completed/done/cancelled 标记完成或取消；priority 必须是 0-10 整数，不要写 low/medium/high。\n"
            "cues[] 需要 summary，可选 cue_type/conversation_id/expires_at。\n"
            f"{retry_text}\n\n"
            f"{context_text}"
        )
        return [
            {
                "role": "system",
                "content": "你是当前角色的人格管理系统，负责维护状态和短期记忆。",
            },
            {"role": "user", "content": prompt},
        ]

    async def _apply_turn_update_locked(
        self,
        update: _TurnUpdate,
        conversation_id: str,
        participants: Any,
        *,
        touch_profile: bool = True,
        trigger: str = "after_turn",
    ) -> None:
        now = time.time()
        state = self._state
        inferred_user_id = _infer_user_id(conversation_id, participants)
        state_before = asdict(state)
        profile_before = _profile_snapshot(
            self._profiles.get(inferred_user_id or "") if inferred_user_id else None
        )
        applied_changes: dict[str, list[dict[str, Any]]] = {
            "state": [],
            "profiles": [],
            "effects": [],
            "todos": [],
            "cues": [],
        }

        if update.mood is not None:
            before = state.mood
            state.mood = clamp_percent(update.mood)
            _append_field_change(
                applied_changes["state"],
                "mood",
                before,
                state.mood,
                source="absolute",
            )
        if update.mood_delta is not None:
            before = state.mood
            state.mood = clamp_percent(state.mood + update.mood_delta)
            _append_field_change(
                applied_changes["state"],
                "mood",
                before,
                state.mood,
                source="delta",
                delta=update.mood_delta,
            )
        if update.social_need is not None:
            before = state.social_need
            state.social_need = clamp_percent(update.social_need)
            _append_field_change(
                applied_changes["state"],
                "social_need",
                before,
                state.social_need,
                source="absolute",
            )
        if update.social_need_delta is not None:
            before = state.social_need
            state.social_need = clamp_percent(state.social_need + update.social_need_delta)
            _append_field_change(
                applied_changes["state"],
                "social_need",
                before,
                state.social_need,
                source="delta",
                delta=update.social_need_delta,
            )
        if update.latest_monologue:
            before = state.latest_monologue
            state.latest_monologue = update.latest_monologue.strip()
            state.last_monologue_at = now
            _append_field_change(
                applied_changes["state"],
                "latest_monologue",
                before,
                state.latest_monologue,
                source="text",
            )
            await _maybe_await_call(
                self.db,
                "add_monologue",
                {"text": state.latest_monologue, "created_at": now},
            )

        for effect_update in update.effects:
            operation = effect_update.operation
            existing = self._find_effect(effect_update.id)
            if operation == "noop":
                applied_changes["effects"].append(
                    {"operation": "noop", "id": effect_update.id or ""}
                )
                continue
            if _operation_requires_existing(operation) and not effect_update.id:
                applied_changes["effects"].append(
                    {
                        "operation": "dropped_missing_id",
                        "requested_operation": operation,
                    }
                )
                continue
            if effect_update.id and existing is None:
                applied_changes["effects"].append(
                    {
                        "operation": "dropped_unknown_id",
                        "requested_operation": operation,
                        "id": effect_update.id,
                    }
                )
                continue
            if existing is not None and operation in _CLOSE_OPERATIONS:
                self._remove_effect_cache(existing.id)
                await _maybe_await_call(self.db, "remove_effects", [existing.id], default=0)
                applied_changes["effects"].append(
                    {
                        "operation": operation,
                        "id": existing.id,
                        "before": asdict(existing),
                        "after": None,
                    }
                )
                continue
            effect = _effect_from_update(effect_update, now, existing)
            self._upsert_effect_cache(effect)
            await _maybe_await_call(self.db, "add_effect", effect)
            applied_changes["effects"].append(
                {
                    "operation": "update" if existing is not None else "create",
                    "id": effect.id,
                    "before": asdict(existing) if existing is not None else None,
                    "after": asdict(effect),
                }
            )

        if touch_profile and inferred_user_id:
            before = _profile_snapshot(self._profiles.get(inferred_user_id))
            await self._touch_profile_locked(inferred_user_id, now, participants)
            after = _profile_snapshot(self._profiles.get(inferred_user_id))
            if before != after:
                applied_changes["profiles"].append(
                    {
                        "operation": "touch",
                        "user_id": inferred_user_id,
                        "before": before,
                        "after": after,
                    }
                )
        for relationship_update in update.relationships:
            requested_user_id = (relationship_update.user_id or inferred_user_id or "").strip()
            before = _profile_snapshot(
                self._profiles.get(requested_user_id) if requested_user_id else None
            )
            profile = self._profile_from_relationship_update(
                relationship_update,
                inferred_user_id,
                now,
            )
            if profile is None:
                await self._append_state_log_locked(
                    {
                        "event": "profile_update_dropped",
                        "drop_reason": "missing_user_id",
                        "source": "relationship",
                        "conversation_id": conversation_id,
                    }
                )
                applied_changes["profiles"].append(
                    {
                        "operation": "drop",
                        "source": "relationship",
                        "reason": "missing_user_id",
                    }
                )
                continue
            self._profiles[profile.user_id] = profile
            await _maybe_await_call(self.db, "upsert_profile", profile)
            applied_changes["profiles"].append(
                {
                    "operation": "update",
                    "source": "relationship",
                    "user_id": profile.user_id,
                    "before": before,
                    "after": asdict(profile),
                }
            )
        for profile_update in update.profiles:
            requested_user_id = (profile_update.user_id or inferred_user_id or "").strip()
            before = _profile_snapshot(
                self._profiles.get(requested_user_id) if requested_user_id else None
            )
            profile = self._profile_from_update(profile_update, inferred_user_id, now)
            if profile is None:
                await self._append_state_log_locked(
                    {
                        "event": "profile_update_dropped",
                        "drop_reason": "missing_user_id",
                        "source": "profile",
                        "conversation_id": conversation_id,
                    }
                )
                applied_changes["profiles"].append(
                    {
                        "operation": "drop",
                        "source": "profile",
                        "reason": "missing_user_id",
                    }
                )
                continue
            self._profiles[profile.user_id] = profile
            await _maybe_await_call(self.db, "upsert_profile", profile)
            applied_changes["profiles"].append(
                {
                    "operation": "update",
                    "source": "profile",
                    "user_id": profile.user_id,
                    "before": before,
                    "after": asdict(profile),
                }
            )

        for todo_update in self._filter_todo_updates(update.todos):
            existing = self._find_todo(todo_update.id)
            operation = todo_update.operation
            if operation == "noop":
                applied_changes["todos"].append(
                    {"operation": "noop", "id": todo_update.id or ""}
                )
                continue
            if _todo_update_requires_existing(todo_update) and not todo_update.id:
                applied_changes["todos"].append(
                    {
                        "operation": "dropped_missing_id",
                        "requested_operation": operation or _todo_update_status(todo_update),
                    }
                )
                continue
            if todo_update.id and existing is None:
                applied_changes["todos"].append(
                    {
                        "operation": "dropped_unknown_id",
                        "requested_operation": operation or _todo_update_status(todo_update),
                        "id": todo_update.id,
                    }
                )
                continue
            record = _todo_record_from_update(todo_update, now, existing)
            if _todo_record_is_closed(record):
                self._remove_todo_cache(record["id"])
                await _maybe_await_call(self.db, "upsert_todo", record)
                applied_changes["todos"].append(
                    {
                        "operation": record.get("status") or operation or "close",
                        "id": record["id"],
                        "before": asdict(existing) if existing is not None else None,
                        "after": dict(record),
                    }
                )
                continue
            todo = _coerce_todo(record)
            if todo is None:
                applied_changes["todos"].append(
                    {
                        "operation": "drop",
                        "id": record.get("id", ""),
                        "reason": "invalid_record",
                    }
                )
                continue
            self._upsert_todo_cache(todo)
            await _maybe_await_call(self.db, "upsert_todo", record)
            applied_changes["todos"].append(
                {
                    "operation": "update" if existing is not None else "create",
                    "id": todo.id,
                    "before": asdict(existing) if existing is not None else None,
                    "after": asdict(todo),
                }
            )

        for cue_update in update.cues:
            operation = cue_update.operation
            existing = self._find_cue(cue_update.id)
            if operation == "noop":
                applied_changes["cues"].append({"operation": "noop", "id": cue_update.id or ""})
                continue
            if _operation_requires_existing(operation) and not cue_update.id:
                applied_changes["cues"].append(
                    {
                        "operation": "dropped_missing_id",
                        "requested_operation": operation,
                    }
                )
                continue
            if cue_update.id and existing is None:
                applied_changes["cues"].append(
                    {
                        "operation": "dropped_unknown_id",
                        "requested_operation": operation,
                        "id": cue_update.id,
                    }
                )
                continue
            if existing is not None and operation in _CLOSE_OPERATIONS:
                self._remove_cue_cache(existing.id)
                await _maybe_await_call(self.db, "remove_cues", [existing.id], default=0)
                applied_changes["cues"].append(
                    {
                        "operation": operation,
                        "id": existing.id,
                        "before": asdict(existing),
                        "after": None,
                    }
                )
                continue
            if existing is None and not cue_update.summary.strip():
                applied_changes["cues"].append(
                    {
                        "operation": "drop",
                        "id": cue_update.id or "",
                        "reason": "empty_summary",
                    }
                )
                continue
            cue = _cue_from_update(cue_update, conversation_id, now, existing)
            self._upsert_cue_cache(cue)
            await _maybe_await_call(self.db, "upsert_cue", cue)
            applied_changes["cues"].append(
                {
                    "operation": "update" if existing is not None else "create",
                    "id": cue.id,
                    "before": asdict(existing) if existing is not None else None,
                    "after": asdict(cue),
                }
            )

        state.mood = clamp_percent(state.mood)
        state.social_need = clamp_percent(state.social_need)
        profile_after = _profile_snapshot(
            self._profiles.get(inferred_user_id or "") if inferred_user_id else None
        )
        await self._append_update_audit_locked(
            {
                "trigger": trigger,
                "event": trigger,
                "conversation_id": conversation_id,
                "user_id": inferred_user_id,
                "inferred_user_id": inferred_user_id,
                "created_at": now,
                "raw_update": update.model_dump(exclude_none=True),
                "applied_changes": applied_changes,
                "state_before": state_before,
                "state_after": asdict(state),
                "profile_before": profile_before,
                "profile_after": profile_after,
                "summary": _audit_summary(applied_changes),
            }
        )

    async def _expire_runtime_records_locked(self, now: float) -> None:
        await _maybe_await_call(self.db, "expire_effects", now)
        await _maybe_await_call(self.db, "expire_cues", now)
        self._effects = [effect for effect in self._effects if effect.expires_at > now]
        self._cues = [cue for cue in self._cues if cue.expires_at > now]

    async def _reconcile_physiology_locked(
        self,
        now: float,
        *,
        allow_collapse: bool = True,
        recovery_source_hint: str | None = None,
    ) -> str | None:
        state = self._state
        energy_mode = self._energy_mode()
        satiety_mode = self._satiety_mode()
        physiology_enabled = energy_mode != "disabled" or satiety_mode != "disabled"
        finished_action: str | None = None

        if physiology_enabled:
            last_tick_at = state.last_tick_at
            action_until = _optional_float(state.action_until)
            if (
                state.current_action in _RESTING_ACTIONS
                and action_until is not None
                and action_until <= last_tick_at
                and action_until <= now
            ):
                finished_action = await self._maybe_finish_action_locked(
                    action_until,
                    recovery_source_hint=recovery_source_hint,
                )
                self._apply_physiology_segment_locked(
                    last_tick_at,
                    now,
                    state.current_action,
                    energy_mode,
                    satiety_mode,
                )
            elif (
                state.current_action in _RESTING_ACTIONS
                and action_until is not None
                and last_tick_at < action_until < now
            ):
                self._apply_physiology_segment_locked(
                    last_tick_at,
                    action_until,
                    state.current_action,
                    energy_mode,
                    satiety_mode,
                )
                finished_action = await self._maybe_finish_action_locked(
                    action_until,
                    recovery_source_hint=recovery_source_hint,
                )
                self._apply_physiology_segment_locked(
                    state.last_tick_at,
                    now,
                    state.current_action,
                    energy_mode,
                    satiety_mode,
                )
            else:
                self._apply_physiology_segment_locked(
                    last_tick_at,
                    now,
                    state.current_action,
                    energy_mode,
                    satiety_mode,
                )
                finished_action = await self._maybe_finish_action_locked(
                    now,
                    recovery_source_hint=recovery_source_hint,
                )

            self._refresh_energy_critical_since(now)
            is_resting = (
                state.current_action in _RESTING_ACTIONS
                and not _action_until_expired(state.action_until, now)
            )
            if (
                not is_resting
                and allow_collapse
                and finished_action != "collapsing"
                and self._should_collapse(now)
            ):
                self._trigger_collapse_locked(now)
                return "collapsing"
            return finished_action

        finished_action = await self._maybe_finish_action_locked(
            now,
            recovery_source_hint=recovery_source_hint,
        )
        if finished_action is not None:
            state.last_tick_at = now
        return finished_action

    def _apply_mood_baseline_drift_locked(
        self,
        now: float,
        previous_tick_at: float,
    ) -> None:
        baseline = 65.0
        current = self._state.mood
        if abs(current - baseline) < 0.01:
            return
        if previous_tick_at <= 0:
            return
        elapsed_hours = _elapsed_hours(previous_tick_at, now)
        if elapsed_hours <= 0:
            interval_minutes = _read_number(
                self.pm_cfg,
                "persona_agent",
                "timer_interval_minutes",
                default=30,
            )
            elapsed_hours = max(0.0, min(interval_minutes / 60.0, 0.5))
        step = (
            _read_number(self.pm_cfg, "mood", "decay_per_hour", default=0.5)
            * elapsed_hours
        )
        if step <= 0:
            return
        if current > baseline:
            self._state.mood = clamp_percent(max(baseline, current - step))
        else:
            self._state.mood = clamp_percent(min(baseline, current + step))
        if self._energy_mode() == "disabled" and self._satiety_mode() == "disabled":
            self._state.last_tick_at = now

    def _apply_physiology_segment_locked(
        self,
        start_at: float,
        end_at: float,
        current_action: str,
        energy_mode: str,
        satiety_mode: str,
    ) -> None:
        elapsed_hours = _elapsed_hours(start_at, end_at)
        if end_at <= start_at:
            return
        self._state.last_tick_at = end_at
        if elapsed_hours <= 0:
            return

        now_hour = _hour_of_day(end_at)
        hours_since_sleep = _hours_since(self._state.last_sleep_at, end_at)
        hours_since_eat = _hours_since(self._state.last_eat_at, end_at)
        recovery_action = "sleeping" if current_action == "collapsing" else current_action
        self._state.energy = self.decay.tick_energy(
            self._state.energy,
            elapsed_hours,
            recovery_action,
            hours_since_sleep,
            now_hour,
            mode=energy_mode,
        )
        self._state.satiety = self.decay.tick_satiety(
            self._state.satiety,
            elapsed_hours,
            current_action,
            hours_since_eat,
            now_hour,
            mode=satiety_mode,
        )

    async def _settle_current_action_for_replacement_locked(self, now: float) -> None:
        await self._reconcile_physiology_locked(now, allow_collapse=False)
        await self._finish_active_record_locked(
            self._state.current_action,
            now,
            {"finish_reason": "replaced"},
        )

    async def _maybe_finish_action_locked(
        self,
        now: float,
        *,
        recovery_source_hint: str | None = None,
    ) -> str | None:
        if not _action_until_expired(self._state.action_until, now):
            return None
        if self._state.current_action not in _RESTING_ACTIONS:
            return None
        previous = self._state.current_action
        self._state.current_action = "awake"
        self._state.action_until = None
        if previous == "collapsing":
            self._state.energy_critical_since = now if self._state.energy <= 0 else None
        extra_updates = {"finish_reason": "action_until"}
        if recovery_source_hint:
            extra_updates["_recovery_source_hint"] = recovery_source_hint
        await self._finish_active_record_locked(previous, now, extra_updates)
        await self._append_state_log_locked(
            {"event": "action_finished", "previous_action": previous}
        )
        return previous

    async def _maybe_daily_consolidation(self, now: float) -> None:
        fallback_hour = int(
            _read_number(
                self.pm_cfg,
                "consolidation",
                "daily_fallback_hour",
                default=4,
            )
        )
        local_dt = datetime.fromtimestamp(now)
        date_key = local_dt.date().isoformat()
        if local_dt.hour != fallback_hour:
            return
        if self._last_daily_consolidation_date == date_key:
            return
        if await self._has_daily_consolidation_record(date_key):
            self._last_daily_consolidation_date = date_key
            return
        consolidation_result = await self._run_consolidation("daily")
        await self._apply_consolidation_result_locked(consolidation_result)
        self._last_daily_consolidation_date = date_key

    async def _has_daily_consolidation_record(self, date_key: str) -> bool:
        try:
            trajectories = await _maybe_await_call(
                self.db,
                "recent_trajectories",
                20,
                default=[],
            )
            for trajectory in _iter_records(trajectories):
                if _record_date_key(trajectory) == date_key:
                    return True

            monologues = await _maybe_await_call(
                self.db,
                "recent_monologues",
                20,
                default=[],
            )
            for monologue in _iter_records(monologues):
                if _record_sleep_type(monologue) != "daily":
                    continue
                if _record_date_key(monologue) == date_key:
                    return True
        except Exception:
            logger.debug("读取每日整理去重记录失败", exc_info=True)
        return False

    async def _run_consolidation(self, sleep_type: str) -> Any:
        if self.consolidation is None:
            return None
        run = getattr(self.consolidation, "run", None)
        if run is None:
            return None
        state_snapshot = self.get_state_snapshot()
        recent_history: list[Any] = []
        try:
            result = run(state_snapshot, recent_history, sleep_type)
        except TypeError:
            try:
                result = run(state_snapshot, recent_history, sleep_type=sleep_type)
            except TypeError:
                try:
                    result = run(sleep_type=sleep_type)
                except TypeError:
                    result = run({"sleep_type": sleep_type})
        if inspect.isawaitable(result):
            return await result
        return result

    async def _apply_consolidation_result(self, result: Any) -> None:
        monologue = _consolidation_monologue(result)
        if not monologue:
            return
        async with self._state_lock:
            await self._apply_consolidation_result_locked(result)
            await self._save_state_locked()

    async def _apply_consolidation_result_locked(self, result: Any) -> None:
        monologue = _consolidation_monologue(result)
        if not monologue:
            return
        self._state.latest_monologue = monologue
        self._state.last_monologue_at = time.time()

    async def _restore_current_monologue_after_sleep_consolidation(
        self,
        monologue: str,
        monologue_at: float | None,
        result: Any,
    ) -> None:
        if not _consolidation_monologue(result):
            return
        async with self._state_lock:
            self._state.latest_monologue = monologue
            self._state.last_monologue_at = monologue_at
            await self._save_state_locked()

    async def _start_subconscious(self, event: dict[str, Any]) -> None:
        if self.subconscious_starter is None:
            return
        try:
            starter = self.subconscious_starter
            prefer_state_snapshot = False
            if not callable(starter):
                starter = (
                    getattr(starter, "start", None)
                    or getattr(starter, "run", None)
                    or getattr(starter, "trigger", None)
                )
                prefer_state_snapshot = True
            if starter is None:
                return
            result = _call_subconscious_starter(
                starter,
                self.get_state_snapshot(),
                event,
                prefer_state_snapshot=prefer_state_snapshot,
            )
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.debug("启动潜意识任务失败", exc_info=True)

    async def _save_state_locked(self) -> None:
        await _maybe_await_call(self.db, "save_state", replace(self._state))

    async def _append_state_log_locked(self, entry: dict[str, Any]) -> None:
        await _maybe_await_call(
            self.db,
            "append_state_log",
            {"state": asdict(self._state), **entry, "created_at": time.time()},
        )

    async def _append_startup_reconcile_log_locked(
        self,
        previous: PersonaState,
        now: float,
    ) -> None:
        current = self._state
        if asdict(previous) == asdict(current):
            return
        source = "startup_reconciled"
        crossed_action_until = (
            previous.current_action in _RESTING_ACTIONS
            and _optional_float(previous.action_until) is not None
            and float(_optional_float(previous.action_until) or 0.0) <= now
        )
        await self._append_state_log_locked(
            {
                "event": source,
                "reconcile_source": "offline_reconcile" if crossed_action_until else source,
                "previous_action": previous.current_action,
                "current_action": current.current_action,
                "previous_action_until": previous.action_until,
                "current_action_until": current.action_until,
                "previous": asdict(previous),
                "final": asdict(current),
            }
        )

    async def _finish_active_record_locked(
        self,
        action: str,
        ended_at: float,
        extra_updates: dict[str, Any] | None = None,
    ) -> None:
        extra = dict(extra_updates or {})
        source_hint = str(extra.pop("_recovery_source_hint", "") or "").strip()
        recovery = await self._evaluate_recovery_locked(action, ended_at, extra)
        recovery_source = source_hint or recovery.source
        updates = {
            "ended_at": ended_at,
            "status": "finished",
            **extra,
            "recovery_source": recovery_source,
        }
        recovery_payload = _recovery_estimate_payload(recovery.estimate)
        if recovery_payload:
            updates["recovery_estimate"] = recovery_payload
        if action in {"sleeping", "collapsing"}:
            record_id = self._state.active_sleep_record_id or self._active_sleep_record_id
            self._active_sleep_record_id = None
            self._state.active_sleep_record_id = None
            self._active_sleep_record = None
            if record_id:
                await _maybe_await_call(
                    self.db,
                    "update_sleep_record",
                    record_id,
                    updates,
                )
            return
        if action == "eating":
            record_id = self._state.active_eat_record_id
            self._state.active_eat_record_id = None
            self._active_eat_record = None
            if record_id:
                await _maybe_await_call(
                    self.db,
                    "update_eat_record",
                    record_id,
                    updates,
                )

    async def _evaluate_recovery_locked(
        self,
        action: str,
        ended_at: float,
        extra_updates: dict[str, Any],
    ) -> _RecoveryResult:
        if action not in _RESTING_ACTIONS:
            return _RecoveryResult()
        estimate = await self._llm_recovery_estimate(action, ended_at, extra_updates)
        source = "persona_eval"
        error = ""
        if estimate is None:
            estimate = self._fallback_recovery_estimate(action)
            source = "fallback_formula"
            error = "persona_eval_unavailable"
        self._apply_recovery_estimate_locked(estimate, ended_at)
        if estimate.latest_monologue:
            await _maybe_await_call(
                self.db,
                "add_monologue",
                {"text": estimate.latest_monologue.strip(), "created_at": ended_at},
            )
        await self._append_state_log_locked(
            {
                "event": "recovery_evaluated",
                "action": action,
                "ended_at": ended_at,
                "recovery_source": source,
                "recovery_error": error or None,
                "recovery_estimate": _recovery_estimate_payload(estimate),
            }
        )
        return _RecoveryResult(estimate=estimate, source=source, error=error)

    def _apply_recovery_estimate_locked(
        self,
        estimate: _RecoveryEstimate,
        now: float,
    ) -> None:
        state = self._state
        if estimate.energy is not None and self._energy_mode() != "disabled":
            state.energy = clamp_percent(estimate.energy)
        if estimate.satiety is not None and self._satiety_mode() != "disabled":
            state.satiety = clamp_percent(estimate.satiety)
        if estimate.mood is not None:
            state.mood = clamp_percent(estimate.mood)
        if estimate.social_need is not None:
            state.social_need = clamp_percent(estimate.social_need)
        if estimate.latest_monologue:
            state.latest_monologue = estimate.latest_monologue.strip()
            state.last_monologue_at = now

    def _fallback_recovery_estimate(self, action: str) -> _RecoveryEstimate:
        if action == "eating":
            meal_type = str(_record_field(self._active_eat_record, "meal_type") or "").strip()
            description = str(_record_field(self._active_eat_record, "description") or "").strip()
            duration = _optional_float(_record_field(self._active_eat_record, "duration_minutes"))
            if _is_substantial_meal(meal_type, description, duration):
                return _RecoveryEstimate(
                    satiety=max(self._state.satiety, _meal_satiety_target(meal_type, duration)),
                    latest_monologue="吃完后饱了一些，状态更稳了。",
                    reason="按餐次和时长进行保守恢复估计。",
                )
        if action in {"sleeping", "collapsing"}:
            duration = _optional_float(
                _record_field(self._active_sleep_record, "planned_duration_minutes")
            )
            if duration is not None and duration >= 60:
                return _RecoveryEstimate(
                    energy=max(self._state.energy, min(90.0, 45.0 + duration / 6.0)),
                    latest_monologue="醒来后精神恢复了一些。",
                    reason="按睡眠时长进行保守恢复估计。",
                )
        return _RecoveryEstimate()

    async def _load_effects(self, now: float) -> list[Effect]:
        raw = await _maybe_await_call(self.db, "get_active_effects", now, default=[])
        effects: list[Effect] = []
        for item in raw or []:
            effect = _coerce_effect(item)
            if effect is not None:
                effects.append(effect)
        return effects

    async def _load_todos(self) -> list[Todo]:
        raw = await _maybe_await_call(self.db, "get_todos", False, default=[])
        todos: list[Todo] = []
        now = time.time()
        for item in raw or []:
            todo = _coerce_todo(item)
            if todo is not None and not _todo_is_expired(todo, now):
                todos.append(todo)
        return todos

    async def _load_cues(self, now: float) -> list[Cue]:
        raw = await _maybe_await_call(self.db, "get_cues", now, default=[])
        cues: list[Cue] = []
        for item in raw or []:
            cue = _coerce_cue(item)
            if cue is not None:
                cues.append(cue)
        return cues

    async def _load_profiles(self) -> dict[str, UserProfile]:
        raw = await _maybe_await_call(self.db, "all_profiles", default=[])
        profiles = [_coerce_profile(item) for item in raw or []]
        return {profile.user_id: profile for profile in profiles if profile is not None}

    async def _touch_profile_locked(
        self,
        user_id: str,
        now: float,
        participants: Any,
    ) -> None:
        user_id = str(user_id or "").strip()
        if not user_id:
            return
        existing = self._profiles.get(user_id, UserProfile(user_id=user_id))
        profile = UserProfile(
            user_id=user_id,
            display_name=_participant_display_name(participants, user_id) or existing.display_name,
            affinity=existing.affinity,
            summary=existing.summary,
            traits=list(existing.traits),
            interaction_count=existing.interaction_count + 1,
            last_interaction_at=now,
        )
        self._profiles[user_id] = profile
        await _maybe_await_call(self.db, "upsert_profile", profile)

    def _profile_from_update(
        self,
        update: _ProfileUpdate,
        inferred_user_id: str | None,
        now: float,
    ) -> UserProfile | None:
        user_id = (update.user_id or inferred_user_id or "").strip()
        if not user_id:
            return None
        existing = self._profiles.get(user_id, UserProfile(user_id=user_id))
        affinity = existing.affinity
        if update.affinity is not None:
            affinity = update.affinity
        elif update.affinity_delta is not None:
            affinity = existing.affinity + update.affinity_delta
        return UserProfile(
            user_id=user_id,
            display_name=update.display_name or existing.display_name,
            affinity=clamp_percent(affinity),
            summary=update.summary or existing.summary,
            traits=update.traits or list(existing.traits),
            interaction_count=(
                update.interaction_count
                if update.interaction_count is not None
                else _next_profile_interaction_count(existing, now)
            ),
            last_interaction_at=update.last_interaction_at or now,
        )

    def _profile_from_relationship_update(
        self,
        update: _RelationshipUpdate,
        inferred_user_id: str | None,
        now: float,
    ) -> UserProfile | None:
        user_id = (update.user_id or inferred_user_id or "").strip()
        if not user_id:
            return None
        existing = self._profiles.get(user_id, UserProfile(user_id=user_id))
        affinity = existing.affinity
        if update.affinity is not None:
            affinity = update.affinity
        elif update.affinity_delta is not None:
            affinity = existing.affinity + update.affinity_delta
        return UserProfile(
            user_id=user_id,
            display_name=update.display_name or existing.display_name,
            affinity=clamp_percent(affinity),
            summary=update.summary or existing.summary,
            traits=update.traits or list(existing.traits),
            interaction_count=_next_profile_interaction_count(existing, now),
            last_interaction_at=now,
        )

    def _upsert_effect_cache(self, effect: Effect) -> None:
        self._effects = [item for item in self._effects if item.id != effect.id]
        self._effects.append(effect)

    def _remove_effect_cache(self, effect_id: str) -> None:
        self._effects = [item for item in self._effects if item.id != effect_id]

    def _find_effect(self, effect_id: str | None) -> Effect | None:
        if not effect_id:
            return None
        for effect in self._effects:
            if effect.id == effect_id:
                return effect
        return None

    def _upsert_todo_cache(self, todo: Todo) -> None:
        self._todos = [item for item in self._todos if item.id != todo.id]
        self._todos.append(todo)

    def _remove_todo_cache(self, todo_id: str) -> None:
        self._todos = [item for item in self._todos if item.id != todo_id]

    async def _complete_current_action_todos_locked(self, action_type: str, now: float) -> None:
        for todo in list(self._todos):
            if not _todo_matches_current_action_start(todo, action_type):
                continue
            self._remove_todo_cache(todo.id)
            record = asdict(todo)
            record["status"] = "completed"
            record["completed"] = True
            record["completed_at"] = now
            await _maybe_await_call(self.db, "upsert_todo", record)

    async def _complete_active_resting_action_todos_locked(self, now: float) -> None:
        current_action = str(self._state.current_action or "").strip().lower()
        if current_action not in _RESTING_ACTIONS:
            return
        action_until = _optional_float(self._state.action_until)
        if action_until is None or action_until <= now:
            return
        action_type = "eat" if current_action == "eating" else "sleep"
        await self._complete_current_action_todos_locked(action_type, now)

    async def _mark_expired_todos_missed_locked(self, now: float) -> None:
        self._drop_expired_todo_cache(now)
        await _maybe_await_call(self.db, "mark_expired_todos_missed", now)

    def _drop_expired_todo_cache(self, now: float) -> None:
        self._todos = [todo for todo in self._todos if not _todo_is_expired(todo, now)]

    def _find_todo(self, todo_id: str | None) -> Todo | None:
        if not todo_id:
            return None
        for todo in self._todos:
            if todo.id == todo_id:
                return todo
        return None

    def _filter_todo_updates(self, todos: list[_TodoUpdate]) -> list[_TodoUpdate]:
        filtered: list[_TodoUpdate] = []
        existing_by_id = {todo.id: todo for todo in self._todos}
        existing_ids = set(existing_by_id)
        key_by_id = {todo.id: _todo_dedupe_key(todo.scope, todo.title) for todo in self._todos}
        existing_keys = set(key_by_id.values())
        seen_new_keys: set[tuple[str, str]] = set()
        for todo in todos:
            if todo.id and todo.id in existing_ids:
                if _todo_update_status(todo) is not None:
                    filtered.append(todo)
                    continue
                existing = existing_by_id[todo.id]
                effective_title = _todo_patch_text(todo, "title", existing.title)
                if not effective_title.strip():
                    continue
                effective_scope = _todo_patch_text(todo, "scope", existing.scope)
                key = _todo_dedupe_key(effective_scope, effective_title)
                other_existing_keys = existing_keys - {key_by_id[todo.id]}
                if key in other_existing_keys or key in seen_new_keys:
                    continue
                filtered.append(todo)
                existing_keys.discard(key_by_id[todo.id])
                existing_keys.add(key)
                key_by_id[todo.id] = key
                continue
            if todo.id:
                filtered.append(todo)
                continue
            status = _todo_update_status(todo)
            if _todo_update_requires_existing(todo):
                filtered.append(todo)
                continue
            if status in _TODO_CLOSED_STATUS_VALUES:
                continue
            if not todo.title.strip():
                continue
            key = _todo_dedupe_key(todo.scope, todo.title)
            if key in existing_keys or key in seen_new_keys:
                continue
            filtered.append(todo)
            seen_new_keys.add(key)
        return filtered

    def _upsert_cue_cache(self, cue: Cue) -> None:
        self._cues = [item for item in self._cues if item.id != cue.id]
        self._cues.append(cue)

    def _remove_cue_cache(self, cue_id: str) -> None:
        self._cues = [item for item in self._cues if item.id != cue_id]

    def _find_cue(self, cue_id: str | None) -> Cue | None:
        if not cue_id:
            return None
        for cue in self._cues:
            if cue.id == cue_id:
                return cue
        return None

    async def _recent_update_audits_for_context(
        self,
        conversation_id: str,
        user_id: str | None,
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        try:
            conversation_audits = await _maybe_await_call(
                self.db,
                "recent_update_audits",
                5,
                conversation_id,
                None,
                default=[],
            )
            merged.extend(
                record for record in _iter_records(conversation_audits) if isinstance(record, dict)
            )
            if user_id:
                user_audits = await _maybe_await_call(
                    self.db,
                    "recent_update_audits",
                    5,
                    None,
                    user_id,
                    default=[],
                )
                merged.extend(
                    record for record in _iter_records(user_audits) if isinstance(record, dict)
                )
        except Exception:
            logger.warning("读取人格更新审计失败", exc_info=True)
            return []
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in merged:
            key = str(
                record.get("id")
                or record.get("audit_id")
                or record.get("created_at")
                or json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(record)
            if len(deduped) >= 5:
                break
        return deduped

    async def _long_term_memory_text_for_context(
        self,
        conversation_id: str,
        user_id: str | None,
    ) -> str:
        try:
            memories = await _maybe_await_call(self.db, "read_important", [], default=[])
        except Exception:
            logger.warning("读取重要记忆失败", exc_info=True)
            return ""
        return _format_relevant_long_term_memory(memories, conversation_id, user_id)

    async def _append_update_audit_locked(self, entry: dict[str, Any]) -> None:
        try:
            await _maybe_await_call(self.db, "append_update_audit", entry)
        except Exception:
            logger.warning("写入人格更新审计失败", exc_info=True)

    def _refresh_energy_critical_since(self, now: float) -> None:
        if self._energy_mode() != "tool":
            self._state.energy_critical_since = None
            return
        if self._state.energy <= 0:
            if self._state.energy_critical_since is None:
                self._state.energy_critical_since = now
        else:
            self._state.energy_critical_since = None

    def _should_collapse(self, now: float) -> bool:
        grace_seconds = (
            _read_number(
                self.pm_cfg,
                "physiology",
                "energy",
                "collapse",
                "grace_minutes",
                default=60,
            )
            * 60.0
        )
        return self.decay.check_collapse(
            self._state.energy,
            self._state.energy_critical_since,
            now,
            grace_seconds,
            mode=self._energy_mode(),
        )

    def _trigger_collapse_locked(self, now: float) -> None:
        sleep_hours = _read_number(
            self.pm_cfg,
            "physiology",
            "energy",
            "collapse",
            "sleep_hours",
            default=12.0,
        )
        mood_penalty = _read_number(
            self.pm_cfg,
            "physiology",
            "energy",
            "collapse",
            "mood_penalty",
            default=20.0,
        )
        self._state.current_action = "collapsing"
        self._state.action_until = now + sleep_hours * 3600.0
        self._state.last_sleep_at = now
        self._state.mood = clamp_percent(self._state.mood - mood_penalty)

    def _profile_for_conversation(self, conversation_id: str) -> UserProfile | None:
        user_id = _user_id_from_conversation(conversation_id)
        if not user_id:
            return None
        return self._profiles.get(user_id)

    def _format_age_lines(self) -> list[str]:
        profile = self.age_profile
        lines = [
            f"- 年龄档位: {profile.bracket}（{profile.age}岁）",
        ]
        if profile.emotional_hint:
            lines.append(f"- 年龄情绪提示: {profile.emotional_hint}")
        if profile.social_hint:
            lines.append(f"- 年龄社交提示: {profile.social_hint}")
        if profile.monologue_style:
            lines.append(f"- 内心独白风格: {profile.monologue_style}")
        return lines

    def _energy_mode(self) -> str:
        return str(_read_field(self.pm_cfg, "physiology", "energy", "mode") or "disabled")

    def _satiety_mode(self) -> str:
        return str(_read_field(self.pm_cfg, "physiology", "satiety", "mode") or "disabled")

    def _to_provider_reasoning(self) -> ReasoningConfig | None:
        reasoning = getattr(self.cfg, "reasoning", None)
        if reasoning is None:
            return None
        return ReasoningConfig(
            enabled=reasoning.enabled,
            budget=reasoning.budget,
            max_tokens=reasoning.max_tokens,
        )

    async def _record_usage(self, usage: Any, **metadata: Any) -> None:
        if self.usage_recorder is None:
            return
        try:
            await self.usage_recorder(
                usage,
                {
                    "provider": getattr(self.provider, "name", ""),
                    "model": self.cfg.model,
                    "agent": "人格管理",
                    **metadata,
                },
            )
        except Exception:
            logger.debug("记录人格管理模型用量失败", exc_info=True)

    def _emit_status(self, state: str, text: str) -> None:
        if self.status_callback is None:
            return
        try:
            self.status_callback(
                {
                    "state": state,
                    "text": text,
                    "model": self.cfg.model,
                    "agent": "人格管理",
                }
            )
        except Exception:
            logger.debug("更新人格管理状态失败", exc_info=True)
