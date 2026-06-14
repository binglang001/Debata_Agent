"""测试 agents 层：persona_loader / context_builder / behavior_prompt 切换 / runner Task Contract。"""

from __future__ import annotations

import json

import pytest

from agents.base import AgentRunResult
from agents.behavior_prompt import (
    build_tool_use_protocol,
)
from agents.context_builder import (
    build_admin_info,
    build_combined_system_prompt,
    build_messages,
    build_task_context,
)
from agents.persona_gen_agent import (
    PERSONA_GEN_SYSTEM_PROMPT,
    PERSONA_REFINE_SYSTEM_PROMPT,
    PersonaBrief,
    PersonaGenAgent,
    PersonaGenResult,
    render_persona_file,
)
from agents.persona_loader import (
    Persona,
    find_persona_dir,
    list_available_personas,
    load_persona,
    validate_persona_name,
)
from agents.proactive_agent import ProactiveRouterAgent, _is_action_decision
from agents.runner import AgentRunner, _kv_prompt_diagnostics
from app_config.schema import AgentConfig, SummarizeConfig
from providers.base import CompletionResult, IProvider, ToolCall, Usage, normalize_messages

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


@pytest.mark.parametrize(
    ("raw_age", "expected"),
    [
        (18, 18),
        (0, 0),
        (-1, None),
        ("18", 18),
        (" 18 ", 18),
        ("0018", 18),
        ("0", 0),
        ("", None),
        ("   ", None),
        ("18.5", None),
        ("abc", None),
        ("-1", None),
        (None, None),
    ],
)
def test_persona_get_age_parses_int_and_string_cases(raw_age, expected):
    persona = Persona(name="alice", prompt="你是 Alice", vars={"age": raw_age})

    assert persona.get_age() == expected


@pytest.mark.parametrize("raw_age", [True, False])
def test_persona_get_age_rejects_bool(raw_age):
    persona = Persona(name="alice", prompt="你是 Alice", vars={"age": raw_age})

    assert persona.get_age() is None


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
    assert "update_important_memory" in s
    assert "必须主动保存" in s
    assert "自动管理" not in s


def test_tool_use_protocol_rag_mode():
    s = build_tool_use_protocol("rag")
    assert "RAG 会话向量检索" in s
    assert "retrieved_conversation_context" in s
    assert "save_important_memory" in s
    assert "update_important_memory" in s
    assert "没有 save_important_memory" not in s
    assert "不能指望 RAG 一定召回" in s


def test_tool_use_protocol_default_is_file():
    assert build_tool_use_protocol() == build_tool_use_protocol("file")


def test_tool_use_protocol_default_has_no_physiology_block():
    s = build_tool_use_protocol("file")
    assert "<physiology>" not in s
    assert "meal_type" not in s
    assert "duration_minutes 填 1-720 分钟" not in s


def test_tool_use_protocol_includes_static_physiology_block_when_enabled():
    s = build_tool_use_protocol("file", eat_tool=True, sleep_tool=True)

    assert "<physiology>" in s
    assert "</physiology>" in s
    assert "meal_type" in s
    assert "description" in s
    assert "duration_minutes 填 1-720 分钟" in s
    assert "reason" in s
    assert "期间入站消息只会记录和进入潜意识缓冲" in s
    assert "调用 eat / sleep 前先发送自然收尾消息" in s
    assert "不要硬套固定话术" in s
    assert "尽量不要再主动抛问题、索要选择" in s
    assert "调用前先可见地自然告知要去吃饭" in s
    assert "调用前先可见地自然告知要去睡觉或休息" in s
    assert s.index("</memory>") < s.index("<physiology>") < s.index("<no_action>")


def test_tool_use_protocol_physiology_block_follows_individual_switches():
    eat_only = build_tool_use_protocol("file", eat_tool=True)
    sleep_only = build_tool_use_protocol("file", sleep_tool=True)

    assert "### eat" in eat_only
    assert "meal_type" in eat_only
    assert "### sleep" not in eat_only
    assert "duration_minutes 填 1-720 分钟" not in eat_only
    assert "### sleep" in sleep_only
    assert "reason" in sleep_only
    assert "### eat" not in sleep_only
    assert "meal_type" not in sleep_only


def test_tool_use_protocol_unknown_mode_defaults_to_file():
    """未知 mode 应回退到 file 模式（健壮性）。"""
    s = build_tool_use_protocol("nonexistent")
    assert "必须主动保存" in s


def test_emoji_hint_in_protocol():
    """关于发图片表情包的明确提示必须在协议里。"""
    s = build_tool_use_protocol("file")
    assert "表情包" in s
    assert "emoji" in s
    assert "image` 字段用于发送普通图片" in s
    assert "正常短回复" in s


def test_tool_trigger_policy_in_protocol():
    s = build_tool_use_protocol("file")
    assert "工具是内部观察" in s
    assert "看完不代表必须回复" in s
    assert "get_recent_chat_messages" in s
    assert "describe_image" in s
    assert "get_forward_msg" in s
    assert "read_file" in s
    assert "needs_review" in s
    assert "needs_review / interrupted" in s
    assert "commit_send_attempt" in s


def test_tool_use_protocol_documents_runtime_contracts_without_legacy_terms():
    s = build_tool_use_protocol("rag")
    lower = s.lower()

    assert "finish_after_success" in s
    assert "no_action 是唯一显式沉默终止工具" in s
    assert "needs_review_again 仍属于同一个 send_attempt_id" in s
    assert "atomic 和 send_* 的 ignore_review_interrupts 都不会绕过发送前 needs_review / needs_review_again" in s
    assert "send_* 的 ignore_review_interrupts=true 只用于发送被系统接受后的打断处理" in s
    assert "不能绕过撤回、禁言、无权限、退群、发送失败等硬错误" in s
    assert "复核后重新调用发送工具改写新消息时，先复核新消息" in s
    assert "commit_send_attempt 的 ignore_review_interrupts 保持旧 attempt 复核语义" in s
    assert "复核旧回复是否会脱离上下文" in s
    assert "私聊/群聊都不要机械每条引用" in s
    assert "普通顺序闲聊、紧邻上一条且无歧义时自然回复即可" in s
    assert "延迟回复、吃饭睡觉后接旧话、主动思考接旧话" in s
    assert "回复非最新消息、多人连续插话、回答被引用的消息" in s
    assert "没有可靠消息 ID 时不要伪造" in s
    assert '"行/OK/可以/知道了/不要"这类简短确认' in s
    assert "只要可能不清楚回谁，就引用、@、点名或自然语言锚定" in s
    assert "私聊里隔了一段时间才接旧话" in s
    assert "中文约 1.5 字/秒" in s
    assert "每条 target 都必须填写 delay" in s
    assert "本条发出后到下一条发出前的等待秒数" in s
    assert "第 i 条 delay 按第 i+1 条即将发送的可见内容估算" in s
    assert "非最后一条通常不要低于 2 秒" in s
    assert "转折、补充、犹豫或长内容加 2-5 秒" in s
    assert "表情包和图片按约 1.5-3 秒" in s
    assert "单条消息只发一条时 delay=0 合法" in s
    assert "最后一条填 0" in s
    assert "多条消息不要贴脸连发" in s
    assert "系统自动估算" not in s
    assert "系统校正" not in s
    assert "自动估算" not in s
    assert "系统会按人类输入速度校正" not in s
    assert '"delay": 0.6' not in s
    assert '"delay": 0.8' not in s
    assert '"delay": 0.5' not in s
    assert '"delay": 1.2' not in s
    assert '"delay": 2.0' not in s
    assert '"delay": 2.8' not in s
    assert '"content": "早啊", "order": 1, "delay": 4.5' in s
    assert '"content": "今天冷死了", "order": 2, "delay": 3.0' in s
    assert '"content": "多穿点", "order": 3, "delay": 0' in s
    assert '"content": "嗯", "order": 1, "delay": 0' in s
    assert '"content": "明天三点", "order": 1, "delay": 4.5' in s
    assert '"content": "不是 四点", "order": 2, "delay": 0' in s
    assert "ignore_review_interrupts 只用于 commit_send_attempt" not in s
    assert "复核后重新调用发送工具改写新内容，必要时传 true 只绕过软复核" not in s
    assert "update_important_memory" in s
    assert "运行时噪声" in s
    assert "tool_search 查询完整参数" in s
    assert "status=need_tool_search" in s
    assert "loop_reminder" in s
    assert "<tool_loop_final_warning>" in s
    assert "send_only" not in s
    assert "no-feedback" not in lower
    assert "no_feedback" not in lower


def test_tool_use_protocol_requires_semantic_memory_scope():
    s = build_tool_use_protocol("file")

    assert "save_important_memory 必须显式填写 scope" in s
    assert "系统不会按当前会话自动推断" in s
    assert "global=跨场景都应参考的事实" in s
    assert "user:QQ号=只适用于该用户本人" in s
    assert "group:群号=只适用于该群" in s
    assert "提到某用户不等于 user scope" in s
    assert "冰狼正在做短中期项目" in s
    assert "不要把 private:QQ 当 scope 返回" in s
    assert "修改内容时重新判断 scope" in s
    assert "仍适用原范围可不传 scope" in s
    assert "scope 通常留空" not in s


def test_tool_use_protocol_documents_tool_result_json_contract():
    s = build_tool_use_protocol("file")

    assert "role=tool.content 的 JSON 字符串" in s
    assert "优先看 ok/status/brief/next" in s
    assert "data/results/content/artifact 是工具数据" in s
    assert "recall_history 的 content" in s
    assert "不得把 brief/next/status/content 原样发给 QQ" in s
    assert "sent[].content 是已经发送过的消息正文" in s


@pytest.mark.asyncio
async def test_summary_agent_rolling_prompt_documents_important_memory_contract():
    from agents.summary_agent import SummaryAgent

    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake-summary")
            self.calls: list[dict] = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools, **kwargs})
            return CompletionResult(
                content=json.dumps(
                    {
                        "summary_text": "合并后的摘要",
                        "new_important": [{"content": "用户长期偏好测试"}],
                    },
                    ensure_ascii=False,
                )
            )

        async def aclose(self) -> None:
            pass

    provider = FakeProvider()
    agent = SummaryAgent(
        provider,
        AgentConfig(provider="fake", model="fake-summary"),
        SummarizeConfig(),
    )

    result = await agent.summarize_rolling(
        [{"role": "user", "content": "我长期喜欢测试", "conversation_id": "private:123"}],
        "已有滚动摘要",
        "用户已经喜欢 Python",
    )

    prompt = provider.calls[0]["messages"][1]["content"]
    assert "用户已经喜欢 Python" in prompt
    assert "现存重要记忆是已经保存的事实，不要重复保存" in prompt
    assert "new_important 只是补充候选，不是完整替换列表" in prompt
    assert "最多返回 3-5 条候选" in prompt
    assert "长期稳定事实" in prompt
    assert "必须客观、完整、有明确主语" in prompt
    assert "已有同主体事实应补充或合并" in prompt
    assert "系统消息、task_context、send_receipt、工具结果、no_action、临时 URL" in prompt
    assert "密钥、token、cookie、rkey、clientkey" in prompt
    assert "scope 可选，只能是 global、user:QQ、group:群号" in prompt
    assert "conversation_id=private:QQ 如果用于 scope，应写成 user:QQ" in prompt
    assert "conversation_id=group:群号 应写成 group:群号" in prompt
    assert "不要把 private:QQ 当 scope 返回" in prompt
    assert "pinned 可选" in prompt
    assert "conversation_id=private:123" in prompt
    assert result == {
        "summary_text": "合并后的摘要",
        "new_important": [{"content": "用户长期偏好测试"}],
    }


def test_group_relevance_uses_clear_addressee_rules():
    p = _persona()
    sys = build_combined_system_prompt(p)
    assert "先分清当前会话是私聊还是群聊" in sys
    assert "私聊里，对方通常是在跟你说" in sys
    assert "最近几条消息实际在对谁说" in sys
    assert "群聊里出现\"你\"、\"你觉得\"、问号" in sys
    assert "不要自动理解成自己" in sys
    assert "最近聊天里的发言对象" in sys
    assert "递话证据" in sys


def test_group_relevance_does_not_treat_unaddressed_you_as_self():
    p = _persona()
    sys = build_combined_system_prompt(p)
    assert "最近小线程是 A 问 B、B 回 A" in sys
    assert "后续无 @ 的\"你觉得/你说/是不是\"默认仍在问 B" in sys
    assert "不是在问你" in sys
    assert "不能只凭字面有\"你\"、问号或\"要求你表态\"来成立" in sys


def test_group_relevance_raises_threshold_after_boundary_message():
    p = _persona()
    sys = build_combined_system_prompt(p)
    assert "没叫你 / 不是问你 / 别插话 / 滚" in sys
    assert "附近未点名消息默认不要接" in sys
    assert "除非后来明确 @你、引用你、叫你名字" in sys


def test_direct_address_should_not_disappear_silently():
    p = _persona()
    sys = build_combined_system_prompt(p)
    assert "被递话后沉默不是收尾" in sys
    assert "想结束也要给一个可见短回应" in sys
    assert "短收尾或贴合的表情包" in sys


def test_group_reply_reference_rules_are_conditional():
    p = _persona()
    sys = build_combined_system_prompt(p)

    assert "回答被引用的消息" in sys
    assert "目标不是紧邻上一条、前后有多人插话" in sys
    assert "复核/被打断后继续提交旧回复" in sys
    assert "如果短回复会产生歧义，要引用、@、点名或自然语言锚定" in sys
    assert '"行/OK/可以/知道了/不要"这类简短确认尤其如此' in sys
    assert "不要机械每条都引用" in sys
    assert "上下文清楚、上一句就是目标消息时自然短回即可" in sys
    assert "不知道消息 ID 或 QQ 号时，用自然语言说明在回哪件事，不要伪造 CQ" in sys


def test_group_claims_and_banter_keep_independent_judgment():
    p = _persona()
    sys = build_combined_system_prompt(p)
    assert "只是\"这个人的说法\"" in sys
    assert "不自动等于事实" in sys
    assert "管理员也可能只是在拱火或逗你" in sys
    assert "字面义、谐音义、玩梗义" in sys
    assert "不必判成\"完全信 A / 完全信 B\"" in sys


def test_kv_prompt_diagnostics_do_not_emit_temp_fields():
    diag = _kv_prompt_diagnostics(
        [{"role": "user", "content": "<task_context>ctx</task_context>"}],
        [{"type": "function", "function": {"name": "no_action"}}],
        loop=1,
        model="deepseek-v4-pro",
    )

    assert "kv_message_count" in diag
    assert "kv_prefix_8k_hash" in diag
    assert diag["kv_user_char_count"] == len("<task_context>ctx</task_context>")
    assert diag["kv_task_context_block_count"] == 1
    assert diag["kv_task_context_char_count"] == len("<task_context>ctx</task_context>")
    assert not any(key.startswith("kv_temp") for key in diag)


def test_kv_prompt_diagnostics_scan_runtime_user_context():
    diag = _kv_prompt_diagnostics(
        [
            {"role": "system", "content": "<core_rules>x</core_rules>"},
            {
                "role": "user",
                "content": (
                    "<task_context><recent_group_messages>x</recent_group_messages>"
                    "<send_receipt>x</send_receipt>"
                    "<retrieved_conversation_context>x</retrieved_conversation_context>"
                    "</task_context>"
                ),
            },
        ],
        [],
        loop=1,
    )

    assert diag["kv_has_send_receipt"] is True
    assert diag["kv_has_recent_group_messages"] is True
    assert diag["kv_has_rag"] is True
    assert diag["kv_send_receipt_block_count"] == 1
    assert diag["kv_rag_block_count"] == 1


def test_kv_prompt_diagnostics_counts_stub_tools():
    diag = _kv_prompt_diagnostics(
        [{"role": "system", "content": "x"}],
        [
            {
                "type": "function",
                "function": {
                    "name": "stub_tool",
                    "parameters": {
                        "type": "object",
                        "properties": {"_tool_search_required": {"type": "boolean"}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "full_tool",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ],
        loop=1,
    )

    assert diag["kv_tools_count"] == 2
    assert diag["kv_tools_stub_count"] == 1
    assert diag["kv_tools_full_count"] == 1
    assert diag["kv_tools_char_count"] > 0


def test_behavior_prompt_does_not_hardcode_persona_replies():
    p = _persona()
    sys = build_combined_system_prompt(p)
    assert "行为规则不提供固定口癖" in sys
    for phrase in ["草", "绷不住", "这什么"]:
        assert phrase not in sys


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


def test_build_combined_system_prompt_propagates_physiology_tools():
    p = _persona()
    sys = build_combined_system_prompt(p, eat_tool=True, sleep_tool=True)
    assert "<physiology>" in sys
    assert "meal_type" in sys
    assert "duration_minutes 填 1-720 分钟" in sys


def test_build_combined_system_prompt_rag_mode_keeps_important_memory():
    p = _persona()
    sys = build_combined_system_prompt(p, important_memory_text="历史片段", memory_mode="rag")
    assert "RAG 会话向量检索" in sys
    assert "<retrieved_conversation_context" in sys
    assert "</retrieved_conversation_context>" not in sys
    assert "历史片段" in sys
    assert "<long_term_memory" in sys
    assert "save_important_memory" in sys
    assert "update_important_memory" in sys
    assert "没有 save_important_memory" not in sys
    assert "不能指望 RAG 一定召回" in sys


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
    brief = PersonaBrief(
        name="Mika",
        gender="female",
        age=18,
        admins=[{"name": "Lily", "qq": "123456", "relation": "创作者"}],
    )
    block = brief.to_brief_block()
    assert "熟悉的人（管理员）" in block
    assert "性别" in block
    assert "年龄" in block
    assert "18 岁" in block
    assert "Lily" in block
    assert "123456" in block
    assert "创作者" in block


def test_persona_brief_skips_missing_age():
    brief = PersonaBrief(name="Mika", gender="female", age=None)
    block = brief.to_brief_block()
    assert "性别" in block
    assert "年龄" not in block


def test_render_persona_file_writes_admins():
    brief = PersonaBrief(name="Mika", gender="female", age=18)
    result = PersonaGenResult(persona_prompt="<identity>Mika</identity>", display_name="Mika")
    text = render_persona_file(
        result,
        brief,
            admins=[{"qq": 123456, "name": "Lily", "role": "owner"}],
    )
    assert "'admins':" in text
    assert "'qq': 123456" in text
    assert "'name': 'Lily'" in text
    assert "'gender': 'female'" in text
    assert "'age': 18" in text


def test_render_persona_file_skips_missing_age():
    brief = PersonaBrief(name="Mika", age=None)
    result = PersonaGenResult(persona_prompt="<identity>Mika</identity>", display_name="Mika")
    text = render_persona_file(result, brief)
    assert "'age'" not in text


def test_persona_generation_prompt_requires_second_person():
    assert "第二人称硬要求" in PERSONA_GEN_SYSTEM_PROMPT
    assert "写\"你是……\"、\"你习惯……\"、\"你不会……\"" in PERSONA_GEN_SYSTEM_PROMPT
    assert "不要写\"她/他/ta 是……\"" in PERSONA_GEN_SYSTEM_PROMPT
    assert "<identity>\n你是[角色名]" in PERSONA_GEN_SYSTEM_PROMPT
    assert "你不会做的事：" in PERSONA_GEN_SYSTEM_PROMPT
    assert "仍必须使用第二人称描述角色本人" in PERSONA_REFINE_SYSTEM_PROMPT
    assert "一个或多个顶层 XML 标签片段" in PERSONA_REFINE_SYSTEM_PROMPT


def test_persona_brief_formats_special_relation_detail():
    brief = PersonaBrief(name="Mika", relation="special:我是她的导师")
    block = brief.to_brief_block()
    assert "用户和角色的关系" in block
    assert "我是她的导师" in block

    detail_brief = PersonaBrief(name="Mika", relation="special", relation_detail="长期网友")
    assert "长期网友" in detail_brief.to_brief_block()


_PERSONA_XML = """<identity>旧 identity</identity>
<past>旧 past</past>
<personality>旧 personality</personality>
<voice>旧 voice</voice>
<boundaries>旧 boundaries</boundaries>
<relation_with_user>旧 relation</relation_with_user>
<consistency_anchors>旧 anchors</consistency_anchors>"""


class _PersonaFakeProvider(IProvider):
    def __init__(self, content: str) -> None:
        super().__init__("fake-persona")
        self.content = content
        self.calls: list[dict] = []

    async def chat_completion(self, messages, *, tools=None, **kwargs):
        self.calls.append({"messages": list(messages), "tools": tools, **kwargs})
        return CompletionResult(content=self.content, usage=Usage(prompt_tokens=1))

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_persona_refine_messages_keep_history_before_current_user():
    provider = _PersonaFakeProvider(_PERSONA_XML)
    agent = PersonaGenAgent(provider, AgentConfig(provider="fake", model="fake"))
    history = [
        {"role": "user", "content": "上一轮用户消息"},
        {"role": "assistant", "content": "<voice>上一轮原始回复</voice>"},
    ]

    result = await agent.refine(
        _PERSONA_XML,
        "这轮把语气放轻",
        refined_count=2,
        edit_history=history,
        current_brief=PersonaBrief(name="Mika"),
    )

    messages = provider.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[1:3] == history
    assert "这轮把语气放轻" in messages[-1]["content"]
    assert messages[-1]["content"].rstrip().endswith(_PERSONA_XML)
    assert result.refined_count == 3


def test_persona_edit_history_append_never_trims_long_history():
    from ui.wizard.context import append_persona_edit_history

    history: list[dict[str, str]] = []
    for idx in range(12):
        history = append_persona_edit_history(
            history,
            f"第 {idx} 轮用户消息",
            f"<voice>第 {idx} 轮回复</voice>",
        )

    updated = append_persona_edit_history(history, "本轮用户消息", "<voice>本轮回复</voice>")

    assert len(updated) == 26
    assert updated[0] == {"role": "user", "content": "第 0 轮用户消息"}
    assert updated[1] == {"role": "assistant", "content": "<voice>第 0 轮回复</voice>"}
    assert updated[-2:] == [
        {"role": "user", "content": "本轮用户消息"},
        {"role": "assistant", "content": "<voice>本轮回复</voice>"},
    ]


@pytest.mark.asyncio
async def test_persona_refine_keeps_all_long_history_messages():
    provider = _PersonaFakeProvider(_PERSONA_XML)
    agent = PersonaGenAgent(provider, AgentConfig(provider="fake", model="fake"))
    history = [
        entry
        for idx in range(12)
        for entry in (
            {"role": "user", "content": f"第 {idx} 轮用户消息"},
            {"role": "assistant", "content": f"<voice>第 {idx} 轮回复</voice>"},
        )
    ]

    await agent.refine(
        _PERSONA_XML,
        "第 13 轮继续调整",
        refined_count=12,
        edit_history=history,
        current_brief=PersonaBrief(name="Mika"),
    )

    messages = provider.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["system", *[m["role"] for m in history], "user"]
    assert messages[1:-1] == history
    assert "第 13 轮继续调整" in messages[-1]["content"]
    assert messages[-1]["content"].rstrip().endswith(_PERSONA_XML)


@pytest.mark.asyncio
async def test_persona_refine_merges_top_level_tag_fragment():
    provider = _PersonaFakeProvider("<voice>新 voice</voice>")
    agent = PersonaGenAgent(provider, AgentConfig(provider="fake", model="fake"))

    result = await agent.refine(_PERSONA_XML, "只改说话方式")

    assert result.raw_response == "<voice>新 voice</voice>"
    assert "<voice>新 voice</voice>" in result.persona_prompt
    assert "<identity>旧 identity</identity>" in result.persona_prompt
    assert "<boundaries>旧 boundaries</boundaries>" in result.persona_prompt
    assert "<voice>旧 voice</voice>" not in result.persona_prompt


@pytest.mark.asyncio
async def test_persona_refine_full_xml_replaces_directly():
    new_xml = """<identity>新 identity</identity>
<past>新 past</past>
<personality>新 personality</personality>
<voice>新 voice</voice>
<boundaries>新 boundaries</boundaries>
<relation_with_user>新 relation</relation_with_user>
<consistency_anchors>新 anchors</consistency_anchors>"""
    provider = _PersonaFakeProvider(new_xml)
    agent = PersonaGenAgent(provider, AgentConfig(provider="fake", model="fake"))

    result = await agent.refine(_PERSONA_XML, "整份重写", refined_count=4)

    assert result.persona_prompt == new_xml
    assert result.raw_response == new_xml
    assert result.refined_count == 5


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


def test_build_task_context_with_persona_context():
    s = build_task_context(
        "现在是 2026 年 5 月",
        persona_context="<persona_context>精力变化摘要</persona_context>",
    )
    assert "现在是 2026 年 5 月" in s
    assert "<persona_context>精力变化摘要</persona_context>" in s
    assert "不是用户新发言" in s


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

    # 顺序：system(combined) → system(admin) → user → assistant → user(task_context)
    assert msgs[0]["role"] == "system"
    assert "<persona" in msgs[0]["content"]
    assert msgs[1]["role"] == "system"
    assert "<admin_info" in msgs[1]["content"]
    assert msgs[2]["role"] == "user"
    assert msgs[3]["role"] == "assistant"
    assert msgs[4]["role"] == "user"
    assert "<task_context" in msgs[4]["content"]
    assert "不是用户新发言" in msgs[4]["content"]
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
    assert "RAG 会话向量检索" in msgs_rag[0]["content"]
    assert "必须主动保存" not in msgs_rag[0]["content"]
    assert "必须主动保存" in msgs_file[0]["content"]


def test_build_messages_propagates_persona_context_and_physiology_tools():
    p = _persona()
    msgs = build_messages(
        p,
        [],
        current_context="现在是 10:00",
        persona_context="<persona_context>动态人格状态</persona_context>",
        eat_tool=True,
        sleep_tool=True,
    )

    assert "<physiology>" in msgs[0]["content"]
    assert "meal_type" in msgs[0]["content"]
    assert "duration_minutes 填 1-720 分钟" in msgs[0]["content"]
    assert msgs[-1]["role"] == "user"
    assert "现在是 10:00" in msgs[-1]["content"]
    assert "<persona_context>动态人格状态</persona_context>" in msgs[-1]["content"]


def test_build_messages_rag_memory_is_tail_context_for_cache_stability():
    p = _persona()
    history = [{"role": "user", "content": "旧消息"}]
    first = build_messages(
        p,
        history,
        important_memory_text="重要记忆",
        rag_context_text="RAG 片段 A",
        current_context="现在是 10:00",
        memory_mode="rag",
    )
    second = build_messages(
        p,
        history,
        important_memory_text="重要记忆",
        rag_context_text="RAG 片段 B",
        current_context="现在是 10:00",
        memory_mode="rag",
    )

    assert first[0]["content"] == second[0]["content"]
    assert "RAG 片段 A" not in first[0]["content"]
    assert "RAG 片段 B" not in second[0]["content"]
    assert "重要记忆" in first[0]["content"]
    assert "<long_term_memory" in first[0]["content"]
    assert [m["role"] for m in first] == ["system", "user", "user", "user"]
    assert "旧消息" in first[1]["content"]
    assert "task_context" in first[2]["content"]
    assert "不是用户新发言" in first[2]["content"]
    assert "RAG 片段 A" in first[3]["content"]
    assert "不是用户新发言" in first[3]["content"]


def test_build_messages_can_reuse_persisted_task_context_record_for_prefix_stability():
    p = _persona()
    history = [{"role": "user", "content": "本轮用户消息"}]
    task_record = {
        "role": "user",
        "content": "<task_context priority=\"medium\">\n现在是 10:00。\n</task_context>",
        "metadata": {"kind": "task_context_snapshot"},
        "conversation_id": "private:1",
    }

    current = build_messages(
        p,
        history,
        current_context_record=task_record,
        memory_mode="rag",
    )
    next_turn = build_messages(
        p,
        [*history, task_record, {"role": "assistant", "content": ""}],
        current_context_record={
            "role": "user",
            "content": "<task_context priority=\"medium\">\n现在是 10:01。\n</task_context>",
        },
        memory_mode="rag",
    )

    assert normalize_messages(current[:3]) == normalize_messages(next_turn[:3])
    assert current[2]["content"] == task_record["content"]


def test_build_messages_does_not_duplicate_persona_context_with_task_record():
    p = _persona()
    task_record = {
        "role": "user",
        "content": "<task_context priority=\"medium\">\n已持久化上下文\n</task_context>",
        "metadata": {"kind": "task_context_snapshot"},
        "conversation_id": "private:1",
    }

    msgs = build_messages(
        p,
        [],
        current_context_record=task_record,
        persona_context="<persona_context>不应重复追加</persona_context>",
    )

    assert msgs[-1]["content"] == task_record["content"]
    assert "不应重复追加" not in "\n".join(str(msg["content"]) for msg in msgs)


def test_turn_summary_labels_assistant_as_current_persona_reply():
    from core.pipeline_task_context import _compact_chat_summary

    summary = _compact_chat_summary(
        user_or_event_text="[系统事件] 主动思考触发",
        task_context="人格待办：提醒主人喝水",
        records=[{"role": "assistant", "content": "该去喝水了"}],
    )

    assert "外部输入/系统事件：[系统事件] 主动思考触发" in summary
    assert "系统上下文：人格待办：提醒主人喝水" in summary
    assert "当前人格自己的回复：该去喝水了" in summary
    assert "用户/事件：" not in summary
    assert "助手：" not in summary


def test_build_messages_preserves_complete_tool_call_group():
    p = _persona()
    msgs = build_messages(
        p,
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc-ok",
                        "type": "function",
                        "function": {"name": "no_action", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tc-ok", "content": '{"no_action": true}'},
        ],
    )

    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["tool_calls"][0]["id"] == "tc-ok"
    assert msgs[2]["role"] == "tool"
    assert msgs[2]["tool_call_id"] == "tc-ok"


def test_build_messages_converts_orphan_tool_result_without_dropping_content():
    p = _persona()
    msgs = build_messages(
        p,
        [
            {"role": "tool", "tool_call_id": "tc-lost", "content": '{"ok": true}'},
            {"role": "user", "content": "后续消息"},
        ],
    )

    assert msgs[1]["role"] == "system"
    assert "historical_tool_record_unreplayable" in msgs[1]["content"]
    assert "tc-lost" in msgs[1]["content"]
    assert "ok" in msgs[1]["content"]
    assert "true" in msgs[1]["content"]
    assert msgs[2]["role"] == "user"
    assert msgs[2]["content"] == "后续消息"


def test_build_messages_user_event_is_final_user_message():
    p = _persona()
    msgs = build_messages(
        p,
        [{"role": "assistant", "content": "旧回复"}],
        current_context="现在是 10:00",
        user_event="[系统事件 · 非用户消息] 定时唤醒已到。",
    )

    assert msgs[-1]["role"] == "user"
    assert "系统事件" in msgs[-1]["content"]
    assert msgs[-2]["role"] == "user"
    assert "task_context" in msgs[-2]["content"]


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


def test_important_memory_keyword_force_save_api_removed():
    import inspect

    import memory.important as important_module
    from memory import ImportantMemoryManager

    assert not hasattr(ImportantMemoryManager, "force_save_from_keyword")
    assert not hasattr(important_module, "DEFAULT_FORCE_SAVE_KEYWORDS")
    assert not hasattr(important_module, "_strip_memory_keyword")
    assert "matched_keyword" not in inspect.getsource(important_module)


@pytest.mark.asyncio
async def test_important_save_keeps_keyword_text_as_explicit_tool_content(tmp_path):
    from memory import ImportantMemoryManager

    im = ImportantMemoryManager(tmp_path / "imp.json")
    await im.load()

    result = await im.save("记住第一条")

    assert result["saved"] is True
    assert im.items()[0]["content"] == "记住第一条"
    assert im.items()[0].get("source") is None


# ============================================================
# Schema 新字段
# ============================================================


def test_schema_long_term_memory_config_defaults():
    from app_config import LongTermMemoryConfig

    c = LongTermMemoryConfig()
    assert c.mode == "file"
    assert not hasattr(c, "keyword_trigger_save")
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


def _basic_tool_schema(name: str = "needs_feedback") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _work_call(idx: int, *, name: str = "needs_feedback", args: str = "{}") -> ToolCall:
    return ToolCall(id=f"tc-{idx}", name=name, arguments=args)


def _tool_payloads(result: AgentRunResult) -> list[dict]:
    return [
        json.loads(record["content"])
        for record in result.records
        if record.get("role") == "tool"
    ]


@pytest.mark.asyncio
async def test_runner_tool_loop_reminder_attaches_to_last_tool_result():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools, **kwargs})
            if len(self.calls) <= 2:
                return CompletionResult(
                    tool_calls=[_work_call(len(self.calls))],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=5),
                )
            return CompletionResult(
                tool_calls=[ToolCall(id="tc-na", name="no_action", arguments="{}")],
                finish_reason="tool_calls",
                usage=Usage(prompt_tokens=5),
            )

        async def aclose(self) -> None:
            pass

    async def executor(name, _args):
        if name == "no_action":
            return {"ok": True, "no_action": True}
        return {"ok": True, "status": "partial"}

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(
            provider="fake",
            model="fake",
            tool_loop_reminder_interval=2,
            tool_loop_final_warning_count=99,
        ),
    )

    result = await runner.run(
        [{"role": "user", "content": "处理资料"}],
        tools=[_basic_tool_schema(), _basic_tool_schema("no_action")],
        tool_executor=executor,
    )

    payloads = _tool_payloads(result)
    assert "loop_reminder" not in payloads[0]
    reminder = payloads[1]["loop_reminder"]
    assert reminder["level"] == "reminder"
    assert reminder["tool_loop_reminder_interval"] == 2
    assert reminder["reminder_count"] == 1
    assert result.finish_reason == "no_action"


@pytest.mark.asyncio
async def test_runner_tool_loop_reminder_resets_and_can_repeat():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools, **kwargs})
            if len(self.calls) <= 4:
                return CompletionResult(
                    tool_calls=[_work_call(len(self.calls))],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=5),
                )
            return CompletionResult(
                tool_calls=[ToolCall(id="tc-na", name="no_action", arguments="{}")],
                finish_reason="tool_calls",
                usage=Usage(prompt_tokens=5),
            )

        async def aclose(self) -> None:
            pass

    async def executor(name, _args):
        if name == "no_action":
            return {"ok": True, "no_action": True}
        return {"ok": True, "status": "partial"}

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(
            provider="fake",
            model="fake",
            tool_loop_reminder_interval=2,
            tool_loop_final_warning_count=99,
        ),
    )

    result = await runner.run(
        [{"role": "user", "content": "处理资料"}],
        tools=[_basic_tool_schema(), _basic_tool_schema("no_action")],
        tool_executor=executor,
    )

    reminders = [
        payload["loop_reminder"]["reminder_count"]
        for payload in _tool_payloads(result)
        if "loop_reminder" in payload
    ]
    assert reminders == [1, 2]
    assert result.finish_reason == "no_action"


@pytest.mark.asyncio
async def test_runner_tool_loop_final_warning_and_grace_then_finalizes():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools, **kwargs})
            call_no = len(self.calls)
            if call_no == 5:
                assert tools is not None
                assert messages[-1]["role"] == "user"
                assert "<tool_loop_final_warning" in messages[-1]["content"]
                assert "你还有 2 轮工具调用机会" in messages[-1]["content"]
            if call_no <= 6:
                return CompletionResult(
                    tool_calls=[_work_call(call_no)],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=5),
                )
            assert tools is None
            assert messages[-1]["role"] == "user"
            assert "<tool_loop_stop" in messages[-1]["content"]
            return CompletionResult(
                content="工具循环已收尾。",
                finish_reason="stop",
                usage=Usage(prompt_tokens=7),
            )

        async def aclose(self) -> None:
            pass

    async def executor(_name, _args):
        return {"ok": True, "status": "partial"}

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(
            provider="fake",
            model="fake",
            tool_loop_reminder_interval=3,
            tool_loop_final_warning_count=1,
            tool_loop_final_grace_loops=2,
            tool_loop_final_max_tokens=1536,
        ),
    )

    result = await runner.run(
        [{"role": "user", "content": "持续处理"}],
        tools=[_basic_tool_schema()],
        tool_executor=executor,
    )

    assert result.finish_reason == "tool_loop_finalized"
    assert result.final_content == "工具循环已收尾。"
    assert provider.calls[4]["tools"] is not None
    assert provider.calls[5]["tools"] is not None
    assert provider.calls[6]["tools"] is None
    assert provider.calls[6]["max_tokens"] == 1536


@pytest.mark.asyncio
async def test_runner_tool_loop_zero_grace_finalizes_at_next_warning_threshold():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            call_no = len(self.calls)
            if call_no <= 4:
                assert tools is not None
                return CompletionResult(
                    tool_calls=[_work_call(call_no)],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=5),
                )
            assert tools is None
            assert messages[-1]["role"] == "user"
            assert "<tool_loop_stop" in messages[-1]["content"]
            assert messages[-2]["role"] == "user"
            assert "<tool_loop_final_warning" in messages[-2]["content"]
            assert "你还有 0 轮工具调用机会" in messages[-2]["content"]
            return CompletionResult(
                content="零宽限收尾。",
                finish_reason="stop",
                usage=Usage(prompt_tokens=7),
            )

        async def aclose(self) -> None:
            pass

    async def executor(_name, _args):
        return {"ok": True, "status": "partial"}

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(
            provider="fake",
            model="fake",
            tool_loop_reminder_interval=2,
            tool_loop_final_warning_count=1,
            tool_loop_final_grace_loops=0,
        ),
    )

    result = await runner.run(
        [{"role": "user", "content": "持续处理"}],
        tools=[_basic_tool_schema()],
        tool_executor=executor,
    )

    assert result.finish_reason == "tool_loop_finalized"
    assert result.final_content == "零宽限收尾。"
    assert len(provider.calls) == 5
    assert provider.calls[4]["tools"] is None


@pytest.mark.asyncio
async def test_runner_no_action_finishes_during_tool_loop_grace():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            if len(self.calls) <= 3:
                return CompletionResult(
                    tool_calls=[_work_call(len(self.calls))],
                    finish_reason="tool_calls",
                )
            assert "<tool_loop_final_warning" in messages[-1]["content"]
            return CompletionResult(
                tool_calls=[ToolCall(id="tc-na", name="no_action", arguments="{}")],
                finish_reason="tool_calls",
            )

        async def aclose(self) -> None:
            pass

    async def executor(name, _args):
        if name == "no_action":
            return {"ok": True, "no_action": True}
        return {"ok": True, "status": "partial"}

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(
            provider="fake",
            model="fake",
            tool_loop_reminder_interval=2,
            tool_loop_final_warning_count=1,
            tool_loop_final_grace_loops=1,
        ),
    )

    result = await runner.run(
        [{"role": "user", "content": "持续处理"}],
        tools=[_basic_tool_schema(), _basic_tool_schema("no_action")],
        tool_executor=executor,
    )

    assert result.finish_reason == "no_action"
    assert all(call["tools"] is not None for call in provider.calls)


@pytest.mark.asyncio
async def test_runner_finish_after_success_finishes_during_tool_loop_grace():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            if len(self.calls) <= 3:
                return CompletionResult(
                    tool_calls=[_work_call(len(self.calls))],
                    finish_reason="tool_calls",
                )
            assert "<tool_loop_final_warning" in messages[-1]["content"]
            return CompletionResult(
                tool_calls=[
                    _work_call(
                        99,
                        name="save_important_memory",
                        args='{"memory_text":"完成","finish_after_success":true}',
                    )
                ],
                finish_reason="tool_calls",
            )

        async def aclose(self) -> None:
            pass

    async def executor(name, _args):
        if name == "save_important_memory":
            return {"ok": True, "status": "done"}
        return {"ok": True, "status": "partial"}

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(
            provider="fake",
            model="fake",
            tool_loop_reminder_interval=2,
            tool_loop_final_warning_count=1,
            tool_loop_final_grace_loops=1,
        ),
    )

    result = await runner.run(
        [{"role": "user", "content": "持续处理"}],
        tools=[_basic_tool_schema(), _basic_tool_schema("save_important_memory")],
        tool_executor=executor,
    )

    assert result.finish_reason == "finish_after_success"
    assert all(call["tools"] is not None for call in provider.calls)


@pytest.mark.asyncio
async def test_runner_legacy_max_loops_no_longer_hard_limits_tool_loop():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            if len(self.calls) <= 2:
                return CompletionResult(
                    tool_calls=[_work_call(len(self.calls))],
                    finish_reason="tool_calls",
                )
            return CompletionResult(
                tool_calls=[ToolCall(id="tc-na", name="no_action", arguments="{}")],
                finish_reason="tool_calls",
            )

        async def aclose(self) -> None:
            pass

    async def executor(name, _args):
        if name == "no_action":
            return {"ok": True, "no_action": True}
        return {"ok": True, "status": "partial"}

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(
            provider="fake",
            model="fake",
            max_loops=1,
            tool_loop_reminder_interval=20,
            tool_loop_final_warning_count=99,
        ),
    )

    result = await runner.run(
        [{"role": "user", "content": "处理资料"}],
        tools=[_basic_tool_schema(), _basic_tool_schema("no_action")],
        tool_executor=executor,
    )

    assert result.finish_reason == "no_action"
    assert len(provider.calls) == 3


@pytest.mark.asyncio
async def test_runner_async_agent_task_tools_do_not_finish_as_no_feedback():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            if len(self.calls) == 1:
                return CompletionResult(
                    tool_calls=[
                        ToolCall(
                            id="tc-agent",
                            name="start_agent_task",
                            arguments='{"prompt":"整理资料"}',
                        )
                    ],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=7),
                )
            assert messages[-1]["role"] == "tool"
            assert "agent-test" in messages[-1]["content"]
            return CompletionResult(
                tool_calls=[
                    ToolCall(id="tc-na", name="no_action", arguments="{}")
                ],
                finish_reason="tool_calls",
                usage=Usage(prompt_tokens=8),
            )

        async def aclose(self) -> None:
            pass

    async def executor(name, _args):
        if name == "start_agent_task":
            return {
                "ok": True,
                "status": "completed",
                "task_id": "agent-test",
                "result_file": "agent_tasks/agent-test/result.md",
                "content": "任务结果",
            }
        if name == "no_action":
            return {"ok": True, "no_action": True}
        raise AssertionError(name)

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(provider="fake", model="fake", max_loops=3),
    )

    result = await runner.run(
        [{"role": "user", "content": "启动后台任务"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "start_agent_task",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "no_action",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ],
        tool_executor=executor,
    )

    assert result.finish_reason == "no_action"
    assert len(provider.calls) == 2
    assert result.prompt_tokens == 15


@pytest.mark.asyncio
async def test_runner_all_finish_after_success_tools_finish():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            return CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-a",
                        name="save_important_memory",
                        arguments=(
                            '{"memory_text":"用户喜欢咖啡",'
                            '"finish_after_success":true}'
                        ),
                    ),
                    ToolCall(
                        id="tc-b",
                        name="schedule_wakeup",
                        arguments=(
                            '{"delay_seconds":60,"mode":"wakeup",'
                            '"reminder":"提醒用户",'
                            '"finish_after_success":true}'
                        ),
                    ),
                ],
                finish_reason="tool_calls",
                usage=Usage(prompt_tokens=5),
            )

        async def aclose(self) -> None:
            pass

    async def executor(name, _args):
        return {"ok": True, "status": "done", "tool": name}

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(provider="fake", model="fake", max_loops=3),
    )

    result = await runner.run(
        [{"role": "user", "content": "保存并提醒"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "save_important_memory",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "schedule_wakeup",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ],
        tool_executor=executor,
    )

    assert result.finish_reason == "finish_after_success"
    assert len(provider.calls) == 1
    tool_records = [r for r in result.records if r.get("role") == "tool"]
    assert len(tool_records) == 2
    for record in tool_records:
        assert '"turn_completion"' in record["content"]
        assert '"allowed": true' in record["content"]


@pytest.mark.parametrize(
    "blocked_result",
    [
        {"ok": False, "status": "failed"},
        {"ok": True, "status": "partial"},
        {"ok": True, "status": "needs_review"},
        {"ok": True, "status": "need_tool_search"},
        {"ok": True, "errors": ["工具返回错误"]},
    ],
)
@pytest.mark.asyncio
async def test_runner_blocked_finish_after_success_tool_continues(blocked_result):
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            if len(self.calls) == 1:
                return CompletionResult(
                    tool_calls=[
                        ToolCall(
                            id="tc-tool",
                            name="save_important_memory",
                            arguments=(
                                '{"memory_text":"用户喜欢茶",'
                                '"finish_after_success":true}'
                            ),
                        )
                    ],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=5),
                )
            assert messages[-1]["role"] == "tool"
            return CompletionResult(
                tool_calls=[
                    ToolCall(id="tc-na", name="no_action", arguments="{}")
                ],
                finish_reason="tool_calls",
                usage=Usage(prompt_tokens=6),
            )

        async def aclose(self) -> None:
            pass

    async def executor(name, _args):
        if name == "save_important_memory":
            return dict(blocked_result)
        if name == "no_action":
            return {"ok": True, "status": "done", "no_action": True}
        raise AssertionError(name)

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(provider="fake", model="fake", max_loops=3),
    )

    result = await runner.run(
        [{"role": "user", "content": "保存"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "save_important_memory",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "no_action",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ],
        tool_executor=executor,
    )

    assert result.finish_reason == "no_action"
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_runner_requires_all_non_no_action_tools_to_allow_completion():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            if len(self.calls) == 1:
                return CompletionResult(
                    tool_calls=[
                        ToolCall(
                            id="tc-a",
                            name="save_important_memory",
                            arguments=(
                                '{"memory_text":"用户喜欢咖啡",'
                                '"finish_after_success":true}'
                            ),
                        ),
                        ToolCall(
                            id="tc-b",
                            name="schedule_wakeup",
                            arguments=(
                                '{"delay_seconds":60,"mode":"wakeup",'
                                '"reminder":"提醒用户"}'
                            ),
                        ),
                    ],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=5),
                )
            return CompletionResult(
                tool_calls=[
                    ToolCall(id="tc-na", name="no_action", arguments="{}")
                ],
                finish_reason="tool_calls",
                usage=Usage(prompt_tokens=6),
            )

        async def aclose(self) -> None:
            pass

    async def executor(name, _args):
        if name == "no_action":
            return {"ok": True, "status": "done", "no_action": True}
        return {"ok": True, "status": "done", "tool": name}

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(provider="fake", model="fake", max_loops=3),
    )

    result = await runner.run(
        [{"role": "user", "content": "保存并提醒"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "save_important_memory",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "schedule_wakeup",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "no_action",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ],
        tool_executor=executor,
    )

    assert result.finish_reason == "no_action"
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_runner_failed_no_action_does_not_finish_tool_loop():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            if len(self.calls) == 1:
                return CompletionResult(
                    tool_calls=[ToolCall(id="tc-na-fail", name="no_action", arguments="{}")],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=5),
                )
            if len(self.calls) == 2:
                assert "policy_rejected" in messages[-1]["content"]
                return CompletionResult(
                    tool_calls=[
                        ToolCall(
                            id="tc-send",
                            name="send_private_messages",
                            arguments=(
                                '{"targets": [{"target_qq": 123, "content": "已处理",'
                                ' "order": 1, "delay": 0}]}'
                            ),
                        )
                    ],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=6),
                )
            return CompletionResult(
                tool_calls=[ToolCall(id="tc-na-ok", name="no_action", arguments="{}")],
                finish_reason="tool_calls",
                usage=Usage(prompt_tokens=6),
            )

        async def aclose(self) -> None:
            pass

    no_action_calls = 0

    async def executor(name, _args):
        nonlocal no_action_calls
        if name == "no_action":
            no_action_calls += 1
            if no_action_calls > 1:
                return {"ok": True, "status": "done"}
            return {"ok": False, "status": "policy_rejected"}
        if name == "send_private_messages":
            return {"ok": True, "status": "sent"}
        raise AssertionError(name)

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(provider="fake", model="fake", max_loops=3),
    )

    result = await runner.run(
        [{"role": "user", "content": "交付结果"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "no_action",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "send_private_messages",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ],
        tool_executor=executor,
    )

    assert result.finish_reason == "no_action"
    assert len(provider.calls) == 3


@pytest.mark.parametrize("pending_status", ["failed", "partial", "needs_review"])
@pytest.mark.asyncio
async def test_runner_pending_no_action_status_does_not_finish_tool_loop(pending_status):
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            if len(self.calls) == 1:
                return CompletionResult(
                    tool_calls=[
                        ToolCall(id="tc-na-pending", name="no_action", arguments="{}")
                    ],
                    finish_reason="tool_calls",
                    usage=Usage(prompt_tokens=5),
                )
            assert messages[-1]["role"] == "tool"
            assert pending_status in messages[-1]["content"]
            return CompletionResult(
                tool_calls=[
                    ToolCall(id="tc-na-ok", name="no_action", arguments="{}")
                ],
                finish_reason="tool_calls",
                usage=Usage(prompt_tokens=6),
            )

        async def aclose(self) -> None:
            pass

    no_action_calls = 0

    async def executor(name, _args):
        nonlocal no_action_calls
        assert name == "no_action"
        no_action_calls += 1
        if no_action_calls == 1:
            return {"status": pending_status}
        return {"ok": True, "status": "done", "no_action": True}

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(provider="fake", model="fake", max_loops=3),
    )

    result = await runner.run(
        [{"role": "user", "content": "不操作"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "no_action",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        tool_executor=executor,
    )

    assert result.finish_reason == "no_action"
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_runner_stop_after_tool_finishes_immediately():
    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            return CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-write",
                        name="write_file",
                        arguments='{"path": "result.md", "content": "done"}',
                    )
                ],
                finish_reason="tool_calls",
                usage=Usage(prompt_tokens=5),
            )

        async def aclose(self) -> None:
            pass

    async def executor(name, _args):
        assert name == "write_file"
        return {"ok": True, "path": "result.md", "stop_after_tool": True}

    runner = AgentRunner(
        FakeProvider(),
        AgentConfig(provider="fake", model="fake", max_loops=3),
    )

    result = await runner.run(
        [{"role": "user", "content": "写结果"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        tool_executor=executor,
    )

    assert result.finish_reason == "tool_stop"
    assert result.loop_count == 1


@pytest.mark.asyncio
async def test_runner_drops_plain_text_draft_before_retry():
    leaked_draft = "思考过程\nRAG里提到撤回消息，所以我应该这样回复"

    class FakeProvider(IProvider):
        def __init__(self) -> None:
            super().__init__("fake")
            self.calls = []

        async def chat_completion(self, messages, *, tools=None, **kwargs):
            self.calls.append({"messages": list(messages), "tools": tools})
            if len(self.calls) == 1:
                return CompletionResult(
                    content=leaked_draft,
                    finish_reason="stop",
                    usage=Usage(prompt_tokens=5),
                )
            joined = "\n".join(str(m.get("content") or "") for m in messages)
            assert leaked_draft not in joined
            assert "上一轮纯文本已被系统丢弃" in joined
            return CompletionResult(
                tool_calls=[
                    ToolCall(id="tc-na", name="no_action", arguments="{}")
                ],
                finish_reason="tool_calls",
                usage=Usage(prompt_tokens=6),
            )

        async def aclose(self) -> None:
            pass

    async def executor(name, _args):
        assert name == "no_action"
        return {"ok": True, "no_action": True}

    provider = FakeProvider()
    runner = AgentRunner(
        provider,
        AgentConfig(provider="fake", model="fake", max_loops=3),
    )

    result = await runner.run(
        [{"role": "user", "content": "不要泄漏内部分析"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "no_action",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        tool_executor=executor,
    )

    assert result.finish_reason == "no_action"
    assert len(provider.calls) == 2
    assert not any(
        r.get("role") == "assistant" and r.get("content") == leaked_draft
        for r in result.records
    )


def test_proactive_router_treats_only_clean_take_actions_as_action():
    assert _is_action_decision("TAKE_ACTIONS") is True
    assert _is_action_decision("TAKE_ACTIONS: 用户要求下次主动思考时提醒") is True
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

    assert await agent.should_act([]) == (False, "")


@pytest.mark.asyncio
async def test_proactive_router_returns_reason():
    class FakeProvider:
        async def chat_completion(self, *_args, **_kwargs):
            return CompletionResult(content="TAKE_ACTIONS: 用户要求空闲后提醒他")

    agent = ProactiveRouterAgent(
        FakeProvider(),
        AgentConfig(provider="fake", model="router", max_tokens=64),
    )

    assert await agent.should_act([]) == (True, "用户要求空闲后提醒他")
