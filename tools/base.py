"""工具系统核心抽象。

设计原则：
    1. 装饰器注册：用 `@tool(name=..., category=..., feature=...)` 把纯函数注册成工具。
       工具实现是纯函数，签名为 `async def fn(args: ArgsModel, ctx: ToolContext) -> dict`。
    2. Pydantic 自动派生 schema：每个工具关联一个 Pydantic args 模型，
       从 `model_json_schema()` 派生 OpenAI tool 格式，避免手写 schema 漂移。
    3. 依赖注入：工具不再直接持有 Bot/Adapter，所有依赖通过 ToolContext 注入。
       这让工具脱离任何具体适配器，便于跨平台。
    4. schema 稳定：常驻工具集合尽量不随 feature 开关变化；低频/高风险/大 schema
       工具以 stub 暴露名称，模型需要时通过 tool_search 获取参数摘要或完整 schema。

OpenAI tool 格式参考：
    {
        "type": "function",
        "function": {
            "name": "send_private_messages",
            "description": "...",
            "parameters": {
                "type": "object",
                "properties": {...},
                "required": [...]
            }
        }
    }
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    Protocol,
    TypeVar,
)

from pydantic import BaseModel

if TYPE_CHECKING:
    from adapters.base import IAdapter
    from memory import ArchiveStore, HistoryManager, ImportantMemoryManager

logger = logging.getLogger(__name__)


class ToolSchemaMode(StrEnum):
    FULL = "full"
    STUB = "stub"


def _default_tool_result_budgets() -> dict[str, Any]:
    try:
        from app_config.schema import default_tool_result_budgets

        return default_tool_result_budgets()
    except Exception:
        logger.debug("加载默认工具预算失败，使用空预算表", exc_info=True)
        return {}


# ============================================================
# Feature service protocols（极简骨架，P2 完整实现）
# ============================================================


class IVisionService(Protocol):
    """图像理解服务接口。"""

    async def describe(self, image_url: str, prompt: str = "") -> str | dict[str, str]: ...


class IWebSearchService(Protocol):
    """联网搜索服务接口。"""

    async def search(self, query: str) -> str: ...


class IWeatherService(Protocol):
    """天气查询服务接口。"""

    async def query(self, city: str, days: int = 1) -> str: ...


class ITTSService(Protocol):
    """文字转语音服务接口（重声明，避免 features.tts 在主路径循环 import）。

    具体定义在 features/tts/__init__.py；此处 Protocol 仅供 ToolContext 类型注解用。
    """

    async def synthesize(
        self,
        text: str,
        *,
        reference_audio: Any = None,
        prompt: str = "",
    ) -> Any: ...


# ============================================================
# 唤醒回调签名
# ============================================================


WakeupCallback = Callable[
    [int, str, dict[str, Any] | None, str, str | None],
    Awaitable[None],
]
"""schedule_wakeup 工具调用的回调：(delay_seconds, reminder, target, mode, message_text) -> None"""

SendActionsCallback = Callable[..., Awaitable[dict[str, Any]]]
"""发送类工具的 Phase 0 队列回调：(actions, source_tool, metadata=...) -> result。"""

AgentTaskCallback = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
"""子 Agent 任务回调：(payload) -> in-band tool result。"""


# ============================================================
# ToolContext —— 工具执行所需的全部依赖
# ============================================================


@dataclass(slots=True)
class ToolContext:
    """工具执行上下文。

    所有工具都从此对象获取所需依赖。message_pipeline / chat handler 在
    每次调用 AgentRunner 前构造一份 ToolContext 并通过闭包传给 executor。

    `collected` 仅保留为少量遗留/兼容动作的兜底队列。Phase 0 后常规发送类
    工具会在工具调用内即时发送并返回 msg_id。
    """

    adapter: IAdapter | None = None
    """当前会话使用的适配器实例。Phase 1.7 大多数工具直接调用 adapter 方法。"""

    important: ImportantMemoryManager | None = None
    """重要记忆管理器。"""

    conversation_id: str | None = None
    """当前工具调用所属会话标签，如 private:123 / group:456。"""

    history: HistoryManager | None = None
    """对话历史管理器（部分工具如总结需要）。"""

    archive: ArchiveStore | None = None
    """永久历史归档（recall_history 等工具使用）。"""

    vision: IVisionService | None = None
    web_search: IWebSearchService | None = None
    weather: IWeatherService | None = None
    tts: ITTSService | None = None
    """TTS 服务实例。启用本地 TTS 插件时由 Runtime 注入。"""

    wakeup_cb: WakeupCallback | None = None
    """schedule_wakeup 工具触发时调用。"""

    workspace_dir: Path | None = None
    """AI 工作目录（data/workspace/）。所有文件类工具操作都被限制在这个目录下。
    None 表示禁用文件类工具（read/write/edit/list/delete/upload_file/run_python）。"""

    emoji_dir: Path | None = None
    """表情包目录（send_* 工具通过 emoji 参数按名称引用时用）。"""

    default_history_fetch_count: int = 100
    """summarize_chat_history 工具拉取群历史的默认 count 参数。"""

    typing_chars_per_second: float = 1.0
    typing_max_delay_seconds: float = 2.0

    tool_result_soft_limit_tokens: int = 600
    tool_result_hard_cap_tokens: int = 1500
    tool_result_soft_overrides: dict[str, int] = field(default_factory=dict)
    tool_result_default_budget_tokens: int = 800
    tool_result_default_hard_cap_tokens: int = 3000
    tool_result_budgets: dict[str, Any] = field(default_factory=_default_tool_result_budgets)
    """工具结果创建即定型压缩阈值。"""

    activity_cb: Callable[[], None] | None = None
    """真实发送/外部动作成功后刷新运行时活动计时。"""

    send_actions_cb: SendActionsCallback | None = None
    """Phase 0 真异步发送入口。存在时 send_* 工具交给 pipeline 队列处理。"""

    agent_task_cb: AgentTaskCallback | None = None
    """start_agent_task 工具触发时调用，创建后台子 Agent 任务。"""

    collected: list[dict[str, Any]] = field(default_factory=list)
    """遗留发送动作兜底队列。
    每条结构：{"action": "private"|"group", "target": str, "content": str,
    "label": str, "delay": float}；语音动作额外带
    {"kind": "voice", "audio_path": str}。常规 send_* 工具不再写入该队列。"""

    extras: dict[str, Any] = field(default_factory=dict)
    """业务方塞的额外数据（如 self_id 等），工具内部读取。"""


# ============================================================
# ITool —— 工具自身的元信息载体
# ============================================================


ArgsModelT = TypeVar("ArgsModelT", bound=BaseModel)


ToolFunc = Callable[[Any, ToolContext], Awaitable[dict[str, Any]]]
"""工具实现函数签名。第一个参数是已校验的 Pydantic args 实例。"""


@dataclass(slots=True)
class ToolSpec:
    """工具规格：装饰器收集到的全部元信息。"""

    name: str
    """工具名（对 LLM 暴露）。"""

    description: str
    """工具描述（对 LLM 暴露）。"""

    args_model: type[BaseModel]
    """Pydantic args 模型。"""

    func: ToolFunc
    """实际执行函数。"""

    category: str = "misc"
    """归类：messaging / memory / platform / control / feature / misc。"""

    feature: str | None = None
    """关联的 feature 开关（如 'vision' / 'web_search' / 'weather'）。
    None 表示核心工具，永远启用。"""

    no_feedback: bool = False
    """兼容旧调用方的工具元信息；runner 不再据此提前终止循环。"""

    schema_mode: ToolSchemaMode = ToolSchemaMode.FULL
    """对模型暴露 full schema 还是 stub schema。"""

    short_description: str | None = None
    """stub schema 和 tool_search 列表里使用的短说明。"""

    search_tags: list[str] = field(default_factory=list)
    """tool_search 可用的检索标签。"""

    risk_level: Literal["low", "medium", "high"] = "low"
    """工具风险等级，用于 tool_search 返回风险提醒。"""

    examples: list[dict[str, Any]] = field(default_factory=list)
    """tool_search 返回的调用示例。"""

    def to_openai_schema(self) -> dict[str, Any]:
        """从 Pydantic 模型派生 OpenAI tool 格式 schema。

        关键转换：
            - Pydantic 的 `$defs` / `$ref` 内联（OpenAI 不支持 $ref）
            - 移除 'title' 字段（仅用于 Pydantic 内部）
            - description 用工具 description 而非模型 docstring
        """
        if self.schema_mode == ToolSchemaMode.STUB:
            return self.to_stub_openai_schema()
        raw_schema = self.args_model.model_json_schema()
        cleaned = _strip_pydantic_metadata(_inline_refs(raw_schema))
        cleaned = _add_finish_after_success_parameter(cleaned, self.name)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": cleaned,
            },
        }

    def to_stub_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": (
                    self.short_description
                    or f"{self.name} 是低频工具。调用前先用 tool_search 查询参数摘要和约束。"
                ),
                "parameters": _add_finish_after_success_parameter(
                    {
                        "type": "object",
                        "properties": {
                            "_tool_search_required": {
                                "type": "boolean",
                                "description": (
                                    "这是占位参数。真实调用前必须先用 tool_search 查询参数摘要。"
                                ),
                            }
                        },
                        "additionalProperties": True,
                    },
                    self.name,
                ),
            },
        }

    def full_parameters_schema(self) -> dict[str, Any]:
        raw_schema = self.args_model.model_json_schema()
        cleaned = _strip_pydantic_metadata(_inline_refs(raw_schema))
        return _add_finish_after_success_parameter(cleaned, self.name)

    def tool_search_result(self, *, detail: Literal["summary", "full"] = "summary") -> dict[str, Any]:
        parameters = self.full_parameters_schema()
        required_fields = parameters.get("required", [])
        result = {
            "ok": True,
            "status": "found",
            "tool_name": self.name,
            "description": self.description,
            "short_description": self.short_description or self.description,
            "required_fields": required_fields,
            "risk_level": self.risk_level,
            "search_tags": list(self.search_tags),
            "examples": list(self.examples),
            "constraints": _tool_constraints(self),
        }
        if detail == "full":
            result["parameters_schema"] = parameters
            result["next"] = "已返回完整 JSON schema。若确认要使用该工具，请按 parameters_schema 调用原工具。"
            return result

        summary_fields = _summarize_parameter_fields(parameters)
        result["parameters"] = summary_fields
        result["parameter_summary"] = {
            "type": parameters.get("type", "object"),
            "required_fields": required_fields,
            "fields": summary_fields,
        }
        result["next"] = (
            "已返回参数摘要，足够按字段调用原工具；需要完整 JSON schema 时，"
            "用 detail=full 重新查询 tool_search。"
        )
        return result


# ============================================================
# 装饰器：把纯函数注册成工具
# ============================================================


_DEFAULT_REGISTRY: list[ToolSpec] = []
"""全局收集所有 @tool 装饰过的工具规格。
ToolRegistry.from_default() 从此列表构建。"""


def tool(
    *,
    name: str,
    description: str,
    args_model: type[BaseModel],
    category: str = "misc",
    feature: str | None = None,
    no_feedback: bool = False,
    schema_mode: ToolSchemaMode | str = ToolSchemaMode.FULL,
    short_description: str | None = None,
    search_tags: list[str] | None = None,
    risk_level: Literal["low", "medium", "high"] = "low",
    examples: list[dict[str, Any]] | None = None,
) -> Callable[[ToolFunc], ToolFunc]:
    """把函数注册成工具的装饰器。

    被装饰函数的签名必须是 `async def fn(args: ArgsModel, ctx: ToolContext) -> dict`。

    Examples:
        @tool(
            name="send_private_messages",
            description="向 QQ 用户发送私聊消息",
            args_model=SendPrivateArgs,
            category="messaging",
        )
        async def send_private(args: SendPrivateArgs, ctx: ToolContext) -> dict:
            ...
    """

    def decorator(func: ToolFunc) -> ToolFunc:
        if not inspect.iscoroutinefunction(func):
            raise TypeError(f"工具 {name} 必须是 async 函数")

        spec = ToolSpec(
            name=name,
            description=description,
            args_model=args_model,
            func=func,
            category=category,
            feature=feature,
            no_feedback=no_feedback,
            schema_mode=ToolSchemaMode(schema_mode),
            short_description=short_description,
            search_tags=list(search_tags or []),
            risk_level=risk_level,
            examples=list(examples or []),
        )
        _DEFAULT_REGISTRY.append(spec)
        # 保留原函数（便于直接测试调用），并把 spec 挂到属性上
        func.__tool_spec__ = spec  # type: ignore[attr-defined]
        return func

    return decorator


def get_default_specs() -> list[ToolSpec]:
    """获取所有已注册的 ToolSpec（副本）。

    主要用于 ToolRegistry 构造时收集，以及测试用例查询。
    """
    return list(_DEFAULT_REGISTRY)


def clear_default_registry() -> None:
    """清空全局注册表。仅用于测试。"""
    _DEFAULT_REGISTRY.clear()


# ============================================================
# ToolRegistry —— 按配置启用/禁用，提供 schema 和 executor
# ============================================================


class ToolRegistry:
    """工具注册中心。

    构造时传入要启用的 ToolSpec 列表（一般由 build_tool_registry() 按配置筛选）。
    对外提供：
        - get_schemas(): 返回稳定工具 schema 列表
        - get_executor(ctx): 返回可直接传给 AgentRunner.run() 的 executor
        - get_no_feedback_names(): 返回所有 no_feedback=True 的工具名集合（兼容旧调用）
    """

    def __init__(self, specs: list[ToolSpec]) -> None:
        # 用 dict 索引便于按名查找；同时按注册顺序保持稳定（dict 在 3.7+ 保序）
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            if spec.name in self._specs:
                raise ValueError(f"工具名冲突：{spec.name} 被注册了两次")
            self._specs[spec.name] = spec

    def __contains__(self, name: str) -> bool:
        return name in self._specs

    def __len__(self) -> int:
        return len(self._specs)

    def names(self) -> list[str]:
        """返回所有已注册工具名列表。"""
        return list(self._specs.keys())

    def get_spec(self, name: str) -> ToolSpec | None:
        """按名获取工具规格，不存在返回 None。"""
        return self._specs.get(name)

    def get_schemas(self) -> list[dict[str, Any]]:
        """返回所有工具的 OpenAI schema 列表。空时返回 []（不要返回 None）。"""
        return [s.to_openai_schema() for s in self._specs.values()]

    def get_no_feedback_names(self) -> set[str]:
        """返回所有 no_feedback=True 的工具名。

        仅保留给旧调用方做元信息兼容；AgentRunner 不再使用它提前结束。
        """
        return {s.name for s in self._specs.values() if s.no_feedback}

    def get_executor(self, ctx: ToolContext):
        """构造可传给 AgentRunner 的 executor 闭包。

        Executor 签名：async (tool_name: str, args: dict) -> dict
            - 找到 spec → 用 args_model 校验入参 → 调用实现函数
            - 参数校验失败 / 工具不存在 / 工具抛异常 → 返回 {"ok": False, "error": "..."}
        """
        ctx.extras.setdefault("tool_registry", self)
        ctx.extras.setdefault("tool_search_approved_tools", set())

        async def executor(
            tool_name: str,
            raw_args: dict[str, Any],
            *,
            tool_call_id: str | None = None,
        ) -> dict[str, Any]:
            spec = self._specs.get(tool_name)
            if spec is None:
                return {"ok": False, "error": f"unknown tool: {tool_name}"}
            finish_after_success = (
                tool_name != "no_action"
                and raw_args.get("finish_after_success") is True
            )
            if tool_name != "no_action" and "finish_after_success" in raw_args:
                raw_args = dict(raw_args)
                raw_args.pop("finish_after_success", None)
            if (
                spec.schema_mode == ToolSchemaMode.STUB
                and tool_name not in ctx.extras.get("tool_search_approved_tools", set())
            ):
                return {
                    "ok": False,
                    "status": "need_tool_search",
                    "tool_name": tool_name,
                    "brief": f"工具 {tool_name} 需要先查询完整说明。",
                    "next": "请先调用 tool_search 获取参数摘要和风险约束，再决定是否调用。",
                }

            try:
                args = spec.args_model.model_validate(raw_args)
            except Exception as e:
                # Pydantic ValidationError 详情比较长，简化输出
                logger.warning(f"工具 {tool_name} 参数校验失败: {e}")
                return {"ok": False, "error": f"参数无效: {e}"}

            try:
                old_tool_call_id = ctx.extras.get("tool_call_id")
                if tool_call_id:
                    ctx.extras["tool_call_id"] = tool_call_id
                result = await spec.func(args, ctx)
            except Exception as e:
                logger.exception(f"工具 {tool_name} 执行异常: {e}")
                return {"ok": False, "error": str(e)}
            finally:
                if tool_call_id:
                    if old_tool_call_id is None:
                        ctx.extras.pop("tool_call_id", None)
                    else:
                        ctx.extras["tool_call_id"] = old_tool_call_id

            if not isinstance(result, dict):
                # 工具实现应统一返回 dict（含 ok 字段）；非 dict 兜底为成功，
                # 但记 warning 让开发者发现并修复
                logger.warning(
                    f"工具 {tool_name} 返回了非 dict 类型 {type(result).__name__}，"
                    f"已兜底为 ok=True；请修工具实现"
                )
                result = {"ok": True, "value": result}

            from .result_shrink import shrink_tool_result

            shrunk = shrink_tool_result(tool_name, result, ctx)
            if finish_after_success and not _tool_result_blocks_completion(shrunk):
                shrunk = dict(shrunk)
                completion = dict(shrunk.get("turn_completion") or {})
                completion["allowed"] = True
                completion.setdefault("reason", "finish_after_success")
                shrunk["turn_completion"] = completion
            return shrunk

        return executor


def _tool_constraints(spec: ToolSpec) -> list[str]:
    constraints = ["按 tool_search 返回的参数摘要/parameters_schema 填写真实参数；不要编造 ID、路径或目标。"]
    if spec.schema_mode == ToolSchemaMode.STUB:
        constraints.append("这是 stub 工具，本轮已查询后才可调用。")
    if spec.risk_level == "high":
        constraints.append("高风险工具：只有用户明确要求、目标明确、上下文清楚时才可调用。")
    elif spec.risk_level == "medium":
        constraints.append("中等风险工具：调用前确认目标、范围和后果。")
    if spec.category in {"messaging", "platform"}:
        constraints.append("QQ 操作类工具只按明确上下文执行，不因玩笑或情绪话误触发。")
    return constraints


def _summarize_parameter_fields(schema: dict[str, Any]) -> list[dict[str, Any]]:
    required = {str(value) for value in schema.get("required", [])}
    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):
        return []
    return [
        _summarize_schema_field(name, field_schema, required=name in required, depth=0)
        for name, field_schema in properties.items()
        if isinstance(field_schema, dict)
    ]


def _summarize_schema_field(
    name: str,
    schema: dict[str, Any],
    *,
    required: bool,
    depth: int,
) -> dict[str, Any]:
    field: dict[str, Any] = {
        "name": name,
        "required": required,
        "type": _schema_type_summary(schema),
    }
    if "description" in schema:
        field["description"] = schema["description"]
    if "default" in schema:
        field["default"] = schema["default"]
    enum_values = _schema_enum_summary(schema)
    if enum_values is not None:
        field["enum"] = enum_values
    constraints = _schema_constraints_summary(schema)
    if constraints:
        field["constraints"] = constraints

    if depth >= 3:
        return field

    variants = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(variants, list):
        nested_variants = [
            _summarize_schema_node(variant, depth=depth + 1)
            for variant in variants
            if isinstance(variant, dict) and variant.get("type") != "null"
        ]
        if nested_variants:
            field["variants"] = nested_variants

    if schema.get("type") == "object" and isinstance(schema.get("properties"), dict):
        child_required = {str(value) for value in schema.get("required", [])}
        field["fields"] = [
            _summarize_schema_field(
                child_name,
                child_schema,
                required=child_name in child_required,
                depth=depth + 1,
            )
            for child_name, child_schema in schema["properties"].items()
            if isinstance(child_schema, dict)
        ]
    if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        field["items"] = _summarize_schema_node(schema["items"], depth=depth + 1)
    return field


def _summarize_schema_node(schema: dict[str, Any], *, depth: int) -> dict[str, Any]:
    summary: dict[str, Any] = {"type": _schema_type_summary(schema)}
    if "description" in schema:
        summary["description"] = schema["description"]
    enum_values = _schema_enum_summary(schema)
    if enum_values is not None:
        summary["enum"] = enum_values
    constraints = _schema_constraints_summary(schema)
    if constraints:
        summary["constraints"] = constraints
    if depth >= 3:
        return summary
    if schema.get("type") == "object" and isinstance(schema.get("properties"), dict):
        required = {str(value) for value in schema.get("required", [])}
        summary["fields"] = [
            _summarize_schema_field(
                name,
                child_schema,
                required=name in required,
                depth=depth + 1,
            )
            for name, child_schema in schema["properties"].items()
            if isinstance(child_schema, dict)
        ]
    if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        summary["items"] = _summarize_schema_node(schema["items"], depth=depth + 1)
    return summary


def _schema_type_summary(schema: dict[str, Any]) -> str:
    schema_type = schema.get("type")
    if isinstance(schema_type, str):
        return schema_type
    if isinstance(schema_type, list):
        return "|".join(str(item) for item in schema_type)
    variants = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(variants, list):
        types = [
            _schema_type_summary(variant)
            for variant in variants
            if isinstance(variant, dict)
        ]
        deduped = []
        for item in types:
            if item and item not in deduped:
                deduped.append(item)
        if deduped:
            return "|".join(deduped)
    return "unknown"


def _schema_enum_summary(schema: dict[str, Any]) -> list[Any] | None:
    enum_values = schema.get("enum")
    if isinstance(enum_values, list):
        return list(enum_values)
    variants = schema.get("anyOf") or schema.get("oneOf")
    if not isinstance(variants, list):
        return None
    merged: list[Any] = []
    for variant in variants:
        if not isinstance(variant, dict) or not isinstance(variant.get("enum"), list):
            continue
        for value in variant["enum"]:
            if value not in merged:
                merged.append(value)
    return merged or None


def _schema_constraints_summary(schema: dict[str, Any]) -> dict[str, Any]:
    constraint_keys = (
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
    )
    constraints = {
        key: schema[key]
        for key in constraint_keys
        if key in schema
    }
    if schema.get("type") == "array" and "items" in schema:
        constraints["items"] = _schema_type_summary(schema["items"])
    return constraints


def _add_finish_after_success_parameter(
    schema: dict[str, Any],
    tool_name: str,
) -> dict[str, Any]:
    if tool_name == "no_action":
        return schema
    patched = dict(schema)
    properties = dict(patched.get("properties") or {})
    properties.setdefault(
        "finish_after_success",
        {
            "type": "boolean",
            "default": False,
            "description": (
                "仅当这个工具成功且没有失败/待处理状态时，允许本轮工具循环在成功后结束。"
                "不确定是否还需要根据工具结果继续判断时保持 false。"
            ),
        },
    )
    patched["properties"] = properties
    return patched


def _tool_result_blocks_completion(result: dict[str, Any]) -> bool:
    if result.get("ok") is False:
        return True
    if result.get("errors"):
        return True
    status = result.get("status")
    pending_statuses = {
        "needs_review",
        "needs_review_again",
        "stale",
        "failed",
        "partial",
        "unsupported",
        "need_tool_search",
    }
    if isinstance(status, str):
        return status in pending_statuses
    if isinstance(status, (list, tuple, set)):
        return any(str(item) in pending_statuses for item in status)
    return False


# ============================================================
# Pydantic schema 后处理：内联 $ref / 清理 title
# ============================================================


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """把 Pydantic 生成的 $ref / $defs 全部内联。

    OpenAI tool schema 不支持 $ref，必须把所有引用展开成内联对象。
    """
    defs = schema.get("$defs", {})
    if not defs:
        # 没有 $defs 直接返回（深拷贝避免外部修改）
        return dict(schema)

    def _resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node and isinstance(node["$ref"], str):
                ref = node["$ref"]
                # Pydantic 生成的 ref 形如 "#/$defs/SomeModel"
                if ref.startswith("#/$defs/"):
                    key = ref.split("/")[-1]
                    target = defs.get(key)
                    if target is not None:
                        return _resolve(target)
                # 不可解析的 ref：原样保留，由调用方处理
            return {k: _resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [_resolve(v) for v in node]
        return node

    return _resolve({k: v for k, v in schema.items() if k != "$defs"})


def _strip_pydantic_metadata(schema: dict[str, Any]) -> dict[str, Any]:
    """递归移除 Pydantic 添加但 OpenAI 不需要的字段。

    主要清理：
        - title（顶层和嵌套）
        - additionalProperties=False 改为不带（OpenAI 默认接受，避免与某些 SDK 不兼容）

    保留：type / properties / required / description / enum / default / items / anyOf / oneOf
    """
    if isinstance(schema, dict):
        cleaned = {}
        for k, v in schema.items():
            if k == "title":
                continue
            cleaned[k] = _strip_pydantic_metadata(v)
        return cleaned
    if isinstance(schema, list):
        return [_strip_pydantic_metadata(v) for v in schema]
    return schema


# ============================================================
# 辅助：常用 no_feedback 工具默认集合（兼容旧导出，不参与 runner 结束）
# ============================================================


DEFAULT_NO_FEEDBACK_TOOLS: set[str] = {
    "save_important_memory",
    "update_important_memory",
    "delete_important_memory",
    "no_action",
    "send_poke",
    "set_msg_emoji_like",
    "set_friend_add_request",
    "set_group_add_request",
    "schedule_wakeup",
}
"""与 agents.runner.DEFAULT_NO_FEEDBACK_TOOLS 对齐，仅作兼容元信息。"""
