"""端到端 KV 缓存命中率实测。

需要真实 API 密钥，被 @pytest.mark.live 标记，默认不跑。
跑法：
    DEEPSEEK_API_KEY=xxx venv/Scripts/python -m pytest tests/test_kv_cache_real.py -m live -s

验证目标（Phase 1.10 验证清单）：
    - 连续 10 轮对话整体命中率 > 90%（DeepSeek / GLM 等支持磁盘缓存的 provider）
    - Task Contract 重注入不破坏前缀缓存

⚠️ 这是 GPT 接手时的实测脚本骨架。Claude 写好用例与判定，
GPT 跑时填具体 fixture（构造 Persona/Provider/Agent）。
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live


# ============================================================
# Live 标记的 skip 条件
# ============================================================


def _skip_if_no_key():
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("需要 DEEPSEEK_API_KEY 才能跑 live 测试")


# ============================================================
# 用例 1：连续多轮对话整体命中率
# ============================================================


@pytest.mark.asyncio
async def test_cache_hit_rate_multi_turn():
    """跑 10 轮连续对话，期望整体 cached/prompt > 90%。

    GPT-TODO 填充：
        1. 构造一个 DeepSeek Provider（用 DEEPSEEK_API_KEY）
        2. 用 MetricsProvider 包装
        3. 构造 Persona / ChatAgent / 空 history
        4. 跑 10 轮：每轮塞一条简短 user 消息，让 Agent 调 no_action 收尾
           （这样无副作用，但稳定区不变）
        5. 拿 wrapped.report.overall_hit_rate 判断
    """
    _skip_if_no_key()

    # GPT-TODO: 实际实现
    pytest.skip("GPT-TODO: 待实现")


# ============================================================
# 用例 2：Task Contract 重注入不破坏前缀缓存
# ============================================================


@pytest.mark.asyncio
async def test_task_contract_preserves_prefix_cache():
    """Task Contract 应放在 messages 末尾，使前缀仍可缓存。

    跑法：连续多轮对话，每 5 轮重注入 task_contract。
    期望：注入轮的 cached_tokens 仍接近上一轮的 prompt_tokens
    （任务合约放在最后，不破坏前缀）。
    """
    _skip_if_no_key()
    pytest.skip("GPT-TODO: 待实现")


# ============================================================
# 用例 3：旧"四区"vs 新"单 system + 标签" 对比
# ============================================================


@pytest.mark.asyncio
async def test_new_structure_vs_legacy_four_zone():
    """对比新结构与旧多 system 平铺结构的命中率差异。

    旧"四区"：4 条 system 消息平铺 → 实际经验：某些 provider 不缓存非首条 system
    新单 system：1 条单一 system + XML 标签 → 更稳定缓存

    跑法：用两组 messages（同样的内容、不同的结构），分别跑相同的连续对话，
    对比 overall_hit_rate。
    """
    _skip_if_no_key()
    pytest.skip("GPT-TODO: 待实现")


# ============================================================
# 用例 4：稳定区 system 注入有效性
# ============================================================


@pytest.mark.asyncio
async def test_stable_prefix_high_cache_rate():
    """验证我们的"稳定区前置"策略真的让 KV 缓存命中。

    跑法：构造一段大 system prompt（5000+ tokens），跑 10 轮短消息，
    期望除第一轮外，cached_tokens >= system prompt 长度。
    """
    _skip_if_no_key()
    pytest.skip("GPT-TODO: 待实现")
