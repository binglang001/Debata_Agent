"""测试 token 预算估算口径。"""

from __future__ import annotations

from utils.token_budget import TokenEstimator


def test_estimate_messages_ignores_provider_stripped_metadata():
    estimator = TokenEstimator()
    base = [{"role": "user", "content": "hello"}]
    noisy = [
        {
            "role": "user",
            "content": "hello",
            "metadata": {"raw": "x" * 20000},
        }
    ]

    assert estimator.estimate_messages(noisy) == estimator.estimate_messages(base)


def test_estimate_messages_ignores_conversation_id():
    estimator = TokenEstimator()
    base = [{"role": "user", "content": "hello"}]
    noisy = [
        {
            "role": "user",
            "content": "hello",
            "conversation_id": "conv-" + ("x" * 20000),
        }
    ]

    assert estimator.estimate_messages(noisy) == estimator.estimate_messages(base)


def test_estimate_messages_does_not_count_reasoning_content_by_default():
    estimator = TokenEstimator()
    base = [{"role": "assistant", "content": "done"}]
    noisy = [
        {
            "role": "assistant",
            "content": "done",
            "reasoning_content": "hidden " * 4000,
        }
    ]

    assert estimator.estimate_messages(noisy) == estimator.estimate_messages(base)


def test_estimate_messages_still_counts_tool_calls():
    estimator = TokenEstimator()
    without_tool = [{"role": "assistant", "content": ""}]
    with_tool = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {
                        "name": "search",
                        "arguments": '{"query":"' + ("alpha " * 400) + '"}',
                    },
                }
            ],
        }
    ]

    assert estimator.estimate_messages(with_tool) > estimator.estimate_messages(without_tool) + 100
