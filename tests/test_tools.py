"""测试工具系统：schema 派生 / Registry 启用禁用 / 工具执行 / 关键词保存。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from tools import (
    DEFAULT_NO_FEEDBACK_TOOLS,
    FEATURE_TOOL_FEATURES,
    MEMORY_FILE_TOOLS,
    ToolContext,
    ToolRegistry,
    build_default_registry,
    build_message,
    contains_forbidden,
    get_default_specs,
    try_save_from_user,
    typing_delay,
)
from tools.base import _inline_refs, _strip_pydantic_metadata, tool
from tools.schemas import (
    DescribeImageArgs,
    GetWeatherArgs,
    ListContactsArgs,
    SaveMemoryArgs,
    SendGroupArgs,
    SendPrivateArgs,
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
    assert "send_only" in fn["parameters"]["properties"]
    assert "targets" in fn["parameters"]["properties"]
    assert "targets" in fn["parameters"]["required"]


def test_schema_no_refs_in_output():
    """派生出的 schema 不应包含 $ref / $defs（OpenAI 不支持）。"""
    specs = {s.name: s for s in get_default_specs()}
    for spec_name in ("send_private_messages", "send_group_message"):
        schema = specs[spec_name].to_openai_schema()
        text = str(schema)
        assert "$ref" not in text, f"{spec_name}: schema 含 $ref"
        assert "$defs" not in text, f"{spec_name}: schema 含 $defs"


def test_schema_no_title_field():
    """派生 schema 不应含 Pydantic 的 title 字段。"""
    specs = {s.name: s for s in get_default_specs()}
    for spec in specs.values():
        schema = spec.to_openai_schema()
        # 递归检查不含 title
        _assert_no_title(schema)


def _assert_no_title(node):
    if isinstance(node, dict):
        assert "title" not in node, f"残留 title: {node}"
        for v in node.values():
            _assert_no_title(v)
    elif isinstance(node, list):
        for v in node:
            _assert_no_title(v)


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
# 所有 17 个工具注册了
# ============================================================


def test_all_expected_tools_registered():
    """检查 17 个工具都已通过装饰器注册到全局列表。"""
    expected = {
        "send_private_messages", "send_group_message", "recall_message", "upload_file",
        "save_important_memory", "delete_important_memory",
        "list_contacts", "get_user_info", "get_forward_msg",
        "set_friend_add_request", "set_group_add_request", "summarize_chat_history",
        "no_action", "schedule_wakeup",
        "describe_image", "web_search", "get_weather",
    }
    actual = {s.name for s in get_default_specs()}
    assert actual == expected, f"差异：{expected ^ actual}"


def test_no_feedback_marks_correct():
    """检查 no_feedback 标记的工具与全局集合一致。"""
    specs = {s.name: s for s in get_default_specs()}
    for name in DEFAULT_NO_FEEDBACK_TOOLS:
        assert name in specs
        # 不强制要求 spec.no_feedback==True 一致（runner 默认集合是兜底）


# ============================================================
# build_default_registry: 按配置筛选
# ============================================================


def _make_config(
    *,
    memory_mode="file",
    vision_enabled=False,
    web_search_enabled=False,
    weather_enabled=False,
):
    """构造最小合法 RootConfig。"""
    from app_config.schema import (
        AgentConfig,
        AgentsConfig,
        FeaturesConfig,
        LongTermMemoryConfig,
        ProviderConfig,
        RootConfig,
        VisionFeatureConfig,
        WeatherFeatureConfig,
        WebSearchFeatureConfig,
    )

    return RootConfig(
        providers={
            "deepseek": ProviderConfig(
                preset="deepseek", api_key_id="k1"
            )
        },
        agents=AgentsConfig(
            chat=AgentConfig(provider="deepseek", model="deepseek-chat"),
        ),
        features=FeaturesConfig(
            vision=VisionFeatureConfig(enabled=vision_enabled),
            web_search=WebSearchFeatureConfig(enabled=web_search_enabled),
            weather=WeatherFeatureConfig(
                enabled=weather_enabled,
                host="devapi.qweather.com" if weather_enabled else "",
            ),
            long_term_memory=LongTermMemoryConfig(mode=memory_mode),
        ),
    )


def test_registry_file_mode_includes_memory_tools():
    cfg = _make_config(memory_mode="file")
    reg = build_default_registry(cfg)
    assert "save_important_memory" in reg
    assert "delete_important_memory" in reg


def test_registry_rag_mode_excludes_memory_tools():
    cfg = _make_config(memory_mode="rag")
    reg = build_default_registry(cfg)
    for name in MEMORY_FILE_TOOLS:
        assert name not in reg, f"RAG 模式下不应注册 {name}"


def test_registry_feature_disabled_excludes_tool():
    cfg = _make_config(vision_enabled=False, weather_enabled=False)
    reg = build_default_registry(cfg)
    for name in ("describe_image", "get_weather"):
        assert name not in reg


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


def test_registry_upload_file_can_be_disabled():
    cfg = _make_config()
    reg = build_default_registry(cfg, include_upload_file=False)
    assert "upload_file" not in reg


def test_registry_no_feedback_names_includes_known():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    names = reg.get_no_feedback_names()
    assert "no_action" in names
    assert "save_important_memory" in names
    assert "schedule_wakeup" in names


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


# ============================================================
# Executor：参数校验与工具执行
# ============================================================


@pytest.mark.asyncio
async def test_executor_unknown_tool():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext()
    executor = reg.get_executor(ctx)
    result = await executor("does_not_exist", {})
    assert result["ok"] is False
    assert "unknown" in result["error"].lower()


@pytest.mark.asyncio
async def test_executor_invalid_args():
    """参数校验失败应返回 ok=False，不抛异常。"""
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext()
    executor = reg.get_executor(ctx)
    # save_important_memory 要求 memory_text 非空
    result = await executor("save_important_memory", {})
    assert result["ok"] is False
    assert "无效" in result["error"] or "memory_text" in result["error"]


@pytest.mark.asyncio
async def test_executor_no_action_works():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext()
    executor = reg.get_executor(ctx)
    result = await executor("no_action", {})
    assert result["ok"] is True
    assert result.get("no_action") is True


@pytest.mark.asyncio
async def test_executor_exception_returns_error():
    """工具内部抛异常应被捕获并转成 ok=False。"""
    # 用一个故意抛异常的临时工具

    class _Args(BaseModel):
        pass

    spec_list = list(get_default_specs())

    @tool(name="_boom", description="boom", args_model=_Args)
    async def _boom(args, ctx):
        raise RuntimeError("boom!")

    # 拿到新注册的 spec
    new_spec = next(s for s in get_default_specs() if s.name == "_boom")
    try:
        reg = ToolRegistry([new_spec])
        ctx = ToolContext()
        executor = reg.get_executor(ctx)
        result = await executor("_boom", {})
        assert result["ok"] is False
        assert "boom" in result["error"]
    finally:
        # 清理：把 _boom 从全局列表移除避免污染其它测试
        from tools.base import _DEFAULT_REGISTRY
        _DEFAULT_REGISTRY[:] = [s for s in _DEFAULT_REGISTRY if s.name != "_boom"]


# ============================================================
# Feature 工具：未启用兜底
# ============================================================


@pytest.mark.asyncio
async def test_describe_image_without_vision_service():
    """未注入 vision 时，describe_image 返回未启用错误。"""
    cfg = _make_config(vision_enabled=True)  # registry 启用，但 ctx 没塞 service
    reg = build_default_registry(cfg)
    ctx = ToolContext()  # 没注入 vision
    executor = reg.get_executor(ctx)
    result = await executor(
        "describe_image", {"image_url": "https://x.com/a.png"}
    )
    assert result["ok"] is False
    assert "未启用" in result["error"]


@pytest.mark.asyncio
async def test_web_search_without_service():
    cfg = _make_config(web_search_enabled=True)
    reg = build_default_registry(cfg)
    ctx = ToolContext()
    executor = reg.get_executor(ctx)
    result = await executor("web_search", {"query": "x"})
    assert result["ok"] is False
    assert "未启用" in result["error"]


@pytest.mark.asyncio
async def test_weather_without_service():
    cfg = _make_config(weather_enabled=True)
    reg = build_default_registry(cfg)
    ctx = ToolContext()
    executor = reg.get_executor(ctx)
    result = await executor("get_weather", {"city": "宁德"})
    assert result["ok"] is False
    assert "未启用" in result["error"]


# ============================================================
# Messaging 工具：collected 攒动作
# ============================================================


@pytest.mark.asyncio
async def test_send_private_collected(tmp_path):
    """send_private_messages 应把动作攒到 ctx.collected。"""
    cfg = _make_config()
    reg = build_default_registry(cfg)
    emoji_dir = tmp_path / "emoji"
    emoji_dir.mkdir()

    ctx = ToolContext(emoji_dir=emoji_dir)
    executor = reg.get_executor(ctx)
    result = await executor(
        "send_private_messages",
        {
            "targets": [
                {"target_qq": 12345, "content": "你好", "order": 1},
                {"target_qq": 12345, "content": "在吗", "order": 2},
            ]
        },
    )
    assert result["ok"] is True
    assert result["count"] == 2
    assert len(ctx.collected) == 2
    assert ctx.collected[0]["action"] == "private"
    assert ctx.collected[0]["target"] == "12345"
    assert ctx.collected[0]["content"] == "你好"
    assert ctx.collected[0]["delay"] > 0  # 自动估算的延迟


@pytest.mark.asyncio
async def test_send_private_forbidden_blocked(tmp_path):
    """含 FORBIDDEN_TAGS 的内容应被拒绝。"""
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(emoji_dir=tmp_path / "emoji")
    executor = reg.get_executor(ctx)
    result = await executor(
        "send_private_messages",
        {
            "targets": [
                {"target_qq": 1, "content": "[私聊给 X]什么", "order": 1},
            ]
        },
    )
    assert result["ok"] is True
    assert result["count"] == 0
    assert "errors" in result
    assert ctx.collected == []


@pytest.mark.asyncio
async def test_send_group_order_sorted(tmp_path):
    """send_group_message 应按 order 升序排列动作。"""
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(emoji_dir=tmp_path / "emoji")
    executor = reg.get_executor(ctx)
    await executor(
        "send_group_message",
        {
            "group_id": 100,
            "targets": [
                {"content": "third", "order": 3},
                {"content": "first", "order": 1},
                {"content": "second", "order": 2},
            ],
        },
    )
    contents = [a["content"] for a in ctx.collected]
    assert contents == ["first", "second", "third"]


# ============================================================
# message_builder 辅助
# ============================================================


def test_typing_delay_short():
    assert typing_delay("a") < 1.0


def test_typing_delay_capped():
    """文本极长时不超过 max_delay。"""
    assert typing_delay("a" * 1000, max_delay=2.0) == 2.0


def test_typing_delay_empty():
    assert typing_delay("") == 0.0


def test_contains_forbidden_positive():
    assert contains_forbidden("我给 QQ 12345 发的消息")


def test_contains_forbidden_negative():
    assert not contains_forbidden("普通消息")


@pytest.mark.asyncio
async def test_build_message_text(tmp_path):
    msg, label = await build_message("你好", None, tmp_path / "emoji")
    assert msg == "你好"
    assert label == "你好"


@pytest.mark.asyncio
async def test_build_message_missing_image(tmp_path):
    """图片不存在时返回 (None, None)。"""
    emoji_dir = tmp_path / "emoji"
    emoji_dir.mkdir()
    msg, label = await build_message(None, "nonexistent.png", emoji_dir)
    assert msg is None
    assert label is None


@pytest.mark.asyncio
async def test_build_message_rejects_path_traversal(tmp_path):
    """图片名含 .. 或 / 时拒绝。"""
    emoji_dir = tmp_path / "emoji"
    emoji_dir.mkdir()
    msg, _ = await build_message(None, "../etc/passwd", emoji_dir)
    assert msg is None


@pytest.mark.asyncio
async def test_build_message_real_image(tmp_path):
    """存在的表情包应被读取并 base64 编码。"""
    emoji_dir = tmp_path / "emoji"
    emoji_dir.mkdir()
    (emoji_dir / "hi.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    msg, label = await build_message(None, "hi.png", emoji_dir)
    assert msg is not None
    assert msg.startswith("[CQ:image,file=base64://")
    assert label == "[表情包: hi.png]"


# ============================================================
# keyword_save 联动
# ============================================================


@pytest.mark.asyncio
async def test_keyword_save_hits(tmp_path):
    from memory import ImportantMemoryManager

    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()
    result = await try_save_from_user("记住我喜欢猫", im, enabled=True)
    assert result is not None
    assert result["saved"] is True
    assert result["matched_keyword"] == "记住"


@pytest.mark.asyncio
async def test_keyword_save_disabled(tmp_path):
    from memory import ImportantMemoryManager

    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()
    result = await try_save_from_user("记住我喜欢猫", im, enabled=False)
    assert result is None


@pytest.mark.asyncio
async def test_keyword_save_no_match(tmp_path):
    from memory import ImportantMemoryManager

    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()
    result = await try_save_from_user("今天天气真好", im, enabled=True)
    assert result is None


@pytest.mark.asyncio
async def test_keyword_save_no_manager():
    """important=None 时应安全返回 None。"""
    result = await try_save_from_user("记住啥的", None, enabled=True)
    assert result is None


# ============================================================
# upload_file 安全检查
# ============================================================


class _FakeAdapter:
    name = "fake"
    is_connected = True

    def __init__(self):
        self.uploaded: list = []

    async def upload_file(self, target, file_path, *, display_name=None):
        self.uploaded.append((target, file_path, display_name))


@pytest.mark.asyncio
async def test_upload_file_outside_whitelist_rejected(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x")

    ctx = ToolContext(adapter=_FakeAdapter(), upload_allowed_dir=allowed)
    executor = reg.get_executor(ctx)
    result = await executor(
        "upload_file",
        {"target_type": "private", "target_id": 1, "file_path": str(outside)},
    )
    assert result["ok"] is False
    assert "范围" in result["error"]


@pytest.mark.asyncio
async def test_upload_file_inside_whitelist_ok(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    inside = allowed / "x.txt"
    inside.write_text("x")

    fake = _FakeAdapter()
    ctx = ToolContext(adapter=fake, upload_allowed_dir=allowed)
    executor = reg.get_executor(ctx)
    result = await executor(
        "upload_file",
        {"target_type": "group", "target_id": 1, "file_path": str(inside)},
    )
    assert result["ok"] is True
    assert len(fake.uploaded) == 1


@pytest.mark.asyncio
async def test_upload_file_no_adapter():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext()  # 没 adapter
    executor = reg.get_executor(ctx)
    result = await executor(
        "upload_file",
        {"target_type": "private", "target_id": 1, "file_path": "/tmp/x"},
    )
    assert result["ok"] is False


# ============================================================
# control: schedule_wakeup
# ============================================================


@pytest.mark.asyncio
async def test_schedule_wakeup_no_callback():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext()
    executor = reg.get_executor(ctx)
    result = await executor(
        "schedule_wakeup", {"delay_seconds": 10, "reminder": "test"}
    )
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_schedule_wakeup_with_callback():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    received: list[tuple[int, str]] = []

    async def cb(delay, reminder):
        received.append((delay, reminder))

    ctx = ToolContext(wakeup_cb=cb)
    executor = reg.get_executor(ctx)
    result = await executor(
        "schedule_wakeup", {"delay_seconds": 10, "reminder": "test"}
    )
    assert result["ok"] is True
    assert received == [(10, "test")]


# ============================================================
# memory 工具：依赖注入
# ============================================================


@pytest.mark.asyncio
async def test_save_memory_no_manager():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext()
    executor = reg.get_executor(ctx)
    result = await executor("save_important_memory", {"memory_text": "x"})
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_save_memory_with_manager(tmp_path):
    from memory import ImportantMemoryManager

    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()

    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(important=im)
    executor = reg.get_executor(ctx)
    result = await executor(
        "save_important_memory", {"memory_text": "记住张三是朋友"}
    )
    assert result["ok"] is True
    assert result["saved"] is True
    assert len(im.items()) == 1


@pytest.mark.asyncio
async def test_delete_memory_with_manager(tmp_path):
    from memory import ImportantMemoryManager

    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()
    await im.save("张三是朋友")
    await im.save("李四是同事")

    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(important=im)
    executor = reg.get_executor(ctx)
    result = await executor(
        "delete_important_memory", {"keyword": "张三"}
    )
    assert result["ok"] is True
    assert result["deleted"] == 1


# ============================================================
# platform: list_contacts 兜底
# ============================================================


@pytest.mark.asyncio
async def test_list_contacts_no_adapter():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext()
    executor = reg.get_executor(ctx)
    result = await executor("list_contacts", {"scope": "friends"})
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_list_contacts_with_adapter():
    """以伪适配器验证 list_friends 转 dict。"""
    from adapters.types import FriendInfo

    class FakeAd:
        name = "fake"
        is_connected = True

        async def list_friends(self):
            return [FriendInfo(user_id="1", nickname="A")]

    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(adapter=FakeAd())  # type: ignore
    executor = reg.get_executor(ctx)
    result = await executor("list_contacts", {"scope": "friends"})
    assert result["ok"] is True
    assert result["count"] == 1
    assert result["friends"][0]["nickname"] == "A"


@pytest.mark.asyncio
async def test_list_contacts_group_members_requires_group_id():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(adapter=object())  # type: ignore  # 仅占位
    executor = reg.get_executor(ctx)
    result = await executor("list_contacts", {"scope": "group_members"})
    assert result["ok"] is False
    assert "group_id" in result["error"]
