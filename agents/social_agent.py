"""社交决策 Agent。

用于后续接入 ProactiveLoop / PersonaAgent：先做本地状态短路，再用小模型输出
结构化社交决策。
"""

from __future__ import annotations

import inspect
import json
import logging
import time
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from providers.base import IProvider, ProviderError, ReasoningConfig

from .base import StatusCallback, UsageRecorder

logger = logging.getLogger(__name__)


SOCIAL_DECISION_INSTRUCTION = (
    "你是社交决策模块。请根据当前上下文判断是否需要主动发言。\n"
    "只输出一个 JSON 对象，不要 Markdown，不要解释。\n"
    "字段：decision 必须是 skip/react/text_lite/full 之一；reason 是一句简短理由；"
    "targets 是目标列表；suggested_intent 是建议意图；suggested_content 是可选草稿。\n"
    "含义：skip=不行动；react=只记录轻量反应，不触发主聊天；"
    "text_lite=可用轻量文本回复；full=需要交给主聊天 Agent 完整处理。\n"
    '示例：{"decision":"skip","reason":"","targets":[],"suggested_intent":"","suggested_content":""}'
)

_SKIP_DECISION = {
    "decision": "skip",
    "reason": "",
    "targets": [],
    "suggested_intent": "",
    "suggested_content": "",
}
_NECESSARY_TODO_KEYWORDS = (
    "唤醒",
    "叫醒",
    "提醒",
    "吃饭",
    "进食",
    "喝水",
    "补水",
    "饮水",
    "生理",
    "必要",
    "必须",
    "用药",
    "服药",
    "吃药",
    "饥饿",
    "饿了",
    "休息",
    "起床",
)
_NECESSARY_TODO_MIN_PRIORITY = 5.0


class _SocialDecisionModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    decision: Literal["skip", "react", "text_lite", "full"]
    reason: str
    targets: list[Any] = Field(default_factory=list)
    suggested_intent: str
    suggested_content: str


class SocialAgent:
    """社交关系维护和主动发言判定器。"""

    def __init__(
        self,
        provider: IProvider,
        cfg,
        *,
        persona_agent=None,
        usage_recorder: UsageRecorder | None = None,
        status_callback: StatusCallback | None = None,
    ) -> None:
        self.provider = provider
        self.cfg = cfg
        self.persona_agent = persona_agent
        self.usage_recorder = usage_recorder
        self.status_callback = status_callback

    async def should_act(self, messages: list[dict[str, Any]]) -> tuple[bool, str]:
        """兼容 ProactiveRouterAgent 的布尔判定接口。"""

        decision = await self.decide(messages)
        kind = str(decision.get("decision") or "").strip()
        if kind in {"text_lite", "full"}:
            return True, str(decision.get("reason") or "")
        return False, ""

    async def decide(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """返回结构化社交决策。"""

        state_skip = await self._preflight_skip()
        if state_skip is not None:
            return state_skip

        for attempt in range(2):
            result_text = await self._call_provider(messages, retry=attempt > 0)
            if result_text is None:
                return dict(_SKIP_DECISION)

            parsed = _parse_json_object(result_text)
            if parsed is None:
                logger.warning("社交决策返回无效 JSON，attempt=%s", attempt + 1)
                continue

            try:
                decision = _SocialDecisionModel.model_validate(parsed)
            except ValidationError as e:
                logger.warning("社交决策字段校验失败，attempt=%s: %s", attempt + 1, e)
                continue

            data = decision.model_dump()
            self._emit_status("idle", "社交决策完成")
            return data

        logger.warning("社交决策重试后仍无合法输出，兜底跳过")
        self._emit_status("idle", "社交决策跳过")
        return dict(_SKIP_DECISION)

    async def _preflight_skip(self) -> dict[str, Any] | None:
        if self.persona_agent is None:
            return None

        if await self._persona_is_resting() and not await self._has_necessary_persona_todo():
            decision = dict(_SKIP_DECISION)
            decision["reason"] = "persona_resting"
            return decision

        if await self._should_skip_for_low_energy():
            decision = dict(_SKIP_DECISION)
            decision["reason"] = "low_energy"
            return decision

        return None

    async def _persona_is_resting(self) -> bool:
        is_resting = getattr(self.persona_agent, "is_resting", None)
        if is_resting is None:
            return False
        try:
            value = is_resting() if callable(is_resting) else is_resting
            value = await _maybe_await(value)
        except Exception:
            logger.debug("读取人格休息状态失败", exc_info=True)
            return False
        return bool(value)

    async def _should_skip_for_low_energy(self) -> bool:
        modes = [
            await self._read_persona_attr("physiology_energy_mode"),
            await self._read_persona_attr("energy_mode"),
        ]
        if not any(str(mode or "").strip() == "tool" for mode in modes):
            return False

        snapshot = await self._read_state_snapshot()
        energy = _coerce_float(_read_field(snapshot, "energy"))
        if energy is None:
            state = _read_field(snapshot, "state", "persona_state", "physiology")
            energy = _coerce_float(_read_field(state, "energy"))
        return energy is not None and energy < 10

    async def _has_necessary_persona_todo(self) -> bool:
        todos = await self._read_persona_todos()
        now = time.time()
        for todo in todos:
            if _todo_is_completed(todo):
                continue
            if _todo_is_expired(todo, now):
                continue
            priority = _coerce_float(_read_field(todo, "priority", "level"))
            if priority is None or priority < _NECESSARY_TODO_MIN_PRIORITY:
                continue
            text = _todo_text(todo).lower()
            if any(keyword in text for keyword in _NECESSARY_TODO_KEYWORDS):
                return True
        return False

    async def _read_persona_todos(self) -> list[Any]:
        if self.persona_agent is None:
            return []
        for name in ("get_todos_for_proactive", "get_pending_todos", "get_todos", "todos", "_todos"):
            if not hasattr(self.persona_agent, name):
                continue
            try:
                value = getattr(self.persona_agent, name)
                if callable(value):
                    value = _call_todo_reader(value)
                value = await _maybe_await(value)
            except Exception:
                logger.debug("读取人格待办失败: %s", name, exc_info=True)
                continue
            if isinstance(value, list):
                return list(value)
            if isinstance(value, tuple):
                return list(value)
        return []

    async def _read_persona_attr(self, name: str) -> Any:
        if self.persona_agent is None or not hasattr(self.persona_agent, name):
            return None
        try:
            value = getattr(self.persona_agent, name)
            if callable(value):
                value = value()
            return await _maybe_await(value)
        except Exception:
            logger.debug("读取人格属性失败: %s", name, exc_info=True)
            return None

    async def _read_state_snapshot(self) -> Any:
        if self.persona_agent is None:
            return None
        for name in ("get_state_snapshot", "state_snapshot", "snapshot", "state"):
            if not hasattr(self.persona_agent, name):
                continue
            try:
                value = getattr(self.persona_agent, name)
                if callable(value):
                    value = value()
                return await _maybe_await(value)
            except TypeError:
                continue
            except Exception:
                logger.debug("读取人格状态快照失败: %s", name, exc_info=True)
                return None
        return None

    async def _call_provider(
        self,
        messages: list[dict[str, Any]],
        *,
        retry: bool,
    ) -> str | None:
        check_msgs = list(messages)
        check_msgs.append({"role": "system", "content": SOCIAL_DECISION_INSTRUCTION})
        if retry:
            check_msgs.append(
                {
                    "role": "user",
                    "content": "上一轮输出不是合法社交决策 JSON。请只返回符合字段要求的 JSON 对象。",
                }
            )

        try:
            self._emit_status("thinking", "社交决策判断中")
            result = await self.provider.chat_completion(
                check_msgs,
                model=str(getattr(self.cfg, "model", "") or ""),
                tools=None,
                temperature=float(getattr(self.cfg, "temperature", 0.6)),
                top_p=float(getattr(self.cfg, "top_p", 1.0)),
                max_tokens=int(getattr(self.cfg, "max_tokens", 512)),
                reasoning=self._to_provider_reasoning(),
                stream=True,
                timeout=float(getattr(self.cfg, "first_token_timeout_seconds", 30.0)) * 2,
                first_token_timeout=float(getattr(self.cfg, "first_token_timeout_seconds", 30.0)),
            )
            await self._record_usage(result.usage, operation="social_decide")
        except ProviderError as e:
            logger.warning("社交决策调用失败: %s，默认跳过", e)
            self._emit_status("error", "社交决策失败")
            return None
        except Exception as e:
            logger.exception("社交决策异常: %s", e)
            self._emit_status("error", "社交决策异常")
            return None

        return result.content or ""

    def _to_provider_reasoning(self) -> ReasoningConfig | None:
        cfg_reasoning = getattr(self.cfg, "reasoning", None)
        if cfg_reasoning is None:
            return None
        return ReasoningConfig(
            enabled=bool(getattr(cfg_reasoning, "enabled", False)),
            budget=getattr(cfg_reasoning, "budget", None),
            max_tokens=getattr(cfg_reasoning, "max_tokens", None),
        )

    async def _record_usage(self, usage, **metadata: Any) -> None:
        if self.usage_recorder is None:
            return
        try:
            await self.usage_recorder(
                usage,
                {
                    "provider": str(getattr(self.provider, "name", "")),
                    "model": str(getattr(self.cfg, "model", "") or ""),
                    "agent": "社交决策",
                    **metadata,
                },
            )
        except Exception:
            logger.debug("记录社交决策用量失败", exc_info=True)

    def _emit_status(self, state: str, text: str) -> None:
        if self.status_callback is None:
            return
        try:
            self.status_callback(
                {
                    "state": state,
                    "text": text,
                    "model": str(getattr(self.cfg, "model", "") or ""),
                    "agent": "社交决策",
                }
            )
        except Exception:
            logger.debug("更新社交决策状态失败", exc_info=True)


def _parse_json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start:end])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _read_field(source: Any, *names: str) -> Any:
    if source is None:
        return None
    if isinstance(source, Mapping):
        for name in names:
            if name in source:
                return source[name]
        return None
    for name in names:
        if hasattr(source, name):
            return getattr(source, name)

    model_dump = getattr(source, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return _read_field(dumped, *names)
    return None


def _coerce_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _call_todo_reader(reader: Any) -> Any:
    try:
        signature = inspect.signature(reader)
    except (TypeError, ValueError):
        try:
            return reader(False)
        except TypeError:
            return reader()
    parameters = signature.parameters
    if "include_completed" in parameters:
        return reader(include_completed=False)
    required_positionals = [
        parameter
        for parameter in parameters.values()
        if parameter.default is inspect.Parameter.empty
        and parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if required_positionals:
        return reader(False)
    return reader()


def _todo_is_completed(todo: Any) -> bool:
    value = _read_field(todo, "completed", "done", "is_completed")
    return bool(value)


def _todo_is_expired(todo: Any, now: float) -> bool:
    expires_at = _coerce_float(_read_field(todo, "expires_at", "expire_at", "deadline"))
    return expires_at is not None and expires_at <= now


def _todo_text(todo: Any) -> str:
    values = [
        _read_field(todo, "title", "summary", "content", "text"),
        _read_field(todo, "reason", "description", "note"),
        _read_field(todo, "scope"),
    ]
    return " ".join(str(value or "") for value in values).strip()


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value
