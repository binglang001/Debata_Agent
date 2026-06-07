"""测试工具系统：schema 派生 / Registry 启用禁用 / 工具执行 / 关键词保存。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from adapters.types import FriendInfo, GroupInfo, GroupMemberInfo, UserInfo
from core.chat_timeline import ChatTimelineMessage, ChatTimelineStore
from features.vision.vision_service import VisionService
from providers.base import ProviderError
from tools import (
    DEFAULT_NO_FEEDBACK_TOOLS,
    FEATURE_TOOL_FEATURES,
    MEMORY_FILE_TOOLS,
    STUB_SCHEMA_TOOLS,
    ToolContext,
    ToolRegistry,
    build_default_registry,
    build_message_action,
    contains_forbidden,
    get_default_specs,
    try_save_from_user,
    typing_delay,
)
from tools.base import _inline_refs, _strip_pydantic_metadata, tool
from tools.feature_tools import send_voice_message
from tools.message_builder import MessageBuildError, resolve_emoji_path
from tools.result_shrink import tool_budget
from tools.schemas import (
    SendVoiceMessageArgs,
)


def test_tool_budget_uses_default_per_tool_values():
    ctx = ToolContext()

    budget = tool_budget("read_file", ctx)

    assert budget.inline == 2500
    assert budget.artifact_threshold == 2500
    assert budget.hard_cap >= 2500


def test_tool_budget_falls_back_to_global_default_for_unknown_tool():
    ctx = ToolContext()

    budget = tool_budget("unknown_tool", ctx)

    assert budget.inline == 800
    assert budget.artifact_threshold == 800
    assert budget.hard_cap == 3000


def test_tool_budget_keeps_legacy_override_when_no_new_budget_exists():
    ctx = ToolContext(
        tool_result_budgets={},
        tool_result_soft_limit_tokens=700,
        tool_result_hard_cap_tokens=1600,
        tool_result_soft_overrides={"read_file": 900},
    )

    budget = tool_budget("read_file", ctx)

    assert budget.inline == 900
    assert budget.artifact_threshold == 900
    assert budget.hard_cap == 1600


def _timeline_message(message_id: str, text: str) -> ChatTimelineMessage:
    return ChatTimelineMessage(
        conversation_id="private:123",
        direction="inbound",
        timestamp=1_780_000_000.0,
        time_text="2026-05-30 00:00:00",
        sender_name="用户",
        sender_id="123",
        target_id="123",
        group_id=None,
        msg_id=message_id,
        text=text,
        raw_message=text,
    )


def _approve_stub_tools(ctx: ToolContext, *names: str) -> None:
    approved = ctx.extras.setdefault("tool_search_approved_tools", set())
    assert isinstance(approved, set)
    approved.update(names)


class FakeSendAdapter:
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[tuple[object, str]] = []
        self.sent_images: list[dict[str, object]] = []
        self.voice_sent: list[tuple[object, Path]] = []
        self._next_msg_id = 100

    async def send_text(self, target, content: str) -> str:
        msg_id = str(self._next_msg_id)
        self._next_msg_id += 1
        self.sent.append((target, content))
        return msg_id

    async def send_voice(self, target, audio_path: Path) -> str:
        msg_id = str(self._next_msg_id)
        self._next_msg_id += 1
        self.voice_sent.append((target, audio_path))
        return msg_id

    async def send_image(self, target, *, image_path=None, image_url=None, image_b64=None) -> str:
        msg_id = str(self._next_msg_id)
        self._next_msg_id += 1
        self.sent_images.append(
            {
                "target": target,
                "image_path": image_path,
                "image_url": image_url,
                "image_b64": image_b64,
            }
        )
        return msg_id


class FullFakeAdapter(FakeSendAdapter):
    """覆盖所有平台工具会触达的适配器方法。"""

    async def list_friends(self):
        return [FriendInfo(user_id="1001", nickname="Alice")]

    async def list_groups(self):
        return [GroupInfo(group_id="2001", group_name="测试群", member_count=2)]

    async def list_group_members(self, group_id: str):
        return [
            GroupMemberInfo(user_id="1001", nickname="Alice", card="AliceCard"),
            GroupMemberInfo(user_id="1002", nickname="Bob"),
        ]

    async def get_user_info(self, user_id: str):
        return UserInfo(user_id=user_id, nickname="Alice", sex="unknown", age=18)

    async def get_forward_msg(self, forward_id: str):
        if forward_id == "root":
            return [
                {
                    "sender": {"nickname": "Alice", "user_id": "1001"},
                    "message_id": "f1",
                    "content": "第一条[CQ:image,summary=[图片],file=a.jpg,url=https://example.com/a.jpg]",
                },
                {
                    "sender": {"nickname": "Bob", "user_id": "1002"},
                    "message_id": "f2",
                    "content": "[CQ:forward,id=child]",
                },
            ]
        if forward_id == "child":
            return [
                {
                    "sender": {"nickname": "Carol", "user_id": "1003"},
                    "message_id": "f3",
                    "content": "内层消息",
                }
            ]
        return []

    async def handle_friend_request(self, flag: str, approve: bool, remark: str = "") -> None:
        self.friend_request = {"flag": flag, "approve": approve, "remark": remark}

    async def handle_group_request(
        self,
        flag: str,
        sub_type: str,
        approve: bool,
        reason: str = "",
    ) -> None:
        self.group_request = {
            "flag": flag,
            "sub_type": sub_type,
            "approve": approve,
            "reason": reason,
        }

    async def get_group_history(self, group_id: str, count: int = 100):
        return [{"message_id": "h1", "raw_message": "群历史", "sender": {"nickname": "A"}}]

    async def recall(self, message_id: str) -> bool:
        self.recalled = message_id
        return True

    async def upload_file(self, target, file_path: Path, *, display_name: str | None = None) -> None:
        self.uploaded = {"target": target, "file_path": file_path, "display_name": display_name}


class FakeVision:
    async def describe(self, image_url: str, prompt: str = ""):
        return {"summary": "一张测试图片", "description": f"图片={image_url}; 问题={prompt or '-'}"}


class FakeWebSearch:
    async def search(self, query: str) -> str:
        return f"1. 结果\n摘要\nhttps://example.com/search?q={query}"


class FakeWeather:
    async def query(self, city: str, days: int = 1) -> str:
        return f"{city} {days} 天天气晴"


class FakeTTS:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    async def synthesize(self, text: str, *, reference_audio=None, prompt: str = "") -> Path:
        path = self.workspace / "voice.wav"
        path.write_bytes(b"RIFFfake")
        return path


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
    assert "emoji" in target_props
    assert "image" in target_props
    assert "不是表情包" in target_props["image"]["description"]


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
# 所有工具注册了
# ============================================================


def test_all_expected_tools_registered():
    """检查核心工具都已通过装饰器注册到全局列表。"""
    expected = {
        "send_private_messages", "send_group_message", "commit_send_attempt",
        "recall_message", "upload_file",
        "save_important_memory", "update_important_memory", "delete_important_memory",
        "list_contacts", "get_user_info", "get_forward_msg", "get_recent_chat_messages",
        "set_friend_add_request", "set_group_add_request", "summarize_chat_history",
        "summarize_conversation", "recall_history", "start_agent_task",
        "no_action", "schedule_wakeup",
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
            vision=VisionFeatureConfig(
                enabled=vision_enabled,
                provider="deepseek" if vision_enabled else None,
            ),
            web_search=WebSearchFeatureConfig(enabled=web_search_enabled),
            weather=WeatherFeatureConfig(
                enabled=weather_enabled,
                api_key_id="fake_qweather" if weather_enabled else None,
                host="devapi.qweather.com",
            ),
            long_term_memory=LongTermMemoryConfig(mode=memory_mode),
        ),
    )


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


def test_registry_upload_file_is_stub_schema():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    schema_by_name = {
        schema["function"]["name"]: schema["function"]
        for schema in reg.get_schemas()
    }
    assert "upload_file" in reg
    assert set(schema_by_name["upload_file"]["parameters"]["properties"]) == {
        "_tool_search_required"
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
        assert set(props) == {"_tool_search_required"}
    assert "targets" in schema_by_name["send_private_messages"]["parameters"]["properties"]
    assert "tool_name" in schema_by_name["tool_search"]["parameters"]["properties"]


@pytest.mark.asyncio
async def test_stub_tool_requires_tool_search_before_execution(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    file_path = workspace / "x.txt"
    file_path.write_text("x", encoding="utf-8")
    adapter = _FakeAdapter()
    ctx = ToolContext(adapter=adapter, workspace_dir=workspace)
    executor = reg.get_executor(ctx)

    blocked = await executor(
        "upload_file",
        {"target_type": "group", "target_id": 1, "file_path": "x.txt"},
    )
    assert blocked["ok"] is False
    assert blocked["status"] == "need_tool_search"

    details = await executor("tool_search", {"tool_name": "upload_file", "intent": "发送文件"})
    assert details["ok"] is True
    assert details["status"] == "found"
    assert details["tool_name"] == "upload_file"
    assert "file_path" in details["parameters_schema"]["properties"]
    assert "file_path" in details["required_fields"]

    sent = await executor(
        "upload_file",
        {"target_type": "group", "target_id": 1, "file_path": "x.txt"},
    )
    assert sent["ok"] is True
    assert sent["status"] == "done"
    assert len(adapter.uploaded) == 1


@pytest.mark.asyncio
async def test_tool_search_reports_unknown_tool_candidates():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    executor = reg.get_executor(ToolContext())

    result = await executor("tool_search", {"tool_name": "send"})

    assert result["ok"] is False
    assert result["status"] == "not_found"
    assert "send_private_messages" in result["candidates"]


@pytest.mark.asyncio
async def test_all_tools_have_clear_results_in_simulated_runtime(tmp_path):
    """逐个调用所有工具，检查真实执行链路下的返回结构足够清晰。"""
    from memory import ArchiveStore, HistoryManager, ImportantMemoryManager

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("old line\nsecond line\n", encoding="utf-8")
    (workspace / "delete-me.txt").write_text("delete", encoding="utf-8")
    upload_path = workspace / "report.md"
    upload_path.write_text("# report\n", encoding="utf-8")

    history = HistoryManager(tmp_path / "history.jsonl")
    await history.load()
    await history.add_user_message(
        "历史消息",
        metadata={"timestamp": "2026-06-01 12:00:00"},
        conversation_id="private:123",
    )
    archive = ArchiveStore(tmp_path / "archive.jsonl")
    await archive.load()
    await archive.append_many(
        [
            {
                "role": "user",
                "content": "归档消息 keyword",
                "conversation_id": "private:123",
                "metadata": {"timestamp": "2026-06-01 11:00:00"},
            }
        ]
    )
    important = ImportantMemoryManager(tmp_path / "important.json")
    await important.load()
    await important.replace_all(
        [{"timestamp": "mem-existing", "content": "用户喜欢绿茶"}]
    )

    timeline = ChatTimelineStore()
    timeline.append(_timeline_message("m1", "最近消息"))
    wakeups: list[dict] = []
    agent_tasks: list[dict] = []
    sent_actions: list[dict] = []

    async def fake_wakeup(delay_seconds, reminder, target, mode, message_text):
        wakeups.append(
            {
                "delay_seconds": delay_seconds,
                "reminder": reminder,
                "target": target,
                "mode": mode,
                "message_text": message_text,
            }
        )

    async def fake_agent_task(payload):
        agent_tasks.append(payload)
        return {
            "ok": True,
            "status": "completed",
            "task_id": "agent-test",
            "brief": "子 Agent 已完成。",
            "result_file": "agent_tasks/agent-test/result.md",
            "content": "子 Agent 结果",
            "summary": "子 Agent 结果",
        }

    async def fake_send_actions(actions, source_tool, *, metadata=None):
        sent_actions.append({"source_tool": source_tool, "actions": actions, "metadata": metadata})
        if source_tool == "commit_send_attempt":
            return {
                "ok": True,
                "status": "already_committed",
                "send_attempt_id": (metadata or {}).get("commit_send_attempt_id"),
                "qq_visible": False,
            }
        return {
            "ok": True,
            "status": "sent",
            "qq_visible": True,
            "send_id": "send-test",
            "count": len(actions),
            "sent": [
                {
                    "conversation_id": f"{a['target_scope']}:{a['target_id']}",
                    "msg_id": f"msg-{idx}",
                    "order": a.get("order", idx),
                    "qq_visible": True,
                }
                for idx, a in enumerate(actions, start=1)
            ],
        }

    adapter = FullFakeAdapter()
    ctx = ToolContext(
        adapter=adapter,
        important=important,
        conversation_id="private:123",
        history=history,
        archive=archive,
        vision=FakeVision(),
        web_search=FakeWebSearch(),
        weather=FakeWeather(),
        tts=FakeTTS(workspace),
        wakeup_cb=fake_wakeup,
        workspace_dir=workspace,
        send_actions_cb=fake_send_actions,
        agent_task_cb=fake_agent_task,
        extras={
            "chat_timeline": timeline,
            "default_reply_target": {"target_type": "private", "target_id": 123},
            "latest_user_message": "帮我测试工具",
        },
    )
    reg = ToolRegistry(get_default_specs())
    executor = reg.get_executor(ctx)

    calls: dict[str, dict] = {
        "start_agent_task": {
            "prompt": "读取资料并写出结果",
            "sources": [{"type": "inline_text", "value": "资料"}],
            "output_format": "markdown",
            "output_name": "result.md",
            "max_loops": 5,
        },
        "no_action": {},
        "schedule_wakeup": {
            "delay_seconds": 1,
            "mode": "send_message",
            "message_text": "提醒",
        },
        "describe_image": {"image_url": "https://example.com/a.jpg", "question": "看图"},
        "web_search": {"query": "Debata"},
        "get_weather": {"city": "北京", "days": 1},
        "send_voice_message": {
            "target_type": "private",
            "target_id": 123,
            "text": "语音测试",
            "prompt": "年轻女性，自然口语",
        },
        "save_important_memory": {"memory_text": "用户喜欢红茶"},
        "update_important_memory": {
            "memory_id": "mem-existing",
            "memory_text": "用户喜欢红茶和乌龙茶",
        },
        "delete_important_memory": {"keyword": "红茶"},
        "send_private_messages": {
            "targets": [{"target_qq": 123, "content": "你好", "order": 1}],
        },
        "send_group_message": {
            "group_id": 456,
            "targets": [{"content": "群消息", "order": 1}],
        },
        "commit_send_attempt": {
            "send_attempt_id": "attempt-test",
            "reviewed_until_seq": 1,
            "delivery_interrupt_policy": "interrupt_priority",
        },
        "recall_message": {"message_id": 100},
        "upload_file": {
            "target_type": "private",
            "target_id": 123,
            "file_path": "report.md",
        },
        "list_contacts": {"scope": "friends", "limit": 10},
        "get_user_info": {"user_id": 1001},
        "get_forward_msg": {"forward_id": "root", "recursive": True, "max_depth": 2},
        "get_recent_chat_messages": {"conversation_id": "private:123", "limit": 10},
        "set_friend_add_request": {"flag": "friend-flag", "approve": True, "remark": "A"},
        "set_group_add_request": {
            "flag": "group-flag",
            "sub_type": "add",
            "approve": False,
            "reason": "拒绝",
        },
        "summarize_chat_history": {"group_id": 456, "custom_prompt": "总结"},
        "summarize_conversation": {
            "conversation_id": "private:123",
            "range_hint": "2026-06-01",
            "goal": "总结",
        },
        "recall_history": {"conversation_id": "private:123", "keyword": "keyword", "limit": 5},
        "read_file": {"path": "note.txt", "max_lines": 10},
        "write_file": {"path": "created.txt", "content": "created"},
        "edit_file": {"path": "note.txt", "old": "old line", "new": "new line"},
        "list_files": {"path": ".", "pattern": "*.txt", "limit": 20},
        "delete_file": {"path": "delete-me.txt"},
        "run_python": {"code": "print('ok')", "timeout_seconds": 5},
        "tool_search": {"tool_name": "upload_file", "intent": "测试工具详情查询"},
    }
    assert set(calls) == {spec.name for spec in get_default_specs()}

    results: dict[str, dict] = {}
    for name, args in calls.items():
        result = await executor(name, args)
        results[name] = result
        assert isinstance(result, dict), name
        assert "ok" in result, name
        if result.get("ok"):
            assert any(k in result for k in ("brief", "status", "data", "sent", "scheduled", "path")), name
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
        assert "_condensed" not in result, name
        assert len(encoded) < 12000, name

    assert results["get_forward_msg"]["status"] == "artifact"
    assert results["get_forward_msg"]["artifact"]["path"].startswith("runtime/forwards/")
    assert results["get_forward_msg"]["data"]["nested_forward_count"] == 1
    forward_artifact = workspace / results["get_forward_msg"]["artifact"]["path"]
    forward_tree = json.loads(forward_artifact.read_text(encoding="utf-8"))
    assert forward_tree["messages"][0]["segments"][1]["url"] == "https://example.com/a.jpg"
    assert (
        forward_tree["messages"][1]["segments"][0]["node"]["messages"][0]
        ["segments"][0]["text"]
        == "内层消息"
    )

    assert results["get_recent_chat_messages"]["content"].count("最近消息") == 1
    assert results["get_recent_chat_messages"]["data"]["range"] == "continuous"
    assert results["get_recent_chat_messages"]["data"]["last_msg_id"] == "m1"

    assert results["read_file"]["data"]["range"] == "continuous_page"
    assert "old line" in results["read_file"]["content"]

    assert results["start_agent_task"]["task_id"] == "agent-test"
    assert results["start_agent_task"]["status"] == "completed"
    assert results["start_agent_task"]["result_file"] == "agent_tasks/agent-test/result.md"
    assert agent_tasks[0]["sources"][0]["type"] == "inline_text"
    assert agent_tasks[0]["max_loops"] == 5

    assert results["no_action"]["no_action"] is True
    assert results["schedule_wakeup"]["scheduled"] is True
    assert wakeups[0]["delay_seconds"] == 1
    assert wakeups[0]["target"] == {"target_type": "private", "target_id": 123}
    assert "提醒" in wakeups[0]["reminder"]

    assert results["describe_image"]["summary"] == "一张测试图片"
    assert results["describe_image"]["data"]["image_ref"] == "https://example.com/a.jpg"
    assert "问题=看图" in results["describe_image"]["description"]
    assert results["web_search"]["query"] == "Debata"
    assert "https://example.com/search?q=Debata" in results["web_search"]["result"]
    assert results["get_weather"]["data"] == {"city": "北京", "days": 1}
    assert "北京 1 天天气晴" in results["get_weather"]["result"]

    assert results["send_voice_message"]["status"] == "sent"
    assert results["send_private_messages"]["status"] == "sent"
    assert results["send_group_message"]["status"] == "sent"
    non_commit_sends = [
        item for item in sent_actions if item["source_tool"] != "commit_send_attempt"
    ]
    assert [
        item["source_tool"] for item in non_commit_sends
    ] == ["send_voice_message", "send_private_messages", "send_group_message"]
    assert results["commit_send_attempt"]["status"] == "already_committed"
    assert sent_actions[0]["actions"][0]["kind"] == "voice"
    assert sent_actions[1]["actions"][0]["target_scope"] == "private"
    assert sent_actions[1]["actions"][0]["content"] == "你好"
    assert sent_actions[2]["actions"][0]["target_scope"] == "group"
    assert sent_actions[2]["actions"][0]["target_id"] == "456"

    assert results["save_important_memory"]["saved"] is True
    assert results["save_important_memory"]["scope"] == "user:123"
    assert results["update_important_memory"]["updated"] is True
    assert results["delete_important_memory"]["deleted"] == 2
    assert important.items() == []

    assert results["recall_message"]["data"]["message_id"] == "100"
    assert adapter.recalled == "100"
    assert results["upload_file"]["data"]["target_type"] == "private"
    assert adapter.uploaded["file_path"] == upload_path
    assert adapter.uploaded["display_name"] == "report.md"
    assert results["tool_search"]["status"] == "found"
    assert results["tool_search"]["tool_name"] == "upload_file"
    assert "file_path" in results["tool_search"]["parameters_schema"]["properties"]

    assert results["list_contacts"]["data"]["scope"] == "friends"
    assert results["list_contacts"]["friends"][0]["nickname"] == "Alice"
    assert results["get_user_info"]["data"]["user_id"] == "1001"
    assert results["get_user_info"]["info"]["nickname"] == "Alice"
    assert adapter.friend_request == {"flag": "friend-flag", "approve": True, "remark": "A"}
    assert adapter.group_request == {
        "flag": "group-flag",
        "sub_type": "add",
        "approve": False,
        "reason": "拒绝",
    }

    assert results["summarize_chat_history"]["task_id"] == "agent-test"
    assert agent_tasks[1]["output_name"] == "group_456_summary.md"
    assert agent_tasks[1]["sources"][0]["data"]["messages"][0]["raw_message"] == "群历史"
    assert results["summarize_conversation"]["task_id"] == "agent-test"
    assert agent_tasks[2]["output_name"] == "conversation_summary.md"
    assert agent_tasks[2]["sources"][0]["conversation_id"] == "private:123"
    assert results["recall_history"]["count"] == 1
    assert "归档消息 keyword" in results["recall_history"]["content"]

    assert results["write_file"]["path"] == "created.txt"
    assert (workspace / "created.txt").read_text(encoding="utf-8") == "created"
    assert results["edit_file"]["data"]["old_length"] == len("old line")
    assert (workspace / "note.txt").read_text(encoding="utf-8").startswith("new line")
    assert results["list_files"]["data"]["count"] >= 1
    assert any(item["path"] == "created.txt" for item in results["list_files"]["entries"])
    assert results["delete_file"]["path"] == "delete-me.txt"
    assert not (workspace / "delete-me.txt").exists()
    assert results["run_python"]["returncode"] == 0
    assert results["run_python"]["stdout"].strip() == "ok"


def test_registry_no_feedback_names_includes_known():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    names = reg.get_no_feedback_names()
    assert "no_action" in names
    assert "save_important_memory" in names
    assert "schedule_wakeup" in names


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
    assert result["status"] == "done"
    assert "brief" not in result
    assert result.get("no_action") is True


@pytest.mark.asyncio
async def test_executor_exception_returns_error():
    """工具内部抛异常应被捕获并转成 ok=False。"""
    # 用一个故意抛异常的临时工具

    class _Args(BaseModel):
        pass

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
async def test_describe_image_accepts_workspace_path(tmp_path):
    class FakeVision:
        def __init__(self) -> None:
            self.image_url = ""

        async def describe(self, image_url: str, prompt: str = "") -> str:
            self.image_url = image_url
            return "ok"

    workspace = tmp_path / "workspace"
    incoming = workspace / "incoming"
    incoming.mkdir(parents=True)
    (incoming / "a.png").write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    )

    cfg = _make_config(vision_enabled=True)
    reg = build_default_registry(cfg)
    vision = FakeVision()
    ctx = ToolContext(vision=vision, workspace_dir=workspace)
    executor = reg.get_executor(ctx)
    result = await executor("describe_image", {"image_url": "incoming/a.png"})

    assert result["ok"] is True
    assert vision.image_url.startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_describe_image_prefers_workspace_over_remote_url(tmp_path, monkeypatch):
    class FakeVision:
        def __init__(self) -> None:
            self.image_url = ""

        async def describe(self, image_url: str, prompt: str = "") -> str:
            self.image_url = image_url
            return "ok"

    async def fail_download(image_url: str) -> str:
        raise AssertionError("workspace 已存在时不应下载远程图片")

    monkeypatch.setattr("tools.feature_tools._download_image_as_data_url", fail_download)

    workspace = tmp_path / "workspace"
    incoming = workspace / "incoming"
    incoming.mkdir(parents=True)
    (incoming / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    cfg = _make_config(vision_enabled=True)
    reg = build_default_registry(cfg)
    vision = FakeVision()
    ctx = ToolContext(vision=vision, workspace_dir=workspace)
    executor = reg.get_executor(ctx)
    result = await executor(
        "describe_image",
        {
            "image_url": (
                "[图片 url=https://multimedia.nt.qq.com.cn/download?"
                "appid=1407&amp;fileid=x&amp;rkey=y workspace=incoming/a.png]"
            )
        },
    )

    assert result["ok"] is True
    assert vision.image_url.startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_describe_image_localizes_qq_image_url(monkeypatch):
    class FakeVision:
        def __init__(self) -> None:
            self.image_url = ""

        async def describe(self, image_url: str, prompt: str = "") -> str:
            self.image_url = image_url
            return "ok"

    async def fake_download(image_url: str) -> str:
        assert image_url.startswith("https://multimedia.nt.qq.com.cn/download?")
        return "data:image/png;base64,ZmFrZQ=="

    monkeypatch.setattr("tools.feature_tools._download_image_as_data_url", fake_download)

    cfg = _make_config(vision_enabled=True)
    reg = build_default_registry(cfg)
    vision = FakeVision()
    ctx = ToolContext(vision=vision)
    executor = reg.get_executor(ctx)
    result = await executor(
        "describe_image",
        {"image_url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=x&rkey=y"},
    )

    assert result["ok"] is True
    assert vision.image_url == "data:image/png;base64,ZmFrZQ=="


@pytest.mark.asyncio
async def test_describe_image_qq_download_failure_does_not_call_vision(monkeypatch):
    class FakeVision:
        async def describe(self, image_url: str, prompt: str = "") -> str:
            raise AssertionError("QQ 临时图片本地化失败后不应继续调用视觉 provider")

    async def fake_download(image_url: str) -> None:
        assert image_url == (
            "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=x&rkey=y"
        )
        return None

    monkeypatch.setattr("tools.feature_tools._download_image_as_data_url", fake_download)

    cfg = _make_config(vision_enabled=True)
    reg = build_default_registry(cfg)
    ctx = ToolContext(vision=FakeVision())
    executor = reg.get_executor(ctx)
    result = await executor(
        "describe_image",
        {
            "image_url": (
                "https://multimedia.nt.qq.com.cn/download?"
                "appid=1407&amp;fileid=x&amp;rkey=y"
            )
        },
    )

    assert result["ok"] is False
    assert result["error"] == "图片链接下载失败"
    assert result["data"]["image_ref"] == "multimedia.nt.qq.com.cn/..."


@pytest.mark.asyncio
async def test_describe_image_failure_result_is_compact():
    class FailingVision:
        async def describe(self, image_url: str, prompt: str = "") -> dict:
            raise RuntimeError(
                "vision_volcengine: API 错误: Error code: 400 - "
                "{'error': {'code': 'InvalidParameter', 'message': "
                "'Error while downloading: https://multimedia.nt.qq.com.cn/download?"
                "appid=1407&fileid=SECRET&rkey=SECRET, status code: 400 "
                "Request id: abc', 'param': 'image_url', 'type': 'BadRequest'}}"
            )

    cfg = _make_config(vision_enabled=True)
    reg = build_default_registry(cfg)
    ctx = ToolContext(vision=FailingVision())
    executor = reg.get_executor(ctx)
    result = await executor("describe_image", {"image_url": "https://example.com/a.png"})

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["brief"] == "图片理解失败。"
    assert result["summary"] == "图片识别失败"
    assert result["error"] == "视觉服务无法读取图片链接"
    dumped = json.dumps(result, ensure_ascii=False)
    assert "SECRET" not in dumped
    assert "Request id" not in dumped
    assert "vision_volcengine" not in dumped
    assert "InvalidParameter" not in dumped


@pytest.mark.asyncio
async def test_vision_service_propagates_provider_error():
    class FailingProvider:
        name = "vision"

        async def chat_completion(self, *args, **kwargs):
            raise ProviderError("raw provider failure")

    service = VisionService(FailingProvider(), model="vision-model")

    with pytest.raises(ProviderError):
        await service.describe("data:image/png;base64,ZmFrZQ==")


@pytest.mark.asyncio
async def test_describe_image_long_description_saved_once(tmp_path):
    class FakeVision:
        async def describe(self, image_url: str, prompt: str = "") -> dict:
            return {
                "summary": "胡桃和魈表情包",
                "description": "完整描述 " * 500,
            }

    workspace = tmp_path / "workspace"
    incoming = workspace / "incoming"
    incoming.mkdir(parents=True)
    (incoming / "a.png").write_bytes(b"fake")

    cfg = _make_config(vision_enabled=True)
    reg = build_default_registry(cfg)
    ctx = ToolContext(
        vision=FakeVision(),
        workspace_dir=workspace,
        tool_result_budgets={
            "describe_image": {
                "inline_budget_tokens": 256,
                "artifact_threshold_tokens": 256,
                "hard_cap_tokens": 512,
            }
        },
    )
    executor = reg.get_executor(ctx)
    result = await executor("describe_image", {"image_url": "incoming/a.png"})

    assert result["ok"] is True
    assert result["status"] == "artifact"
    assert result["summary"] == "胡桃和魈表情包"
    assert "description" not in result
    assert result["full_saved"] == "incoming/a.desc.md"
    assert result["artifact"]["path"] == "incoming/a.desc.md"
    assert (workspace / "incoming" / "a.desc.md").read_text(encoding="utf-8").startswith("完整描述")
    first = dict(result)
    second = await executor("describe_image", {"image_url": "incoming/a.png"})
    assert second == first


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
async def test_web_search_condenses_long_results():
    class FakeSearch:
        async def search(self, query: str) -> str:
            return "\n\n".join(
                f"{i}. 标题 {i}\n" + ("摘要内容 " * 80) + f"\nhttps://example.com/{i}"
                for i in range(1, 8)
            )

    cfg = _make_config(web_search_enabled=True)
    reg = build_default_registry(cfg)
    ctx = ToolContext(
        web_search=FakeSearch(),
        tool_result_budgets={},
        tool_result_soft_limit_tokens=80,
        tool_result_hard_cap_tokens=500,
    )
    executor = reg.get_executor(ctx)
    result = await executor("web_search", {"query": "测试"})

    assert result["ok"] is True
    assert "preview" not in result
    assert result["_condensed"]["reason"] == "搜索结果过长已保留高位结果摘要"
    assert "1. 标题 1" in result["result"]
    assert "https://example.com/1" in result["result"]
    assert "7. 标题 7" not in result["result"]


@pytest.mark.asyncio
async def test_weather_without_service():
    cfg = _make_config(weather_enabled=True)
    reg = build_default_registry(cfg)
    ctx = ToolContext()
    executor = reg.get_executor(ctx)
    result = await executor("get_weather", {"city": "北京"})
    assert result["ok"] is False
    assert "未启用" in result["error"]


def test_send_voice_message_requires_style_prompt():
    with pytest.raises(ValidationError):
        SendVoiceMessageArgs.model_validate(
            {"target_type": "private", "target_id": 1, "text": "你好"}
        )


@pytest.mark.asyncio
async def test_send_voice_message_sends_immediately(tmp_path):
    class FakeTTS:
        async def synthesize(self, text, *, prompt):
            path = tmp_path / "voice.wav"
            path.write_bytes(b"RIFF")
            return path

    adapter = FakeSendAdapter()
    ctx = ToolContext(tts=FakeTTS(), adapter=adapter)
    args = SendVoiceMessageArgs(
        target_type="private",
        target_id=123,
        text="语音测试",
        prompt="年轻女性，自然口语",
    )

    result = await send_voice_message(args, ctx)

    assert result["ok"] is True
    assert result["sent"]["msg_id"] == "100"
    assert ctx.collected == []
    assert len(adapter.voice_sent) == 1
    target, audio_path = adapter.voice_sent[0]
    assert target.scope == "private"
    assert target.target_id == "123"
    assert audio_path.name == "voice.wav"


# ============================================================
# Messaging 工具：即时发送
# ============================================================


@pytest.mark.asyncio
async def test_send_private_sends_immediately(tmp_path):
    """send_private_messages 应即时发送并返回 msg_id。"""
    cfg = _make_config()
    reg = build_default_registry(cfg)
    emoji_dir = tmp_path / "emoji"
    emoji_dir.mkdir()

    adapter = FakeSendAdapter()
    ctx = ToolContext(emoji_dir=emoji_dir, adapter=adapter)
    executor = reg.get_executor(ctx)
    result = await executor(
        "send_private_messages",
        {
            "targets": [
                {"target_qq": 12345, "content": "你好", "order": 1, "delay": 0},
                {"target_qq": 12345, "content": "在吗", "order": 2, "delay": 0},
            ]
        },
    )
    assert result["ok"] is True
    assert result["count"] == 2
    assert [item["order"] for item in result["sent"]] == [1, 2]
    assert [item["target_qq"] for item in result["sent"]] == ["12345", "12345"]
    assert [item["msg_id"] for item in result["sent"]] == ["100", "101"]
    assert result["status"] == "sent"
    assert result["qq_visible"] is True
    assert result["sent"][0]["conversation_id"] == "private:12345"
    assert result["sent"][0]["content"] == "你好"
    assert result["sent"][0]["qq_visible"] is True
    assert "sent_messages" not in result
    assert ctx.collected == []
    assert [content for _, content in adapter.sent] == ["你好", "在吗"]


@pytest.mark.asyncio
async def test_send_private_forbidden_blocked(tmp_path):
    """含 FORBIDDEN_TAGS 的内容应被拒绝。"""
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(emoji_dir=tmp_path / "emoji", adapter=FakeSendAdapter())
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
    adapter = FakeSendAdapter()
    ctx = ToolContext(emoji_dir=tmp_path / "emoji", adapter=adapter)
    executor = reg.get_executor(ctx)
    result = await executor(
        "send_group_message",
        {
            "group_id": 100,
            "targets": [
                {"content": "third", "order": 3, "delay": 0},
                {"content": "first", "order": 1, "delay": 0},
                {"content": "second", "order": 2, "delay": 0},
            ],
        },
    )
    contents = [content for _, content in adapter.sent]
    assert contents == ["first", "second", "third"]
    assert [item["msg_id"] for item in result["sent"]] == ["100", "101", "102"]
    assert result["qq_visible"] is True
    assert result["sent"][0]["conversation_id"] == "group:100"
    assert [item["content"] for item in result["sent"]] == [
        "first",
        "second",
        "third",
    ]
    assert "sent_messages" not in result


@pytest.mark.asyncio
async def test_send_group_emoji_uses_emoji_name_without_suffix(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    emoji_dir = tmp_path / "emoji"
    emoji_dir.mkdir()
    (emoji_dir / "无语.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    adapter = FakeSendAdapter()
    ctx = ToolContext(emoji_dir=emoji_dir, adapter=adapter)
    executor = reg.get_executor(ctx)

    result = await executor(
        "send_group_message",
        {
            "group_id": 100,
            "targets": [{"emoji": "无语", "order": 1, "delay": 0}],
        },
    )

    assert result["ok"] is True
    assert result["sent"][0]["content"] == "[表情包: 无语]"
    assert adapter.sent == []
    assert adapter.sent_images[0]["image_path"] == emoji_dir / "无语.png"


@pytest.mark.asyncio
async def test_send_group_image_is_workspace_or_url_not_emoji(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image_path = workspace / "incoming" / "a.png"
    image_path.parent.mkdir()
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    adapter = FakeSendAdapter()
    ctx = ToolContext(workspace_dir=workspace, adapter=adapter)
    executor = reg.get_executor(ctx)

    result = await executor(
        "send_group_message",
        {
            "group_id": 100,
            "targets": [{"image": "incoming/a.png", "order": 1, "delay": 0}],
        },
    )

    assert result["ok"] is True
    assert result["sent"][0]["content"] == "[图片: incoming/a.png]"
    assert adapter.sent == []
    assert adapter.sent_images[0]["image_path"] == image_path


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
    assert contains_forbidden("思考过程\nRAG里提到撤回消息")
    assert contains_forbidden("<retrieved_conversation_context source=\"rag\">旧消息</retrieved_conversation_context>")
    assert contains_forbidden("工具结果 · call_123")


def test_contains_forbidden_negative():
    assert not contains_forbidden("普通消息")


def test_build_message_action_text(tmp_path):
    action = build_message_action("你好", None, None, tmp_path / "emoji", tmp_path)
    assert action["kind"] == "text"
    assert action["content"] == "你好"
    assert action["label"] == "你好"


def test_resolve_emoji_path_by_name_without_suffix(tmp_path):
    emoji_dir = tmp_path / "emoji"
    emoji_dir.mkdir()
    expected = emoji_dir / "hi.png"
    expected.write_bytes(b"\x89PNG\r\n\x1a\n")

    assert resolve_emoji_path("hi", emoji_dir) == expected


def test_build_message_action_missing_emoji(tmp_path):
    emoji_dir = tmp_path / "emoji"
    emoji_dir.mkdir()
    with pytest.raises(MessageBuildError, match="表情包不存在"):
        build_message_action(None, "missing", None, emoji_dir, tmp_path)


def test_build_message_action_rejects_emoji_path_traversal(tmp_path):
    emoji_dir = tmp_path / "emoji"
    emoji_dir.mkdir()
    with pytest.raises(MessageBuildError, match="不能包含路径"):
        build_message_action(None, "../etc/passwd", None, emoji_dir, tmp_path)


def test_build_message_action_image_url():
    action = build_message_action(None, None, "https://example.com/a.png", None, None)
    assert action["kind"] == "image"
    assert action["image_url"] == "https://example.com/a.png"
    assert action["label"] == "[图片]"


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


def _simple_pdf_bytes(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            b"3 0 obj\n"
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\n"
            b"endobj\n"
        ),
        b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
        (
            f"5 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"\nendstream\nendobj\n"
        ),
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for obj in objects:
        offsets.append(len(pdf))
        pdf.extend(obj)

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(pdf)


@pytest.mark.asyncio
async def test_read_file_extracts_simple_pdf_text(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pdf = workspace / "simple.pdf"
    pdf.write_bytes(_simple_pdf_bytes("Hello PDF from workspace"))

    ctx = ToolContext(workspace_dir=workspace)
    executor = reg.get_executor(ctx)
    result = await executor("read_file", {"path": "simple.pdf"})

    assert result["ok"] is True
    assert "Hello PDF from workspace" in result["content"]


@pytest.mark.asyncio
async def test_read_file_large_text_is_paginated(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "long.txt").write_text(
        "\n".join(f"line {i}" for i in range(300)),
        encoding="utf-8",
    )

    ctx = ToolContext(workspace_dir=workspace)
    executor = reg.get_executor(ctx)
    first = await executor("read_file", {"path": "long.txt", "max_lines": 20})

    assert first["ok"] is True
    assert first["offset"] == 0
    assert first["next_offset"] == 20
    assert "line 0" in first["content"]
    assert "line 25" not in first["content"]

    second = await executor(
        "read_file",
        {"path": "long.txt", "offset": first["next_offset"], "max_lines": 20},
    )
    assert second["offset"] == 20
    assert "line 20" in second["content"]


@pytest.mark.asyncio
async def test_read_file_writes_complete_artifact_for_large_page(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "long.txt").write_text(
        "\n".join(f"line {i} " + ("内容 " * 30) for i in range(300)),
        encoding="utf-8",
    )

    ctx = ToolContext(
        workspace_dir=workspace,
        tool_result_budgets={},
        tool_result_soft_limit_tokens=80,
        tool_result_hard_cap_tokens=500,
    )
    executor = reg.get_executor(ctx)
    result = await executor("read_file", {"path": "long.txt"})

    assert result["ok"] is True
    assert result["status"] == "artifact"
    assert result["offset"] == 0
    assert result["next_offset"] > 0
    assert result["total_lines"] == 300
    assert "preview" not in result
    assert "content" not in result
    assert result["artifact"]["type"] == "markdown"
    artifact = workspace / result["artifact"]["path"]
    assert artifact.exists()
    text = artifact.read_text(encoding="utf-8")
    assert "line 0" in text
    assert f"line {result['next_offset'] - 1}" in text
    assert "...[已按 token 预算截断]..." not in text


@pytest.mark.asyncio
async def test_get_user_info_strips_binary_buffers():
    class FakeAdapter:
        async def get_user_info(self, user_id: str):
            return {
                "user_id": user_id,
                "nickname": "冰狼",
                "sex": "male",
                "age": 17,
                "extra": {
                    "longNick": "愿岁月清净",
                    "qqLevel": 56,
                    "richBuffer": {"0": 1, "1": 2},
                    "extBuffer": {"buf": "noise"},
                },
            }

    cfg = _make_config()
    reg = build_default_registry(cfg)
    executor = reg.get_executor(ToolContext(adapter=FakeAdapter()))
    result = await executor("get_user_info", {"user_id": 123})

    assert result["ok"] is True
    assert result["info"]["nickname"] == "冰狼"
    assert result["info"]["signature"] == "愿岁月清净"
    dumped = str(result)
    assert "richBuffer" not in dumped
    assert "extBuffer" not in dumped


@pytest.mark.asyncio
async def test_list_files_returns_explicit_pages(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for i in range(60):
        (workspace / f"{i:02d}.txt").write_text("x", encoding="utf-8")

    executor = reg.get_executor(ToolContext(workspace_dir=workspace))
    result = await executor("list_files", {"path": ".", "pattern": "*.txt", "limit": 50})

    assert result["ok"] is True
    assert result["count"] == 60
    assert len(result["entries"]) == 50
    assert result["next_offset"] == 50
    assert "preview" not in result

    second = await executor(
        "list_files",
        {
            "path": ".",
            "pattern": "*.txt",
            "limit": 50,
            "offset": result["next_offset"],
        },
    )
    assert len(second["entries"]) == 10
    assert "next_offset" not in second


@pytest.mark.asyncio
async def test_get_forward_msg_writes_nested_artifact_and_preserves_image_url(tmp_path):
    class FakeAdapter:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_forward_msg(self, forward_id: str):
            self.calls.append(forward_id)
            if forward_id == "outer":
                return [
                    {
                        "sender": {"nickname": "Lilith", "user_id": 1},
                        "raw_message": (
                            "看图 "
                            "[CQ:image,summary=&#91;动画表情&#93;,file=a.png,url=https://img.example/a.png]"
                            "[CQ:forward,id=inner]"
                        ),
                        "message_id": "m1",
                    }
                ]
            return [
                {
                    "sender": {"nickname": "Diana", "user_id": 2},
                    "content": [
                        {"type": "text", "data": {"text": "内层消息"}},
                        {
                            "type": "image",
                            "data": {
                                "summary": "截图",
                                "file": "b.jpg",
                                "url": "https://img.example/b.jpg",
                            },
                        },
                    ],
                }
            ]

    cfg = _make_config()
    reg = build_default_registry(cfg)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = FakeAdapter()
    ctx = ToolContext(
        adapter=adapter,
        workspace_dir=workspace,
    )
    executor = reg.get_executor(ctx)
    result = await executor("get_forward_msg", {"forward_id": "outer"})

    assert result["ok"] is True
    assert result["status"] == "artifact"
    assert "content" not in result
    assert result["artifact"]["type"] == "json"
    assert result["data"]["message_count"] == 2
    assert result["data"]["nested_forward_count"] == 1
    assert result["data"]["image_count"] == 2
    assert adapter.calls == ["outer", "inner"]
    path = workspace / result["artifact"]["path"]
    tree = json.loads(path.read_text(encoding="utf-8"))
    outer_segments = tree["messages"][0]["segments"]
    assert outer_segments[1]["url"] == "https://img.example/a.png"
    nested = outer_segments[2]["node"]
    assert nested["forward_id"] == "inner"
    assert nested["messages"][0]["segments"][1]["url"] == "https://img.example/b.jpg"
    assert "preview" in result
    assert "artifact.path" in result["next"]


@pytest.mark.asyncio
async def test_get_forward_msg_keeps_parent_when_nested_forward_expired(tmp_path):
    class FakeAdapter:
        async def get_forward_msg(self, forward_id: str):
            if forward_id == "outer":
                return [
                    {
                        "sender": {"nickname": "Lilith"},
                        "raw_message": "[CQ:forward,id=expired-inner]",
                    }
                ]
            raise RuntimeError("API get_forward_msg 失败 (retcode=1200): 消息已过期或者为内层消息")

    cfg = _make_config()
    reg = build_default_registry(cfg)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = reg.get_executor(ToolContext(adapter=FakeAdapter(), workspace_dir=workspace))

    result = await executor("get_forward_msg", {"forward_id": "outer"})

    assert result["ok"] is True
    assert result["data"]["expired_forward_count"] == 1
    path = workspace / result["artifact"]["path"]
    tree = json.loads(path.read_text(encoding="utf-8"))
    nested = tree["messages"][0]["segments"][0]["node"]
    assert nested["status"] == "expired"
    assert nested["forward_id"] == "expired-inner"


@pytest.mark.asyncio
async def test_get_forward_msg_preserves_unescaped_summary_image_url(tmp_path):
    class Adapter:
        async def get_forward_msg(self, forward_id: str):
            return [
                {
                    "sender": {"nickname": "Alice"},
                    "raw_message": (
                        "[CQ:image,summary=[图片],file=a.jpg,"
                        "url=https://img.example/a.jpg]"
                    ),
                }
            ]

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cfg = _make_config()
    reg = build_default_registry(cfg)
    executor = reg.get_executor(ToolContext(adapter=Adapter(), workspace_dir=workspace))

    result = await executor("get_forward_msg", {"forward_id": "outer"})

    assert result["ok"] is True
    path = workspace / result["artifact"]["path"]
    tree = json.loads(path.read_text(encoding="utf-8"))
    segment = tree["messages"][0]["segments"][0]
    assert segment["summary"] == "[图片]"
    assert segment["file"] == "a.jpg"
    assert segment["url"] == "https://img.example/a.jpg"


@pytest.mark.asyncio
async def test_get_recent_chat_messages_requires_timeline():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    executor = reg.get_executor(ToolContext(conversation_id="private:123"))

    result = await executor("get_recent_chat_messages", {"limit": 5})

    assert result["ok"] is False
    assert "聊天时间线" in result["error"]


@pytest.mark.asyncio
async def test_get_recent_chat_messages_returns_inline_markdown():
    timeline = ChatTimelineStore()
    timeline.append(_timeline_message("m1", "你好"))
    timeline.append(_timeline_message("m2", "我改口"))
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(
        conversation_id="private:123",
        extras={"chat_timeline": timeline},
    )
    executor = reg.get_executor(ctx)

    result = await executor("get_recent_chat_messages", {"limit": 2})

    assert result["ok"] is True
    assert result["status"] == "inline"
    assert result["data"]["count"] == 2
    assert result["data"]["first_msg_id"] == "m1"
    assert result["data"]["last_msg_id"] == "m2"
    assert "2026-05-30 00:00:00 用户(123)：你好 [msg_id=m1]" in result["content"]
    assert "我改口" in result["content"]


@pytest.mark.asyncio
async def test_get_recent_chat_messages_writes_complete_artifact(tmp_path):
    timeline = ChatTimelineStore()
    for idx in range(20):
        timeline.append(_timeline_message(f"m{idx}", f"消息{idx} " + "很长" * 40))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(
        conversation_id="private:123",
        workspace_dir=workspace,
        extras={"chat_timeline": timeline},
        tool_result_budgets={
            "get_recent_chat_messages": {
                "inline_budget_tokens": 256,
                "artifact_threshold_tokens": 256,
                "hard_cap_tokens": 1200,
            }
        },
    )
    executor = reg.get_executor(ctx)

    result = await executor("get_recent_chat_messages", {"limit": 20})

    assert result["ok"] is True
    assert result["status"] == "artifact"
    assert "content" not in result
    assert result["data"]["count"] == 20
    assert result["data"]["first_msg_id"] == "m0"
    assert result["data"]["last_msg_id"] == "m19"
    path = workspace / result["path"]
    text = path.read_text(encoding="utf-8")
    assert "消息0" in text
    assert "消息19" in text
    assert "msg_id=m0" in text
    assert "msg_id=m19" in text
    assert "已按 token 预算截断" not in text


@pytest.mark.asyncio
async def test_recall_history_writes_complete_artifact(tmp_path):
    from memory import ArchiveStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    archive = ArchiveStore(tmp_path / "archive.jsonl")
    await archive.append_many(
        [
            {
                "role": "user",
                "content": f"历史消息 {idx} " + ("很长" * 80),
                "conversation_id": "group:42",
                "metadata": {"timestamp": f"2026-05-30 00:{idx:02d}"},
            }
            for idx in range(12)
        ]
    )
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(
        archive=archive,
        workspace_dir=workspace,
        tool_result_budgets={
            "recall_history": {
                "inline_budget_tokens": 256,
                "artifact_threshold_tokens": 256,
                "hard_cap_tokens": 1200,
            }
        },
    )
    _approve_stub_tools(ctx, "recall_history")
    executor = reg.get_executor(ctx)

    result = await executor(
        "recall_history",
        {"conversation_id": "group:42", "limit": 12},
    )

    assert result["ok"] is True
    assert result["status"] == "artifact"
    assert "content" not in result
    assert result["artifact"]["count"] == 12
    assert result["count"] == 12
    assert "metadata" not in result["results"][0]
    text = (workspace / result["artifact"]["path"]).read_text(encoding="utf-8")
    assert "历史消息 0" in text
    assert "历史消息 11" in text
    assert "2026-05-30 00:00" in text
    assert "2026-05-30 00:11" in text
    assert "已按 token 预算截断" not in text


@pytest.mark.asyncio
async def test_executor_hard_cap_is_creation_time_stable():
    class _Args(BaseModel):
        pass

    @tool(name="_huge_result", description="huge", args_model=_Args)
    async def _huge_result(args, ctx):
        return {"ok": True, "payload": "x" * 10000}

    new_spec = next(s for s in get_default_specs() if s.name == "_huge_result")
    try:
        reg = ToolRegistry([new_spec])
        ctx = ToolContext(
            tool_result_budgets={},
            tool_result_soft_limit_tokens=100,
            tool_result_hard_cap_tokens=120,
        )
        executor = reg.get_executor(ctx)
        first = await executor("_huge_result", {})
        second = await executor("_huge_result", {})
    finally:
        from tools.base import _DEFAULT_REGISTRY
        _DEFAULT_REGISTRY[:] = [s for s in _DEFAULT_REGISTRY if s.name != "_huge_result"]

    assert first == second
    assert first["_condensed"]["reason"].startswith("工具结果超过中央 hard cap")
    assert "payload" not in first


@pytest.mark.asyncio
async def test_run_python_single_long_line_keeps_stdout_field(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = ToolContext(
        workspace_dir=workspace,
        tool_result_budgets={},
        tool_result_soft_limit_tokens=80,
        tool_result_hard_cap_tokens=800,
    )
    executor = reg.get_executor(ctx)

    result = await executor("run_python", {"code": "print('x' * 5000)"})

    assert result["ok"] is True
    assert result["status"] == "artifact"
    assert "stdout" in result
    assert "preview" not in result
    assert len(result["stdout"]) < 5000
    assert result["stdout_truncated"] is True
    artifact = workspace / result["artifact"]["path"]
    text = artifact.read_text(encoding="utf-8")
    assert "x" * 5000 in text
    assert "...[已按 token 预算截断]..." not in text


class _FakeAdapter:
    name = "fake"
    is_connected = True

    def __init__(self):
        self.uploaded: list = []
        self.recalled: list[str] = []
        self.friend_requests: list[tuple[str, bool, str]] = []
        self.group_requests: list[tuple[str, str, bool, str]] = []

    async def upload_file(self, target, file_path, *, display_name=None):
        self.uploaded.append((target, file_path, display_name))

    async def recall(self, message_id: str) -> bool:
        self.recalled.append(message_id)
        return message_id != "999"

    async def handle_friend_request(self, flag, approve, remark=""):
        self.friend_requests.append((flag, approve, remark))

    async def handle_group_request(self, flag, sub_type, approve, reason=""):
        self.group_requests.append((flag, sub_type, approve, reason))


@pytest.mark.asyncio
async def test_upload_file_outside_whitelist_rejected(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x")

    ctx = ToolContext(adapter=_FakeAdapter(), workspace_dir=allowed)
    _approve_stub_tools(ctx, "upload_file")
    executor = reg.get_executor(ctx)
    result = await executor(
        "upload_file",
        {"target_type": "private", "target_id": 1, "file_path": str(outside)},
    )
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "上传文件失败" in result["brief"]
    assert "workspace" in result["error"]


@pytest.mark.asyncio
async def test_upload_file_inside_whitelist_ok(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    inside = allowed / "x.txt"
    inside.write_text("x")

    fake = _FakeAdapter()
    ctx = ToolContext(adapter=fake, workspace_dir=allowed)
    _approve_stub_tools(ctx, "upload_file")
    executor = reg.get_executor(ctx)
    result = await executor(
        "upload_file",
        {"target_type": "group", "target_id": 1, "file_path": str(inside)},
    )
    assert result["ok"] is True
    assert result["status"] == "done"
    assert result["data"]["file_name"] == "x.txt"
    assert result["data"]["target_type"] == "group"
    assert len(fake.uploaded) == 1
    assert fake.uploaded[0][2] == "x.txt"


@pytest.mark.asyncio
async def test_upload_file_no_adapter():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext()  # 没 adapter
    _approve_stub_tools(ctx, "upload_file")
    executor = reg.get_executor(ctx)
    result = await executor(
        "upload_file",
        {"target_type": "private", "target_id": 1, "file_path": "/tmp/x"},
    )
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "未连接适配器" in result["brief"]


@pytest.mark.asyncio
async def test_recall_message_result_envelope():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    fake = _FakeAdapter()
    executor = reg.get_executor(ToolContext(adapter=fake))

    result = await executor("recall_message", {"message_id": 123})

    assert result["ok"] is True
    assert result["status"] == "done"
    assert result["data"]["message_id"] == "123"
    assert fake.recalled == ["123"]


@pytest.mark.asyncio
async def test_recall_message_failure_result_envelope():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    executor = reg.get_executor(ToolContext(adapter=_FakeAdapter()))

    result = await executor("recall_message", {"message_id": 999})

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "撤回失败" in result["brief"]


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
    assert result["status"] == "failed"
    assert "未注册唤醒回调" in result["brief"]


@pytest.mark.asyncio
async def test_schedule_wakeup_with_callback():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    received: list[tuple[int, str, dict | None, str, str | None]] = []

    async def cb(delay, reminder, target=None, mode="wakeup", message_text=None):
        received.append((delay, reminder, target, mode, message_text))

    ctx = ToolContext(wakeup_cb=cb)
    executor = reg.get_executor(ctx)
    result = await executor(
        "schedule_wakeup", {"delay_seconds": 10, "reminder": "test"}
    )
    assert result["ok"] is True
    assert result["status"] == "done"
    assert result["data"]["delay_seconds"] == 10
    assert result["data"]["mode"] == "wakeup"
    assert received == [(10, "test", None, "wakeup", None)]


@pytest.mark.asyncio
async def test_schedule_wakeup_uses_default_reply_target():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    received: list[tuple[int, str, dict | None, str, str | None]] = []

    async def cb(delay, reminder, target=None, mode="wakeup", message_text=None):
        received.append((delay, reminder, target, mode, message_text))

    ctx = ToolContext(
        wakeup_cb=cb,
        extras={"default_reply_target": {"target_type": "private", "target_id": 123}},
    )
    executor = reg.get_executor(ctx)
    result = await executor(
        "schedule_wakeup", {"delay_seconds": 10, "reminder": "test"}
    )

    assert result["ok"] is True
    assert result["data"]["target"] == {"target_type": "private", "target_id": 123}
    delay, reminder, target, mode, message_text = received[0]
    assert delay == 10
    assert target == {"target_type": "private", "target_id": 123}
    assert mode == "wakeup"
    assert message_text is None
    assert "任务说明：test" in reminder
    assert "提醒目标：private:123" in reminder
    assert "不要把历史中已经完成" in reminder


@pytest.mark.asyncio
async def test_schedule_wakeup_includes_latest_user_message():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    received: list[tuple[int, str, dict | None, str, str | None]] = []

    async def cb(delay, reminder, target=None, mode="wakeup", message_text=None):
        received.append((delay, reminder, target, mode, message_text))

    ctx = ToolContext(
        wakeup_cb=cb,
        extras={
            "default_reply_target": {"target_type": "private", "target_id": 123},
            "latest_user_message": "30秒后单独发个消息，发个“到点了”就行",
        },
    )
    executor = reg.get_executor(ctx)
    result = await executor(
        "schedule_wakeup",
        {"delay_seconds": 30, "reminder": "30秒后发送“到点了”"},
    )

    assert result["ok"] is True
    reminder = received[0][1]
    assert "设置时用户原话：30秒后单独发个消息" in reminder
    assert "到点了" in reminder


@pytest.mark.asyncio
async def test_schedule_wakeup_send_message_mode_uses_default_target():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    received: list[tuple[int, str, dict | None, str, str | None]] = []

    async def cb(delay, reminder, target=None, mode="wakeup", message_text=None):
        received.append((delay, reminder, target, mode, message_text))

    ctx = ToolContext(
        wakeup_cb=cb,
        extras={"default_reply_target": {"target_type": "private", "target_id": 123}},
    )
    executor = reg.get_executor(ctx)
    result = await executor(
        "schedule_wakeup",
        {
            "delay_seconds": 30,
            "mode": "send_message",
            "message_text": "到点了",
        },
    )

    assert result["ok"] is True
    assert result["data"]["message_text"] == "到点了"
    delay, reminder, target, mode, message_text = received[0]
    assert delay == 30
    assert target == {"target_type": "private", "target_id": 123}
    assert mode == "send_message"
    assert message_text == "到点了"
    assert "消息内容：到点了" in reminder


@pytest.mark.asyncio
async def test_schedule_wakeup_send_message_requires_target():
    cfg = _make_config()
    reg = build_default_registry(cfg)

    async def cb(*_args):
        raise AssertionError("缺少目标时不应注册定时任务")

    ctx = ToolContext(wakeup_cb=cb)
    executor = reg.get_executor(ctx)
    result = await executor(
        "schedule_wakeup",
        {
            "delay_seconds": 30,
            "mode": "send_message",
            "message_text": "到点了",
        },
    )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "缺少发送目标" in result["brief"]
    assert "mode=send_message 需要" in result["error"]


@pytest.mark.asyncio
async def test_request_action_tools_result_envelope():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    fake = _FakeAdapter()
    executor = reg.get_executor(ToolContext(adapter=fake))

    friend = await executor(
        "set_friend_add_request",
        {"flag": "f1", "approve": True, "remark": "熟人"},
    )
    group = await executor(
        "set_group_add_request",
        {"flag": "g1", "sub_type": "invite", "approve": False, "reason": "暂不加入"},
    )

    assert friend["ok"] is True
    assert friend["status"] == "done"
    assert friend["data"]["flag"] == "f1"
    assert group["ok"] is True
    assert group["status"] == "done"
    assert group["data"]["approve"] is False
    assert fake.friend_requests == [("f1", True, "熟人")]
    assert fake.group_requests == [("g1", "invite", False, "暂不加入")]


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
    ctx = ToolContext(important=im, conversation_id="private:123")
    executor = reg.get_executor(ctx)
    result = await executor(
        "save_important_memory", {"memory_text": "记住张三是朋友"}
    )
    assert result["ok"] is True
    assert result["saved"] is True
    assert result["scope"] == "user:123"
    assert len(im.items()) == 1
    assert im.items()[0]["scope"] == "user:123"


@pytest.mark.asyncio
async def test_save_memory_explicit_scope_and_pinned(tmp_path):
    from memory import ImportantMemoryManager

    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()

    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(important=im, conversation_id="private:123")
    executor = reg.get_executor(ctx)
    result = await executor(
        "save_important_memory",
        {"memory_text": "全局稳定约定", "scope": "global", "pinned": True},
    )

    assert result["ok"] is True
    assert result["scope"] == "global"
    assert result["pinned"] is True
    assert im.items()[0]["scope"] == "global"
    assert im.items()[0]["pinned"] is True


@pytest.mark.asyncio
async def test_update_memory_with_manager(tmp_path):
    from memory import ImportantMemoryManager

    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()
    await im.replace_all([{"timestamp": "mem-1", "content": "张三是朋友"}])

    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(important=im)
    executor = reg.get_executor(ctx)
    result = await executor(
        "update_important_memory",
        {
            "memory_id": "mem-1",
            "memory_text": "张三是朋友，生日是7月8日",
            "reason": "补充生日",
        },
    )

    assert result["ok"] is True
    assert result["updated"] is True
    assert im.items()[0]["content"] == "张三是朋友，生日是7月8日"


@pytest.mark.asyncio
async def test_update_memory_exact_duplicate_returns_existing_id(tmp_path):
    from memory import ImportantMemoryManager

    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()
    await im.replace_all(
        [
            {"timestamp": "mem-1", "content": "张三是朋友"},
            {"timestamp": "mem-2", "content": "李四是朋友"},
        ]
    )

    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(important=im)
    executor = reg.get_executor(ctx)
    result = await executor(
        "update_important_memory",
        {"memory_id": "mem-2", "memory_text": "张三是朋友"},
    )

    assert result["ok"] is True
    assert result["status"] == "exact_duplicate"
    assert result["updated"] is False
    assert result["existing_id"] == "mem-1"
    assert im.items()[1]["content"] == "李四是朋友"


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


@pytest.mark.asyncio
async def test_summarize_conversation_starts_agent_task(tmp_path):
    from memory import ArchiveStore, HistoryManager

    archive = ArchiveStore(tmp_path / "archive.jsonl")
    history = HistoryManager(tmp_path / "history.jsonl")
    await archive.append_many(
        [
            {
                "role": "user",
                "content": "归档里提到茶会安排",
                "conversation_id": "group:42",
            }
        ]
    )
    await history.add_user_message("活跃区继续讨论茶会", conversation_id="group:42")
    calls = []

    async def fake_agent_task(payload):
        calls.append(payload)
        return {
            "ok": True,
            "status": "completed",
            "task_id": "agent-test",
            "result_file": "agent_tasks/agent-test/result.md",
            "content": "茶会总结",
        }

    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(
        archive=archive,
        history=history,
        agent_task_cb=fake_agent_task,
        conversation_id="group:42",
    )
    _approve_stub_tools(ctx, "summarize_conversation")
    executor = reg.get_executor(ctx)

    result = await executor(
        "summarize_conversation",
        {"range_hint": "茶会", "max_tokens": 512},
    )

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["task_id"] == "agent-test"
    assert calls
    assert "茶会" in calls[0]["prompt"]
    assert calls[0]["sources"][0]["type"] == "conversation_history"
    assert calls[0]["sources"][0]["conversation_id"] == "group:42"
    assert calls[0]["sources"][0]["time_range"] == "茶会"


@pytest.mark.asyncio
async def test_start_agent_task_requires_prompt_and_calls_runtime():
    calls = []

    async def fake_agent_task(payload):
        calls.append(payload)
        return {
            "ok": True,
            "status": "completed",
            "task_id": "agent-1",
            "result_file": "agent_tasks/agent-1/result.md",
            "content": "提取结果",
        }

    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(agent_task_cb=fake_agent_task)
    _approve_stub_tools(ctx, "start_agent_task")
    executor = reg.get_executor(ctx)

    result = await executor(
        "start_agent_task",
        {
            "prompt": "提取对话，保留发送者",
            "sources": [{"type": "inline_text", "value": "A: hi"}],
            "output_format": "markdown",
            "max_loops": 30,
            "timeout_seconds": 900,
        },
    )

    assert result["ok"] is True
    assert result["task_id"] == "agent-1"
    assert calls[0]["prompt"] == "提取对话，保留发送者"
    assert calls[0]["sources"][0]["type"] == "inline_text"
    assert calls[0]["max_loops"] == 30
    assert calls[0]["timeout_seconds"] == 900


@pytest.mark.asyncio
async def test_start_agent_task_rejects_image_ref_without_vision_service():
    calls = []

    async def fake_agent_task(payload):
        calls.append(payload)
        return {"ok": True, "status": "completed"}

    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(agent_task_cb=fake_agent_task)
    _approve_stub_tools(ctx, "start_agent_task")
    executor = reg.get_executor(ctx)

    result = await executor(
        "start_agent_task",
        {
            "prompt": "看看这张图",
            "sources": [{"type": "image_ref", "value": "incoming/a.png"}],
            "output_format": "markdown",
        },
    )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "图片理解能力" in result["error"]
    assert calls == []


@pytest.mark.asyncio
async def test_start_agent_task_rejects_image_workspace_path_without_vision_service():
    calls = []

    async def fake_agent_task(payload):
        calls.append(payload)
        return {"ok": True, "status": "completed"}

    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(agent_task_cb=fake_agent_task)
    _approve_stub_tools(ctx, "start_agent_task")
    executor = reg.get_executor(ctx)

    result = await executor(
        "start_agent_task",
        {
            "prompt": "描述这张图片的内容",
            "sources": [{"type": "workspace_path", "value": "incoming/a.jpg"}],
            "output_format": "markdown",
        },
    )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "图片理解能力" in result["error"]
    assert calls == []


@pytest.mark.asyncio
async def test_start_agent_task_rejects_image_retry_after_describe_image_failure():
    class FailingVision:
        async def describe(self, image_url: str, prompt: str = ""):
            raise RuntimeError("vision failed")

    calls = []

    async def fake_agent_task(payload):
        calls.append(payload)
        return {"ok": True, "status": "completed"}

    cfg = _make_config(vision_enabled=True)
    reg = build_default_registry(cfg)
    ctx = ToolContext(vision=FailingVision(), agent_task_cb=fake_agent_task)
    _approve_stub_tools(ctx, "start_agent_task")
    executor = reg.get_executor(ctx)

    image_result = await executor(
        "describe_image",
        {"image_url": "https://example.com/a.jpg", "question": "看图"},
    )
    assert image_result["ok"] is False

    result = await executor(
        "start_agent_task",
        {
            "prompt": "描述这张图片的内容",
            "sources": [{"type": "workspace_path", "value": "incoming/a.jpg"}],
            "output_format": "markdown",
        },
    )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "不能启动子 Agent 代替看图" in result["error"]
    assert calls == []


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
    assert result["status"] == "inline"
    assert result["count"] == 1
    assert result["friends"][0]["nickname"] == "A"


@pytest.mark.asyncio
async def test_list_contacts_returns_explicit_pages():
    """联系人列表按 offset/limit 显式分页，不依赖压缩器截断。"""
    from adapters.types import FriendInfo

    class FakeAd:
        name = "fake"
        is_connected = True

        async def list_friends(self):
            return [FriendInfo(user_id=str(i), nickname=f"F{i}") for i in range(60)]

    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(adapter=FakeAd())  # type: ignore
    executor = reg.get_executor(ctx)
    result = await executor("list_contacts", {"scope": "friends", "limit": 50})

    assert result["ok"] is True
    assert result["count"] == 60
    assert len(result["friends"]) == 50
    assert result["next_offset"] == 50
    assert "_condensed" not in result

    second = await executor(
        "list_contacts",
        {"scope": "friends", "limit": 50, "offset": result["next_offset"]},
    )
    assert len(second["friends"]) == 10
    assert "next_offset" not in second


@pytest.mark.asyncio
async def test_list_contacts_group_members_requires_group_id():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext(adapter=object())  # type: ignore  # 仅占位
    executor = reg.get_executor(ctx)
    result = await executor("list_contacts", {"scope": "group_members"})
    assert result["ok"] is False
    assert "group_id" in result["error"]
