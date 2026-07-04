"""测试 agents 层：行为提示词与人格生成提示词。"""

from __future__ import annotations

import json

import pytest

from agents.behavior_prompt import build_tool_use_protocol
from agents.context_builder import build_admin_info, build_combined_system_prompt
from agents.persona_gen_agent import (
    PERSONA_GEN_SYSTEM_PROMPT,
    PERSONA_REFINE_SYSTEM_PROMPT,
    PersonaBrief,
    PersonaGenAgent,
    PersonaGenResult,
    render_persona_file,
)
from agents.persona_loader import Persona
from agents.runner import _kv_prompt_diagnostics
from app_config.schema import AgentConfig, SummarizeConfig
from providers.base import CompletionResult, IProvider, Usage

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
    assert '"content": "不是四点", "order": 2, "delay": 0' in s
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
