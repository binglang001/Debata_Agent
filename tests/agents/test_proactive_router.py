"""测试主动路由决策。"""

from __future__ import annotations

import pytest

from agents.proactive_agent import ProactiveRouterAgent, _is_action_decision
from app_config.schema import AgentConfig
from providers.base import CompletionResult


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
