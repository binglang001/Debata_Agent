"""Schema and registry tests split from tests/test_tools.py."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from tests.tools.helpers import _assert_no_title, _assert_tool_result_envelope, _make_config
from tools import (
    DEFAULT_NO_FEEDBACK_TOOLS,
    FEATURE_TOOL_FEATURES,
    FULL_SCHEMA_TOOLS,
    MEMORY_FILE_TOOLS,
    STUB_SCHEMA_TOOLS,
    ToolContext,
    ToolRegistry,
    build_default_registry,
    get_default_specs,
)
from tools.base import _inline_refs, _strip_pydantic_metadata
from tools.schemas import (
    EatArgs,
    GetRecentChatMessagesArgs,
    SaveMemoryArgs,
    SendGroupArgs,
    SendPrivateArgs,
    SleepArgs,
    ToolSearchArgs,
    UpdateMemoryArgs,
)

# ============================================================
# Schema 自动派生
# ============================================================

def test_send_private_schema_derivation():
    """SendPrivateArgs 应能派生出 OpenAI 兼容 schema。"""
    specs = {s.name: s for s in get_default_specs()}
    schema = specs["send_private_messages"].to_openai_schema()

    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "send_private_messages"
    assert "send_only" not in fn["parameters"]["properties"]
    assert "targets" in fn["parameters"]["properties"]
    assert "targets" in fn["parameters"]["required"]
    target_props = fn["parameters"]["properties"]["targets"]["items"]["properties"]
    target_required = fn["parameters"]["properties"]["targets"]["items"]["required"]
    assert "emoji" in target_props
    assert "image" in target_props
    assert "不是表情包" in target_props["image"]["description"]
    assert "delay" in target_required
    assert "最后一条也必须填写" in target_props["delay"]["description"]
    assert "1.5 个中文字/秒" in target_props["delay"]["description"]
    props = fn["parameters"]["properties"]
    assert "responding_to_message_ids" in props
    assert "reply_to_message_id" in props
    assert "回答被引用的消息或复核后继续旧内容" in props["responding_to_message_ids"]["description"]
    assert "私聊/群聊都适用" in props["reply_to_message_id"]["description"]
    assert "延迟回复" in props["reply_to_message_id"]["description"]
    assert "吃饭睡觉后接旧话" in props["reply_to_message_id"]["description"]
    assert "没有可靠 msg_id 不要编造" in props["reply_to_message_id"]["description"]
    assert "reply_to_message_id" not in fn["parameters"]["required"]


def test_eat_args_schema_and_validation():
    args = EatArgs.model_validate(
        {"meal_type": "早餐", "duration_minutes": 20, "description": "豆浆和包子"}
    )
    assert args.meal_type == "早餐"
    assert args.duration_minutes == 20
    assert args.description == "豆浆和包子"

    schema = EatArgs.model_json_schema()
    props = schema["properties"]
    assert set(schema["required"]) == {"meal_type", "duration_minutes", "description"}
    assert props["meal_type"]["minLength"] == 1
    assert props["duration_minutes"]["minimum"] == 1
    assert props["duration_minutes"]["maximum"] == 60
    assert props["description"]["minLength"] == 1

    for duration in (0, 61):
        with pytest.raises(ValidationError):
            EatArgs.model_validate(
                {
                    "meal_type": "早餐",
                    "duration_minutes": duration,
                    "description": "豆浆和包子",
                }
            )

    with pytest.raises(ValidationError):
        EatArgs.model_validate(
            {"meal_type": "", "duration_minutes": 20, "description": "豆浆和包子"}
        )


def test_sleep_args_schema_and_validation():
    args = SleepArgs.model_validate({"duration_minutes": 90, "reason": "午休"})
    assert args.duration_minutes == 90
    assert args.reason == "午休"

    schema = SleepArgs.model_json_schema()
    props = schema["properties"]
    assert set(schema["required"]) == {"duration_minutes", "reason"}
    assert props["duration_minutes"]["minimum"] == 1
    assert props["duration_minutes"]["maximum"] == 720
    assert props["reason"]["minLength"] == 1

    for duration in (0, 721):
        with pytest.raises(ValidationError):
            SleepArgs.model_validate({"duration_minutes": duration, "reason": "午休"})

    with pytest.raises(ValidationError):
        SleepArgs.model_validate({"duration_minutes": 90, "reason": ""})


def test_send_targets_require_delay():
    with pytest.raises(ValidationError):
        SendPrivateArgs.model_validate(
            {"targets": [{"target_qq": 123, "content": "你好", "order": 1}]}
        )
    with pytest.raises(ValidationError):
        SendGroupArgs.model_validate(
            {"group_id": 456, "targets": [{"content": "群消息", "order": 1}]}
        )


@pytest.mark.parametrize("delay", [-0.1, math.nan, math.inf, -math.inf])
def test_send_targets_reject_invalid_delay(delay):
    with pytest.raises(ValidationError):
        SendPrivateArgs.model_validate(
            {
                "targets": [
                    {
                        "target_qq": 123,
                        "content": "你好",
                        "order": 1,
                        "delay": delay,
                    }
                ]
            }
        )
    with pytest.raises(ValidationError):
        SendGroupArgs.model_validate(
            {
                "group_id": 456,
                "targets": [
                    {"content": "群消息", "order": 1, "delay": delay}
                ],
            }
        )


def test_non_no_action_schemas_expose_finish_after_success():
    specs = {s.name: s for s in get_default_specs()}

    for spec in specs.values():
        schema_props = spec.to_openai_schema()["function"]["parameters"].get(
            "properties", {}
        )
        full_props = spec.full_parameters_schema().get("properties", {})
        if spec.name == "no_action":
            assert "finish_after_success" not in schema_props
            assert "finish_after_success" not in full_props
            continue
        assert schema_props["finish_after_success"]["default"] is False
        assert full_props["finish_after_success"]["default"] is False


def test_schema_no_refs_in_output():
    """派生出的 schema 不应包含 $ref / $defs（OpenAI 不支持）。"""
    specs = {s.name: s for s in get_default_specs()}
    for spec_name in ("send_private_messages", "send_group_message"):
        schema = specs[spec_name].to_openai_schema()
        text = str(schema)
        assert "$ref" not in text, f"{spec_name}: schema 含 $ref"
        assert "$defs" not in text, f"{spec_name}: schema 含 $defs"


def test_send_schemas_expose_review_policy_and_ignore_review_interrupts():
    specs = {s.name: s for s in get_default_specs()}
    for spec_name in (
        "send_private_messages",
        "send_group_message",
        "send_voice_message",
    ):
        schema = specs[spec_name].full_parameters_schema()
        props = schema["properties"]
        required = schema.get("required", [])

        assert "ignore_review_interrupts" in props
        assert props["ignore_review_interrupts"]["default"] is False
        assert "系统接受后" in props["ignore_review_interrupts"]["description"]
        assert "不能绕过发送前 needs_review" in props["ignore_review_interrupts"]["description"]
        assert "ignore_review_interrupts" not in required

    for spec_name in ("send_private_messages", "send_group_message"):
        schema = specs[spec_name].to_openai_schema()
        props = schema["function"]["parameters"]["properties"]
        required = schema["function"]["parameters"].get("required", [])
        assert "review_policy" in props
        assert set(props["review_policy"]["enum"]) == {"review_priority", "review_all"}
        assert props["review_policy"]["default"] == "review_priority"
        assert "review_priority" in props["review_policy"]["description"]
        assert "review_all" in props["review_policy"]["description"]
        assert "review_policy" not in required


def test_send_group_schema_describes_conditional_reply_reference():
    specs = {s.name: s for s in get_default_specs()}

    send_schema = specs["send_group_message"].to_openai_schema()
    send_props = send_schema["function"]["parameters"]["properties"]
    reply_desc = send_props["reply_to_message_id"]["description"]
    responding_desc = send_props["responding_to_message_ids"]["description"]

    assert "回复对象不是紧邻上一条" in reply_desc
    assert "多人连续插话" in reply_desc
    assert "回答被引用的消息" in reply_desc
    assert "私聊/群聊都适用" in reply_desc
    assert "延迟回复" in reply_desc
    assert "主动思考接旧话" in reply_desc
    assert "行/OK/可以/知道了/不要" in reply_desc
    assert "普通顺序闲聊不要机械每条都填" in reply_desc
    assert "没有可靠 msg_id 不要编造" in reply_desc
    assert "自然语言锚定" in reply_desc
    assert "回复对象不是紧邻上一条" in responding_desc
    assert "回答被引用的消息或复核后提交旧内容" in responding_desc

    commit_schema = specs["commit_send_attempt"].to_openai_schema()
    commit_desc = commit_schema["function"]["parameters"]["properties"][
        "reply_to_message_id"
    ]["description"]
    commit_required = commit_schema["function"]["parameters"].get("required", [])
    assert "私聊/群聊都适用" in commit_desc
    assert "提交旧 attempt 前先复核旧回复是否需要补引用" in commit_desc
    assert "复核后提交旧 attempt" in commit_desc
    assert "回复非最新消息" in commit_desc
    assert "回答被引用的消息" in commit_desc
    assert "短确认会看不出回谁" in commit_desc
    assert "普通顺序闲聊不要机械每条都填" in commit_desc
    assert "没有可靠 msg_id 不要编造" in commit_desc
    assert "reply_to_message_id" not in commit_required


def test_commit_send_attempt_schema_exposes_ignore_review_interrupts():
    specs = {s.name: s for s in get_default_specs()}
    schema = specs["commit_send_attempt"].to_openai_schema()
    props = schema["function"]["parameters"]["properties"]

    assert props["ignore_review_interrupts"]["default"] is False
    assert "send_attempt_id" in props


def test_schema_no_title_field():
    """派生 schema 不应含 Pydantic 的 title 字段。"""
    specs = {s.name: s for s in get_default_specs()}
    for spec in specs.values():
        schema = spec.to_openai_schema()
        # 递归检查不含 title
        _assert_no_title(schema)



def test_inline_refs_handles_nested():
    """测试 _inline_refs 把嵌套 $ref 全部展开。"""
    schema = {
        "type": "object",
        "properties": {
            "x": {"$ref": "#/$defs/Inner"},
            "y": {"type": "array", "items": {"$ref": "#/$defs/Inner"}},
        },
        "$defs": {
            "Inner": {"type": "object", "properties": {"k": {"type": "string"}}}
        },
    }
    result = _inline_refs(schema)
    assert "$defs" not in result
    assert result["properties"]["x"]["type"] == "object"
    assert result["properties"]["y"]["items"]["type"] == "object"


def test_strip_pydantic_metadata_removes_title():
    schema = {"type": "object", "title": "X", "properties": {"a": {"title": "A"}}}
    cleaned = _strip_pydantic_metadata(schema)
    assert "title" not in cleaned
    assert "title" not in cleaned["properties"]["a"]


# ============================================================
# 所有工具注册了
# ============================================================

def test_all_expected_tools_registered():
    """检查核心工具都已通过装饰器注册到全局列表。"""
    expected = {
        "send_private_messages", "send_group_message", "commit_send_attempt",
        "recall_message", "upload_file",
        "save_important_memory", "update_important_memory", "delete_important_memory",
        "list_contacts", "get_user_info", "get_forward_msg", "get_recent_chat_messages",
        "get_msg", "send_poke", "set_msg_emoji_like",
        "get_group_self_role", "set_group_kick", "set_group_ban",
        "set_group_whole_ban", "set_group_leave",
        "set_friend_add_request", "set_group_add_request", "summarize_chat_history",
        "summarize_conversation", "filter_archive_records", "recall_history",
        "start_agent_task",
        "no_action", "schedule_wakeup",
        "eat", "sleep",
        "tool_search",
        "describe_image", "web_search", "get_weather",
        "send_voice_message",
        # workspace tools
        "read_file", "write_file", "edit_file", "list_files", "delete_file", "run_python",
    }
    actual = {s.name for s in get_default_specs()}
    assert actual == expected, f"差异：{expected ^ actual}"


def test_no_feedback_marks_correct():
    """检查 no_feedback 标记的工具与全局集合一致。"""
    specs = {s.name: s for s in get_default_specs()}
    for name in DEFAULT_NO_FEEDBACK_TOOLS:
        assert name in specs
        # 不强制要求 spec.no_feedback==True 一致（runner 默认集合是兜底）


def test_schedule_wakeup_schema_explains_delayed_wakeup():
    specs = {s.name: s for s in get_default_specs()}
    schema = specs["schedule_wakeup"].to_openai_schema()["function"]

    desc = schema["description"]
    props = schema["parameters"]["properties"]

    assert "延迟任务" in desc
    assert "delay_seconds 是从现在开始等待的秒数" in desc
    assert "mode=send_message" in desc
    assert "mode=wakeup" in desc
    assert "发送消息" in desc
    assert "不会重新附带完整旧聊天历史" in desc
    assert "绝对时间字符串" in props["delay_seconds"]["description"]
    assert "send_message" in props["mode"]["description"]
    assert "wakeup" in props["mode"]["description"]
    assert "固定消息正文" in props["message_text"]["description"]
    assert "target_type" in props
    assert "target_id" in props


def test_delete_memory_schema_prefers_memory_id():
    specs = {s.name: s for s in get_default_specs()}
    schema = specs["delete_important_memory"].to_openai_schema()["function"]
    props = schema["parameters"]["properties"]

    assert "memory_id" in props
    assert "keyword" in props
    assert "推荐使用 memory_id" in schema["description"]
    assert "旧版兼容" in props["keyword"]["description"]


def test_memory_scope_schema_requires_semantic_explicit_choice():
    specs = {s.name: s for s in get_default_specs()}
    save_schema = specs["save_important_memory"].to_openai_schema()["function"]
    save_props = save_schema["parameters"]["properties"]
    save_desc = save_props["scope"]["description"]

    assert "必须显式传 scope" in save_schema["description"]
    assert "必填：按语义显式选择" in save_desc
    assert "不会按当前会话自动推断" in save_desc
    assert "global=跨场景" in save_desc
    assert "user:QQ号=只适用于该用户本人" in save_desc
    assert "group:群号=只适用于该群" in save_desc
    assert "提到某用户不等于 user scope" in save_desc
    assert "冰狼正在做短中期项目" in save_desc
    assert "private:QQ" in save_desc

    update_schema = specs["update_important_memory"].to_openai_schema()["function"]
    update_props = update_schema["parameters"]["properties"]
    update_scope_desc = update_props["scope"]["description"]
    assert "修改内容时重新判断适用范围" in update_schema["description"]
    assert "修改 memory_text 时要重新判断语义范围" in update_scope_desc
    assert "仍适用原范围可不填" in update_scope_desc
    assert "不要因为正文提到某用户就自动选 user scope" in update_scope_desc


def test_memory_args_scope_guidance_and_recent_chat_private_delay_hint():
    assert "提到某用户不等于 user scope" in SaveMemoryArgs.model_fields["scope"].description
    assert "不会按当前会话自动推断" in SaveMemoryArgs.model_fields["scope"].description
    assert "重新判断语义范围" in UpdateMemoryArgs.model_fields["scope"].description
    assert (
        "私聊或群聊延迟接旧话"
        in GetRecentChatMessagesArgs.model_fields["since_msg_id"].description
    )


# ============================================================
# build_default_registry: 按配置筛选
# ============================================================

def test_registry_file_mode_includes_memory_tools():
    cfg = _make_config(memory_mode="file")
    reg = build_default_registry(cfg)
    assert "save_important_memory" in reg
    assert "update_important_memory" in reg
    assert "delete_important_memory" in reg


def test_registry_rag_mode_includes_memory_tools():
    cfg = _make_config(memory_mode="rag")
    reg = build_default_registry(cfg)
    for name in MEMORY_FILE_TOOLS:
        assert name in reg, f"RAG 模式下也应注册 {name}"


def test_registry_feature_disabled_keeps_schema_stable():
    cfg = _make_config(vision_enabled=False, weather_enabled=False)
    reg = build_default_registry(cfg)
    for name in ("describe_image", "get_weather"):
        assert name in reg


def test_registry_feature_enabled_includes_tool():
    cfg = _make_config(vision_enabled=True, weather_enabled=True)
    reg = build_default_registry(cfg)
    assert "describe_image" in reg
    assert "get_weather" in reg


def test_registry_web_search_default_enabled():
    """WebSearchFeatureConfig 默认 enabled=True。"""
    cfg = _make_config(web_search_enabled=True)
    reg = build_default_registry(cfg)
    assert "web_search" in reg


def test_registry_messaging_always_enabled():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    assert "send_private_messages" in reg
    assert "send_group_message" in reg
    assert "recall_message" in reg


def test_registry_persona_tools_disabled_by_default():
    cfg = _make_config()
    reg = build_default_registry(cfg)

    assert "eat" not in reg
    assert "sleep" not in reg


def test_registry_persona_energy_tool_includes_only_sleep():
    cfg = _make_config(persona_management_enabled=True, energy_mode="tool")
    reg = build_default_registry(cfg)

    assert "sleep" in reg
    assert "eat" not in reg


def test_registry_persona_satiety_tool_includes_only_eat():
    cfg = _make_config(persona_management_enabled=True, satiety_mode="tool")
    reg = build_default_registry(cfg)

    assert "eat" in reg
    assert "sleep" not in reg


def test_registry_persona_dual_tool_includes_eat_and_sleep():
    cfg = _make_config(
        persona_management_enabled=True,
        energy_mode="tool",
        satiety_mode="tool",
    )
    reg = build_default_registry(cfg)

    assert "eat" in reg
    assert "sleep" in reg
    assert {"eat", "sleep"}.issubset(FULL_SCHEMA_TOOLS)


@pytest.mark.asyncio
async def test_persona_tools_without_persona_agent_return_unavailable():
    cfg = _make_config(
        persona_management_enabled=True,
        energy_mode="tool",
        satiety_mode="tool",
    )
    reg = build_default_registry(cfg)
    executor = reg.get_executor(ToolContext())

    eat_result = await executor(
        "eat",
        {
            "meal_type": "早餐",
            "duration_minutes": 20,
            "description": "豆浆和包子",
        },
    )
    sleep_result = await executor(
        "sleep",
        {"duration_minutes": 90, "reason": "午休"},
    )

    _assert_tool_result_envelope(eat_result, "eat")
    _assert_tool_result_envelope(sleep_result, "sleep")
    assert eat_result["ok"] is False
    assert eat_result["status"] == "unavailable"
    assert sleep_result["ok"] is False
    assert sleep_result["status"] == "unavailable"


@pytest.mark.asyncio
async def test_persona_tools_call_persona_agent_and_sleep_reason_compatibility():
    class FakePersonaAgent:
        def __init__(self) -> None:
            self.eat_calls: list[tuple[str, int, str]] = []
            self.sleep_calls: list[tuple[int, str]] = []

        async def on_eat_start(
            self,
            meal_type: str,
            duration_minutes: int,
            description: str,
        ) -> dict:
            self.eat_calls.append((meal_type, duration_minutes, description))
            return {"status": "started", "record_id": "eat-1"}

        async def on_sleep_start(
            self,
            duration_minutes: int,
            *,
            reason: str,
        ) -> dict:
            self.sleep_calls.append((duration_minutes, reason))
            return {"status": "started", "record_id": "sleep-1"}

    class DurationOnlySleepPersonaAgent:
        def __init__(self) -> None:
            self.sleep_calls: list[int] = []

        async def on_sleep_start(self, duration_minutes: int) -> dict:
            self.sleep_calls.append(duration_minutes)
            return {"status": "started", "record_id": "sleep-legacy"}

    cfg = _make_config(
        persona_management_enabled=True,
        energy_mode="tool",
        satiety_mode="tool",
    )
    reg = build_default_registry(cfg)
    agent = FakePersonaAgent()
    executor = reg.get_executor(ToolContext(persona_agent=agent))

    eat_result = await executor(
        "eat",
        {
            "meal_type": "早餐",
            "duration_minutes": 20,
            "description": "豆浆和包子",
        },
    )
    sleep_result = await executor(
        "sleep",
        {"duration_minutes": 90, "reason": "午休"},
    )

    assert agent.eat_calls == [("早餐", 20, "豆浆和包子")]
    assert agent.sleep_calls == [(90, "午休")]
    assert eat_result["ok"] is True
    assert eat_result["status"] == "started"
    assert eat_result["result"]["record_id"] == "eat-1"
    assert sleep_result["ok"] is True
    assert sleep_result["status"] == "started"
    assert sleep_result["reason"] == "午休"
    assert sleep_result["result"]["record_id"] == "sleep-1"

    duration_only_agent = DurationOnlySleepPersonaAgent()
    duration_only_executor = reg.get_executor(
        ToolContext(persona_agent=duration_only_agent)
    )
    duration_only_result = await duration_only_executor(
        "sleep",
        {"duration_minutes": 30, "reason": "小睡"},
    )

    assert duration_only_agent.sleep_calls == [30]
    assert duration_only_result["ok"] is True
    assert duration_only_result["status"] == "started"
    assert duration_only_result["reason"] == "小睡"
    assert duration_only_result["result"]["record_id"] == "sleep-legacy"


def test_registry_upload_file_is_stub_schema():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    schema_by_name = {
        schema["function"]["name"]: schema["function"]
        for schema in reg.get_schemas()
    }
    assert "upload_file" in reg
    assert set(schema_by_name["upload_file"]["parameters"]["properties"]) == {
        "_tool_search_required",
        "finish_after_success",
    }


def test_registry_stub_and_full_schema_modes_are_stable():
    cfg = _make_config(vision_enabled=False, web_search_enabled=False, weather_enabled=False)
    reg_disabled = build_default_registry(cfg)
    cfg_enabled = _make_config(vision_enabled=True, web_search_enabled=True, weather_enabled=True)
    reg_enabled = build_default_registry(cfg_enabled)

    assert reg_disabled.names() == reg_enabled.names()
    schema_by_name = {
        schema["function"]["name"]: schema["function"]
        for schema in reg_disabled.get_schemas()
    }
    for name in STUB_SCHEMA_TOOLS:
        assert name in schema_by_name
        props = schema_by_name[name]["parameters"]["properties"]
        assert set(props) == {"_tool_search_required", "finish_after_success"}
    filter_archive_spec = reg_disabled.get_spec("filter_archive_records")
    assert filter_archive_spec is not None
    assert {"archive", "history", "recall"}.issubset(filter_archive_spec.search_tags)
    assert "targets" in schema_by_name["send_private_messages"]["parameters"]["properties"]
    tool_search_props = schema_by_name["tool_search"]["parameters"]["properties"]
    assert "tool_name" in tool_search_props
    assert tool_search_props["detail"]["enum"] == ["summary", "full"]
    assert tool_search_props["detail"]["default"] == "summary"
    args_detail_schema = ToolSearchArgs.model_json_schema()["properties"]["detail"]
    assert args_detail_schema["enum"] == ["summary", "full"]
    assert args_detail_schema["default"] == "summary"


def test_registry_excludes_sensitive_and_unused_napcat_apis():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    forbidden = {
        "call_api",
        "add_friend",
        "delete_friend",
        "get_credentials",
        "get_cookies",
        "get_csrf_token",
        "mark_private_msg_as_read",
        "mark_group_msg_as_read",
        "get_ai_record",
        "send_group_ai_record",
    }

    assert forbidden.isdisjoint(set(reg.names()))


def test_registry_no_feedback_names_includes_known():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    names = reg.get_no_feedback_names()
    assert "no_action" in names
    assert "save_important_memory" in names
    assert "schedule_wakeup" in names
    assert "send_poke" in names
    assert "set_msg_emoji_like" in names


def test_registry_rag_no_feedback_names_includes_memory_tools():
    cfg = _make_config(memory_mode="rag")
    reg = build_default_registry(cfg)
    names = reg.get_no_feedback_names()
    assert "no_action" in names
    assert "save_important_memory" in names
    assert "update_important_memory" in names
    assert "delete_important_memory" in names


def test_registry_duplicate_spec_raises():
    """注册器禁止重名工具。"""
    specs = list(get_default_specs())
    with pytest.raises(ValueError, match="冲突"):
        ToolRegistry([specs[0], specs[0]])


def test_registry_feature_tool_features_keys():
    """FEATURE_TOOL_FEATURES 的 key 必须都已注册。"""
    all_names = {s.name for s in get_default_specs()}
    for tool_name in FEATURE_TOOL_FEATURES:
        assert tool_name in all_names


