"""睡眠/每日整理骨架。"""

from __future__ import annotations

import inspect
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from providers.base import IProvider, ProviderError, ReasoningConfig

from .db import PersonaDB

logger = logging.getLogger(__name__)


class ConsolidationOutput(BaseModel):
    """睡眠整理模型的结构化输出。"""

    model_config = ConfigDict(extra="ignore")

    daily_trajectory: dict[str, Any] | None = None
    tidy_todos: dict[str, Any] = Field(default_factory=dict)
    tidy_cues: dict[str, Any] = Field(default_factory=dict)
    persona_arc_adjustment: dict[str, Any] | None = None
    consolidated_memories: list[dict[str, Any]] = Field(default_factory=list)
    tomorrow_monologue: str = ""

    @field_validator("tidy_todos", "tidy_cues", mode="before")
    @classmethod
    def _none_to_dict(cls, value: Any) -> Any:
        return {} if value is None else value

    @field_validator("consolidated_memories", mode="before")
    @classmethod
    def _none_to_list(cls, value: Any) -> Any:
        return [] if value is None else value

    @field_validator("tomorrow_monologue", mode="before")
    @classmethod
    def _clean_monologue(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()


class SleepConsolidation:
    """一次性睡眠/每日整理执行器。"""

    def __init__(
        self,
        db: PersonaDB,
        provider: IProvider,
        cfg: Any,
        age_profile: Any,
        *,
        usage_recorder: Any = None,
    ) -> None:
        self.db = db
        self.provider = provider
        self.cfg = cfg
        self.age_profile = age_profile
        self.usage_recorder = usage_recorder

    async def run(self, state: Any, recent_history: Any, sleep_type: Any) -> dict[str, Any]:
        """执行整理，失败时返回安全默认值且不向外抛异常。"""

        sleep_type_text = str(sleep_type or "").strip() or "unknown"
        last_error = ""
        for attempt in range(2):
            messages = self._build_messages(
                state,
                recent_history,
                sleep_type_text,
                retry=attempt > 0,
            )
            try:
                result = await self.provider.chat_completion(
                    messages,
                    model=self.cfg.model,
                    tools=None,
                    temperature=getattr(self.cfg, "temperature", 0.6),
                    top_p=getattr(self.cfg, "top_p", 1.0),
                    max_tokens=getattr(self.cfg, "max_tokens", 16384),
                    reasoning=self._to_provider_reasoning(),
                    stream=True,
                    timeout=_first_token_timeout(self.cfg) * 6 + 60.0,
                    first_token_timeout=_first_token_timeout(self.cfg) * 2,
                )
                await self._record_usage(
                    getattr(result, "usage", None),
                    sleep_type=sleep_type_text,
                )
            except ProviderError as e:
                last_error = str(e)
                _log_attempt_failure(
                    "睡眠整理模型调用失败",
                    e,
                    attempt=attempt,
                    exc_info=False,
                )
                continue
            except Exception as e:  # noqa: BLE001
                last_error = str(e)
                _log_attempt_failure(
                    "睡眠整理模型调用异常",
                    e,
                    attempt=attempt,
                    exc_info=True,
                )
                continue

            parsed = _parse_json_object(getattr(result, "content", "") or "")
            if parsed is None:
                last_error = "no_valid_json"
                _log_attempt_failure(
                    "睡眠整理返回无合法 JSON",
                    None,
                    attempt=attempt,
                    raw=(getattr(result, "content", "") or "")[:200],
                )
                continue

            try:
                output = ConsolidationOutput.model_validate(parsed)
            except ValidationError as e:
                last_error = str(e)
                _log_attempt_failure(
                    "睡眠整理 JSON 结构校验失败",
                    e,
                    attempt=attempt,
                    exc_info=False,
                )
                continue

            await self._persist_output(output, state, sleep_type_text)
            return output.model_dump()

        logger.error("睡眠整理失败，返回安全默认值: %s", last_error)
        return _safe_default()

    def _to_provider_reasoning(self) -> ReasoningConfig | None:
        if self.cfg.reasoning is None:
            return None
        return ReasoningConfig(
            enabled=self.cfg.reasoning.enabled,
            budget=self.cfg.reasoning.budget,
            max_tokens=self.cfg.reasoning.max_tokens,
        )

    async def _record_usage(self, usage: Any, **metadata: Any) -> None:
        if self.usage_recorder is None:
            return
        try:
            await _maybe_await(
                self.usage_recorder(
                    usage,
                    {
                        "provider": getattr(self.provider, "name", type(self.provider).__name__),
                        "model": self.cfg.model,
                        "agent": "睡眠整理",
                        "operation": "sleep_consolidation",
                        **metadata,
                    },
                )
            )
        except Exception:
            logger.debug("记录睡眠整理模型用量失败", exc_info=True)

    def _build_messages(
        self,
        state: Any,
        recent_history: Any,
        sleep_type: str,
        *,
        retry: bool,
    ) -> list[dict[str, str]]:
        system = (
            "你是当前角色的睡眠/每日整理系统。根据状态和近期历史，整理可持久化的人格轨迹、"
            "待办、线索、重要记忆和明日醒来前的内心独白。只输出一个合法 JSON 对象，"
            "不要输出 Markdown、代码块或解释。"
        )
        prompt_parts = [
            f"<整理类型>\n{sleep_type}\n</整理类型>",
            f"<当前状态>\n{_json_text(_to_plain_data(state))}\n</当前状态>",
            f"<近期历史>\n{_json_text(_to_plain_data(recent_history))}\n</近期历史>",
        ]
        age_block = _format_age_block(self.age_profile)
        if age_block:
            prompt_parts.append(age_block)
        prompt_parts.append(_OUTPUT_CONTRACT)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n\n".join(prompt_parts)},
        ]
        if retry:
            messages.append(
                {
                    "role": "system",
                    "content": "上一次输出无法解析或不符合结构。重新输出一个合法 JSON 对象，不要附加任何说明。",
                }
            )
        return messages

    async def _persist_output(
        self,
        output: ConsolidationOutput,
        state: Any,
        sleep_type: str,
    ) -> None:
        if output.daily_trajectory:
            try:
                await _maybe_await(self.db.add_trajectory(dict(output.daily_trajectory)))
            except Exception:
                logger.exception("写入每日轨迹失败")

        if output.persona_arc_adjustment and _has_change(output.persona_arc_adjustment):
            try:
                await _maybe_await(self.db.add_arc_event(dict(output.persona_arc_adjustment)))
            except Exception:
                logger.exception("写入人格弧线事件失败")

        await self._merge_important_memories(output.consolidated_memories)
        await self._upsert_new_records(output.tidy_todos, "upsert_todo", "待办")
        await self._upsert_new_records(output.tidy_cues, "upsert_cue", "线索")

        if output.tomorrow_monologue:
            await self._add_monologue(output.tomorrow_monologue, _read_field(state, "mood"), sleep_type)

    async def _merge_important_memories(self, new_memories: list[dict[str, Any]]) -> None:
        if not new_memories:
            return
        try:
            existing = await _maybe_await(self.db.read_important(default=[]))
            if not isinstance(existing, list):
                existing = []
            merged = _merge_memories(existing, new_memories)
            await _maybe_await(self.db.write_important(merged))
        except Exception:
            logger.exception("合并重要记忆失败")

    async def _upsert_new_records(self, tidy: dict[str, Any], method_name: str, label: str) -> None:
        method = getattr(self.db, method_name, None)
        if not callable(method) or not isinstance(tidy, dict):
            return
        for record in _iter_new_records(tidy):
            try:
                await _maybe_await(method(record))
            except Exception:
                logger.debug("写入%s失败: %r", label, record, exc_info=True)

    async def _add_monologue(self, text: str, mood: Any, sleep_type: str) -> None:
        method = getattr(self.db, "add_monologue", None)
        if not callable(method):
            return

        timestamp = time.time()
        payload = {"text": text, "mood": mood, "sleep_type": sleep_type, "created_at": timestamp}
        try:
            accepts_two = _accepts_positional_count(method, 2)
            if accepts_two is True:
                await _maybe_await(method(text, mood))
            elif accepts_two is False:
                await _maybe_await(method(payload))
            else:
                try:
                    await _maybe_await(method(text, mood))
                except TypeError:
                    await _maybe_await(method(payload))
            await self._save_latest_monologue(text, timestamp)
        except Exception:
            logger.exception("写入明日独白失败")

    async def _save_latest_monologue(self, text: str, timestamp: float) -> None:
        state = await self._load_state_for_monologue_update()
        if state is None:
            return
        if isinstance(state, dict):
            state["latest_monologue"] = text
            state["last_monologue_at"] = timestamp
            await _maybe_await(self.db.save_state(state))
            return
        try:
            setattr(state, "latest_monologue", text)
            setattr(state, "last_monologue_at", timestamp)
        except Exception:
            logger.debug("更新明日独白状态对象失败", exc_info=True)
            return
        await _maybe_await(self.db.save_state(state))

    async def _load_state_for_monologue_update(self) -> Any:
        method = getattr(self.db, "get_state", None)
        if not callable(method):
            return None
        try:
            return await _maybe_await(method())
        except Exception:
            logger.debug("读取人格状态用于明日独白回填失败", exc_info=True)
            return None


_OUTPUT_CONTRACT = (
    "<输出结构>\n"
    "返回字段必须是：\n"
    "{\n"
    '  "daily_trajectory": null 或对象,\n'
    '  "tidy_todos": {"new": [对象, ...]},\n'
    '  "tidy_cues": {"new": [对象, ...]},\n'
    '  "persona_arc_adjustment": null 或 {"has_change": true/false, ...},\n'
    '  "consolidated_memories": [对象, ...],\n'
    '  "tomorrow_monologue": "字符串"\n'
    "}\n"
    "没有内容时使用 null、空对象、空数组或空字符串。\n"
    "consolidated_memories 只允许写入长期稳定事实；每条必须是客观完整句，且有明确主语或明确对象。\n"
    "不要把 tomorrow_monologue 或 latest_monologue 式文本写进 important memory。\n"
    "consolidated_memories 禁止写离开上下文无法理解的内心独白、短期情绪或泛泛关系感受，"
    "例如“我”“他”“这种感觉”“被理解”“心里...”这类无明确指代的内容。\n"
    "无合格事实返回空数组，不要为了填充而生成记忆。"
    "</输出结构>"
)


def _parse_json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(raw[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _safe_default() -> dict[str, Any]:
    return ConsolidationOutput().model_dump()


def _first_token_timeout(cfg: Any) -> float:
    value = getattr(cfg, "first_token_timeout_seconds", 30.0)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 30.0


def _format_age_block(age_profile: Any) -> str:
    if age_profile is None:
        return ""
    age = _read_field(age_profile, "age")
    bracket = _read_field(age_profile, "bracket")
    bedtime = _read_field(age_profile, "bedtime_hour")
    wakeup = _read_field(age_profile, "wakeup_hour")
    ideal_sleep = _read_field(age_profile, "ideal_sleep_hours")
    monologue_style = _read_field(age_profile, "monologue_style")
    emotional_hint = _read_field(age_profile, "emotional_hint")
    social_hint = _read_field(age_profile, "social_hint")
    lines = [
        "<年龄信息>",
        f"年龄：{age}" if age is not None else "",
        f"档位：{bracket}" if bracket else "",
        f"理想睡眠小时：{ideal_sleep}" if ideal_sleep is not None else "",
        f"建议作息：{bedtime} - {wakeup}" if bedtime is not None and wakeup is not None else "",
        f"独白风格：{monologue_style}" if monologue_style else "",
        f"情绪提示：{emotional_hint}" if emotional_hint else "",
        f"社交提示：{social_hint}" if social_hint else "",
        "</年龄信息>",
    ]
    return "\n".join(line for line in lines if line)


def _to_plain_data(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _to_plain_data(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_to_plain_data(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _to_plain_data(model_dump())
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, dict):
        return {
            key: _to_plain_data(item)
            for key, item in attrs.items()
            if not key.startswith("_") and not callable(item)
        }
    return value


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        return json.dumps(str(value), ensure_ascii=False)


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


def _has_change(value: Mapping[str, Any]) -> bool:
    raw = value.get("has_change")
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "y", "是"}
    return bool(raw)


def _merge_memories(
    existing: list[Any],
    new_memories: list[dict[str, Any]],
) -> list[Any]:
    merged = list(existing)
    seen = {
        key
        for memory in merged
        if isinstance(memory, Mapping) and (key := _memory_key(memory))
    }
    for memory in new_memories:
        key = _memory_key(memory)
        if key and key in seen:
            continue
        merged.append(dict(memory))
        if key:
            seen.add(key)
    return merged


def _memory_key(memory: Mapping[str, Any]) -> tuple[str, str] | tuple[str, str, str] | None:
    memory_id = str(memory.get("id") or "").strip()
    if memory_id:
        return ("id", memory_id)
    content = str(memory.get("content") or "").strip()
    if not content:
        return None
    scope = str(memory.get("scope") or "").strip()
    return ("content", scope, content)


def _iter_new_records(tidy: dict[str, Any]) -> list[dict[str, Any]]:
    raw = tidy.get("new", [])
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _accepts_positional_count(func: Any, count: int) -> bool | None:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return None
    parameters = list(signature.parameters.values())
    if any(param.kind == inspect.Parameter.VAR_POSITIONAL for param in parameters):
        return True
    positional = [
        param
        for param in parameters
        if param.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return len(positional) >= count


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _log_attempt_failure(
    message: str,
    error: Exception | None,
    *,
    attempt: int,
    exc_info: bool = False,
    raw: str = "",
) -> None:
    suffix = "，准备重试" if attempt == 0 else "，返回安全默认值"
    detail = f": {error}" if error is not None else (f": {raw!r}" if raw else "")
    if attempt == 0:
        logger.warning("%s%s%s", message, detail, suffix, exc_info=exc_info)
    else:
        logger.error("%s%s%s", message, detail, suffix, exc_info=exc_info)
