"""测试 agents 层：persona_loader / context_builder / behavior_prompt 切换 / runner Task Contract。"""

from __future__ import annotations

import pytest

from agents.behavior_prompt import (
    CORE_RULES,
    build_tool_use_protocol,
)
from agents.context_builder import (
    build_admin_info,
    build_combined_system_prompt,
    build_messages,
    build_task_context,
)
from agents.persona_loader import (
    Persona,
    find_persona_dir,
    list_available_personas,
    load_persona,
    validate_persona_name,
)
from agents.persona_gen_agent import PersonaBrief, PersonaGenResult, render_persona_file
from agents.proactive_agent import ProactiveRouterAgent, _is_action_decision
from app_config.schema import AgentConfig
from providers.base import CompletionResult


# ============================================================
# persona_loader
# ============================================================


def test_validate_persona_name_ok():
    validate_persona_name("yuexi")
    validate_persona_name("a_b-c123")
    # 中文与混合应通过（人格名常用中文）
    validate_persona_name("小明")
    validate_persona_name("寒月-01")
    validate_persona_name("小桃")


def test_validate_persona_name_rejects_bad():
    # 拒：空 / 长 / 路径敏感字符 / 空格 / 特殊符号
    for bad in ["", "..", "hi/x", "a\\b", "a:b", 'a"b', "a*b", "a?b", "a<b", "a|b", "a b", "a" * 65]:
        with pytest.raises(ValueError):
            validate_persona_name(bad)


def test_find_persona_dir_missing(tmp_paths):
    assert find_persona_dir(tmp_paths, "nonexistent") is None


def test_find_persona_dir_returns_path(tmp_paths):
    # 所有人格平级在 PERSONAS_DIR 下，找到就返回路径
    (tmp_paths.PERSONAS_DIR / "alice").mkdir(parents=True)
    (tmp_paths.PERSONAS_DIR / "alice" / "persona_prompt.py").write_text(
        "PERSONA_PROMPT = 'x'", encoding="utf-8"
    )
    found = find_persona_dir(tmp_paths, "alice")
    assert found == tmp_paths.PERSONAS_DIR / "alice"


def test_load_persona_basic(tmp_paths):
    (tmp_paths.PERSONAS_DIR / "alice").mkdir(parents=True)
    (tmp_paths.PERSONAS_DIR / "alice" / "persona_prompt.py").write_text(
        'PERSONA_PROMPT = "你是 Alice"\n'
        'PERSONA_VARS = {"name": "Alice", "admins": [{"qq": 1, "name": "Lily"}]}\n',
        encoding="utf-8",
    )
    p = load_persona(tmp_paths, "alice")
    assert p.name == "alice"
    assert p.prompt == "你是 Alice"
    assert p.vars["name"] == "Alice"
    assert p.get_admins() == [{"qq": 1, "name": "Lily"}]
    assert p.display_name() == "Alice"


def test_load_persona_missing_prompt_raises(tmp_paths):
    (tmp_paths.PERSONAS_DIR / "bad").mkdir(parents=True)
    (tmp_paths.PERSONAS_DIR / "bad" / "persona_prompt.py").write_text(
        "X = 1", encoding="utf-8"
    )
    with pytest.raises(ValueError):
        load_persona(tmp_paths, "bad")


def test_load_persona_not_found(tmp_paths):
    with pytest.raises(FileNotFoundError):
        load_persona(tmp_paths, "ghost")


def test_list_available_personas(tmp_paths):
    # 所有人格平级。下划线开头的目录被忽略（保留作为系统目录）
    (tmp_paths.PERSONAS_DIR / "p1").mkdir(parents=True)
    (tmp_paths.PERSONAS_DIR / "p1" / "persona_prompt.py").write_text(
        'PERSONA_PROMPT = "1"', encoding="utf-8"
    )
    (tmp_paths.PERSONAS_DIR / "p2").mkdir(parents=True)
    (tmp_paths.PERSONAS_DIR / "p2" / "persona_prompt.py").write_text(
        'PERSONA_PROMPT = "2"', encoding="utf-8"
    )
    # 下划线开头的应被忽略
    (tmp_paths.PERSONAS_DIR / "_hidden").mkdir(parents=True)
    (tmp_paths.PERSONAS_DIR / "_hidden" / "persona_prompt.py").write_text(
        'PERSONA_PROMPT = "x"', encoding="utf-8"
    )

    found = list_available_personas(tmp_paths)
    assert "p1" in found
    assert "p2" in found
    assert "_hidden" not in found


# ============================================================
# behavior_prompt: memory_mode 切换
# ============================================================


def test_tool_use_protocol_file_mode():
    s = build_tool_use_protocol("file")
    assert "save_important_memory" in s
    assert "必须主动保存" in s
    assert "自动管理" not in s


def test_tool_use_protocol_rag_mode():
    s = build_tool_use_protocol("rag")
    assert "RAG 语义检索" in s
    assert "save_important_memory" in s
    assert "必须主动保存" in s


def test_tool_use_protocol_default_is_file():
    assert build_tool_use_protocol() == build_tool_use_protocol("file")


def test_tool_use_protocol_unknown_mode_defaults_to_file():
    """未知 mode 应回退到 file 模式（健壮性）。"""
    s = build_tool_use_protocol("nonexistent")
    assert "必须主动保存" in s


def test_emoji_hint_in_protocol():
    """关于发图片表情包的明确提示必须在协议里。"""
    s = build_tool_use_protocol("file")
    assert "表情包" in s
    assert "image" in s
    assert "别忘了" in s or "不需要犹豫" in s  # 鼓励性表述


# ============================================================
# context_builder
# ============================================================


def _persona(prompt: str = "你是 Debata", admins: list[dict] | None = None) -> Persona:
    return Persona(
        name="test",
        prompt=prompt,
        vars={"name": "Debata", "admins": admins or []},
    )


def test_build_combined_system_prompt_includes_persona():
    p = _persona("你是云月晞")
    sys = build_combined_system_prompt(p)
    assert "你是云月晞" in sys
    assert '<core_rules priority="critical">' in sys
    assert '<persona priority="high">' in sys


def test_build_combined_system_prompt_includes_human_chat_patterns():
    """human_chat_patterns 必须被注入到 system prompt 里 —— 这是"像人"的核心规则。"""
    p = _persona()
    sys = build_combined_system_prompt(p)
    assert '<human_chat_patterns priority="high">' in sys
    # 校验几个关键概念在 prompt 里（避免 prompt 被精简到丢规则）
    assert "极短为主" in sys
    assert "拆条瀑布" in sys
    assert "不打句号" in sys
    assert "不告别" in sys


def test_human_chat_patterns_after_persona_before_tools():
    """human_chat_patterns 应该紧跟在 persona 之后、tool_use_protocol 之前。

    顺序：先认识"我是谁"，再知道"人是怎么聊天的"，再看怎么用工具。
    """
    p = _persona()
    sys = build_combined_system_prompt(p)
    persona_pos = sys.find('<persona priority="high">')
    human_pos = sys.find('<human_chat_patterns priority="high">')
    tools_pos = sys.find('<tool_use_protocol priority="high">')
    assert persona_pos < human_pos < tools_pos, (
        f"顺序应该是 persona < human_chat_patterns < tool_use_protocol，"
        f"实际：{persona_pos=} {human_pos=} {tools_pos=}"
    )


def test_build_combined_system_prompt_memory_mode_default():
    p = _persona()
    sys = build_combined_system_prompt(p)
    assert "必须主动保存" in sys  # 文件模式默认


def test_build_combined_system_prompt_rag_mode_no_active_save():
    p = _persona()
    sys = build_combined_system_prompt(p, memory_mode="rag")
    assert "RAG 语义检索" in sys
    assert "save_important_memory" in sys
    assert "必须主动保存" in sys


def test_build_combined_system_prompt_with_important_memory():
    p = _persona()
    sys = build_combined_system_prompt(
        p, important_memory_text="[重要记忆]\n- 张三是朋友"
    )
    assert "<long_term_memory" in sys
    assert "张三是朋友" in sys


def test_build_combined_system_prompt_without_memory_skips_tag():
    p = _persona()
    sys = build_combined_system_prompt(p, important_memory_text="")
    assert "<long_term_memory" not in sys


def test_build_admin_info_empty():
    p = _persona(admins=[])
    assert build_admin_info(p) == ""


def test_build_admin_info_with_admins():
    p = _persona(admins=[{"qq": 123, "name": "Lily", "role": "creator"}])
    info = build_admin_info(p)
    assert "Lily" in info
    assert "123" in info
    assert "creator" in info
    assert '<admin_info priority="high">' in info


def test_persona_brief_includes_admin_info():
    brief = PersonaBrief(name="Mika", admin_name="Lily", admin_qq="123456")
    block = brief.to_brief_block()
    assert "管理员/用户信息" in block
    assert "Lily" in block
    assert "123456" in block


def test_render_persona_file_writes_admins():
    brief = PersonaBrief(name="Mika")
    result = PersonaGenResult(persona_prompt="<identity>Mika</identity>", display_name="Mika")
    text = render_persona_file(
        result,
        brief,
        admins=[{"qq": 123456, "name": "Lily", "role": "owner"}],
    )
    assert '"admins":' in text
    assert '"qq": 123456' in text
    assert '"name": "Lily"' in text


def test_build_task_context_empty():
    assert build_task_context("") == ""


def test_build_task_context_with_content():
    s = build_task_context("现在是 2026 年 5 月")
    assert "<task_context" in s
    assert "现在是 2026 年 5 月" in s


def test_build_task_context_with_refocus():
    s = build_task_context("ctx", refocus_hint="本轮目标：回应 Lily")
    assert "ctx" in s
    assert "本轮焦点提醒" in s
    assert "Lily" in s


def test_build_messages_structure():
    p = _persona(admins=[{"qq": 1, "name": "A"}])
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    msgs = build_messages(
        p,
        history,
        important_memory_text="[重要记忆]\n- X",
        current_context="时间：2026/05/23",
    )

    # 顺序：system(combined) → system(admin) → user → assistant → system(task_context)
    assert msgs[0]["role"] == "system"
    assert "<persona" in msgs[0]["content"]
    assert msgs[1]["role"] == "system"
    assert "<admin_info" in msgs[1]["content"]
    assert msgs[2]["role"] == "user"
    assert msgs[3]["role"] == "assistant"
    assert msgs[4]["role"] == "system"
    assert "<task_context" in msgs[4]["content"]
    assert "2026/05/23" in msgs[4]["content"]


def test_build_messages_no_admin_no_context():
    p = _persona(admins=[])
    msgs = build_messages(p, [], important_memory_text="", current_context="")
    # 只有 1 个 system（combined system prompt）
    assert len(msgs) == 1
    assert msgs[0]["role"] == "system"


def test_build_messages_system_override():
    """system_override 应跳过结构化拼接。"""
    p = _persona()
    msgs = build_messages(
        p,
        [{"role": "user", "content": "x"}],
        system_override="自定义 system",
    )
    assert msgs[0]["content"] == "自定义 system"
    # 不应包含 persona/core_rules
    assert "core_rules" not in msgs[0]["content"]


def test_build_messages_memory_mode_propagates():
    p = _persona()
    msgs_rag = build_messages(p, [], memory_mode="rag")
    msgs_file = build_messages(p, [], memory_mode="file")
    assert "RAG 语义检索" in msgs_rag[0]["content"]
    assert "必须主动保存" in msgs_rag[0]["content"]
    assert "必须主动保存" in msgs_file[0]["content"]


# ============================================================
# Memory hooks（新加的 on_append）
# ============================================================


@pytest.mark.asyncio
async def test_history_on_append_called(tmp_path):
    """订阅 on_append 后，每次写入都应触发回调。"""
    from memory import HistoryManager

    h = HistoryManager(tmp_path / "h.jsonl")
    received: list[list[dict]] = []

    async def cb(records):
        received.append(records)

    h.on_append(cb)
    await h.add_user_message("hi")
    await h.add_assistant_message("hello")

    # 给 task 时间执行
    import asyncio
    await asyncio.sleep(0.05)

    assert len(received) == 2
    assert received[0][0]["role"] == "user"
    assert received[1][0]["role"] == "assistant"


@pytest.mark.asyncio
async def test_history_on_append_batch(tmp_path):
    from memory import HistoryManager

    h = HistoryManager(tmp_path / "h.jsonl")
    received: list[list[dict]] = []

    async def cb(records):
        received.append(records)

    h.on_append(cb)
    await h.add_records([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])

    import asyncio
    await asyncio.sleep(0.05)

    assert len(received) == 1
    assert len(received[0]) == 2


@pytest.mark.asyncio
async def test_history_on_append_multiple_subscribers(tmp_path):
    """允许多个订阅者。"""
    from memory import HistoryManager

    h = HistoryManager(tmp_path / "h.jsonl")
    counts = [0, 0]

    async def cb1(records):
        counts[0] += 1

    async def cb2(records):
        counts[1] += 1

    h.on_append(cb1)
    h.on_append(cb2)
    await h.add_user_message("x")

    import asyncio
    await asyncio.sleep(0.05)

    assert counts == [1, 1]


@pytest.mark.asyncio
async def test_important_force_save_keyword(tmp_path):
    """关键词强制保存：命中关键词应保存，未命中应跳过。"""
    from memory import ImportantMemoryManager

    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()

    # 命中 "记住"
    result = await im.force_save_from_keyword("记住我的 QQ 是 123456")
    assert result["saved"] is True
    assert result["matched_keyword"] == "记住"

    result_note = await im.force_save_from_keyword("记一下：7月7日参加选拔赛")
    assert result_note["saved"] is True
    assert result_note["matched_keyword"] == "记一下"
    assert result_note["content"] == "7月7日参加选拔赛"

    # 未命中
    result2 = await im.force_save_from_keyword("今天天气真好")
    assert result2["saved"] is False
    assert result2["matched_keyword"] is None


@pytest.mark.asyncio
async def test_important_force_save_custom_keywords(tmp_path):
    from memory import ImportantMemoryManager

    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()

    # 自定义关键词
    result = await im.force_save_from_keyword(
        "我喜欢猫", keywords=["喜欢"]
    )
    assert result["saved"] is True
    assert result["matched_keyword"] == "喜欢"


@pytest.mark.asyncio
async def test_important_force_save_bypasses_dedup(tmp_path):
    """强制保存不应被去重检查阻止（直接写入）。"""
    from memory import ImportantMemoryManager

    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()
    await im.force_save_from_keyword("记住第一条")
    await im.force_save_from_keyword("记住第一条")  # 相同内容
    # 两条都被保存
    assert len(im.items()) == 2
    # 都标记了来源
    for item in im.items():
        assert item.get("source", "").startswith("keyword:")


# ============================================================
# Schema 新字段
# ============================================================


def test_schema_long_term_memory_config_defaults():
    from app_config import LongTermMemoryConfig

    c = LongTermMemoryConfig()
    assert c.mode == "file"
    assert c.keyword_trigger_save is True
    assert c.rag_top_k == 5


def test_schema_embedding_feature_default_disabled():
    from app_config import EmbeddingFeatureConfig

    c = EmbeddingFeatureConfig()
    assert c.enabled is False
    assert c.type == "api"
    assert c.local_quality == "performance"


def test_schema_refocus_interval_default():
    from app_config import AgentConfig

    c = AgentConfig(provider="x", model="y")
    assert c.refocus_interval == 5


def test_runner_assistant_record_preserves_empty_reasoning_with_blocks():
    from agents.runner import AgentRunner
    from providers.base import CompletionResult

    result = CompletionResult(
        content="ok",
        reasoning_content="",
        reasoning_blocks=[
            {"type": "thinking", "thinking": "", "signature": "sig"}
        ],
    )

    record = AgentRunner._build_assistant_record(result)

    assert record["reasoning_content"] == ""
    assert record["reasoning_blocks"] == [
        {"type": "thinking", "thinking": "", "signature": "sig"}
    ]


def test_runner_assistant_record_preserves_reasoning_content():
    from agents.runner import AgentRunner
    from providers.base import CompletionResult

    record = AgentRunner._build_assistant_record(
        CompletionResult(content="ok", reasoning_content="plan")
    )

    assert record["reasoning_content"] == "plan"


def test_proactive_router_treats_only_clean_take_actions_as_action():
    assert _is_action_decision("TAKE_ACTIONS") is True
    assert _is_action_decision(" TAKE_ACTIONS ") is True
    assert _is_action_decision("<｜｜DSML｜｜TOOL_CALLS>\n<｜｜DSML｜｜INVOKE NAME=send_private_messages>") is False
    assert _is_action_decision("两分钟到了，提醒用户。\n\n<｜｜DSML｜｜TOOL_CALLS>") is False
    assert _is_action_decision("NO_ACTIONS") is False


@pytest.mark.asyncio
async def test_proactive_router_provider_dsml_content_returns_false():
    class FakeProvider:
        async def chat_completion(self, *_args, **_kwargs):
            return CompletionResult(content="<｜｜DSML｜｜TOOL_CALLS>\n<｜｜DSML｜｜INVOKE NAME=send_group_message>")

    agent = ProactiveRouterAgent(
        FakeProvider(),
        AgentConfig(provider="fake", model="router", max_tokens=64),
    )

    assert await agent.should_act([]) is False
