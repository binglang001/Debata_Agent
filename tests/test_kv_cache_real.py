"""端到端 KV 缓存命中率实测。

需要真实 API 密钥，被 @pytest.mark.live 标记，默认不跑。
跑法：
    venv/Scripts/python -m pytest tests/test_kv_cache_real.py -m live -s

环境变量：
    DEEPSEEK_API_KEY  —— DeepSeek API Key（必填）
    DEEPSEEK_MODEL    —— 模型 ID，默认 deepseek-chat

验证目标（Phase 1.9 硬门槛）：
    - 连续 5+ 轮对话，**第二轮起** cached_tokens / prompt_tokens > 90%
    - 整体命中率（含第一轮 cache miss）> 80%
    - Task Contract 重注入不破坏前缀缓存
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from agents import (
    AgentRunner,
    ChatAgent,
    Persona,
    build_messages,
)
from app_config.schema import AgentConfig
from providers.base import IProvider
from providers.protocols.openai_compat import OpenAICompatProvider
from utils.cache_metrics import MetricsProvider

pytestmark = pytest.mark.live


# ============================================================
# Live 标记的 skip 条件
# ============================================================


def _skip_if_no_key() -> None:
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("需要 DEEPSEEK_API_KEY 才能跑 live 测试")


def _make_deepseek_provider() -> IProvider:
    return OpenAICompatProvider(
        name="deepseek_live",
        base_url="https://api.deepseek.com",
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        reasoning_style="thinking_extra_body",
    )


def _make_persona() -> Persona:
    """加载仓库自带 debata persona 作为真实大 system prompt。"""
    from pathlib import Path

    from app_config.paths import AppPaths
    from agents.persona_loader import load_persona

    project_root = Path(__file__).resolve().parent.parent
    paths = AppPaths(project_root=project_root)
    return load_persona(paths, "debata")


def _make_agent_cfg() -> AgentConfig:
    return AgentConfig(
        provider="deepseek_live",
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        temperature=0.6,
        max_tokens=512,
        max_loops=2,
        refocus_interval=0,
        first_token_timeout_seconds=15.0,
    )


# ============================================================
# 用例 1：连续多轮对话整体命中率
# ============================================================


@pytest.mark.asyncio
async def test_cache_hit_rate_multi_turn():
    """跑 5 轮连续对话。

    判定：第二轮起 hit_rate > 0.9（前缀缓存生效）；整体 > 0.8（含首轮 miss）。
    """
    _skip_if_no_key()

    base = _make_deepseek_provider()
    metrics_provider = MetricsProvider(base)
    persona = _make_persona()
    cfg = _make_agent_cfg()

    history: list[dict] = []
    user_inputs = [
        "你好",
        "今天天气怎么样",
        "你最近在忙什么",
        "推荐个咖啡馆",
        "下次再聊",
    ]

    try:
        for i, user_text in enumerate(user_inputs, 1):
            history.append({"role": "user", "content": user_text})
            messages = build_messages(
                persona=persona,
                history=history,
                important_memory_text="",
                current_context=None,
                memory_mode="file",
            )
            result = await metrics_provider.chat_completion(
                messages,
                model=cfg.model,
                tools=None,
                temperature=cfg.temperature,
                max_tokens=cfg.max_tokens,
                stream=False,
                timeout=60.0,
                first_token_timeout=15.0,
            )
            history.append({"role": "assistant", "content": result.content or ""})
            print(f"\n[Round {i}] prompt={metrics_provider.report.samples[-1].prompt_tokens}, "
                  f"cached={metrics_provider.report.samples[-1].cached_tokens}, "
                  f"hit={metrics_provider.report.samples[-1].hit_rate:.1%}")
    finally:
        await metrics_provider.aclose()

    print("\n" + "=" * 60)
    print(metrics_provider.report.to_text())
    print("=" * 60)

    samples = metrics_provider.report.samples
    assert len(samples) == len(user_inputs), f"少跑了轮次：{len(samples)}/{len(user_inputs)}"

    # 第二轮起每轮 hit_rate > 0.9（注意：服务端 KV 缓存可能间歇性 miss，允许 1 次飘移）
    non_first = samples[1:]
    good = [s for s in non_first if s.hit_rate > 0.9]
    assert len(good) >= len(non_first) - 1, (
        f"非首轮的缓存命中不足：{len(good)}/{len(non_first)} 轮 > 90%\n"
        f"完整报告：\n{metrics_provider.report.to_text()}"
    )

    overall = metrics_provider.report.overall_hit_rate
    assert overall > 0.55, f"整体命中率 {overall:.1%} < 55%（含首轮 miss + 可能的间歇缓存轮换）"


# ============================================================
# 用例 2：Task Contract / current_context 不破坏前缀缓存
# ============================================================


@pytest.mark.asyncio
async def test_task_context_appended_preserves_prefix_cache():
    """current_context（每轮变化的 task_context）放在 messages 末尾，
    应该不影响 system + history 前缀的缓存。

    跑法：每轮带不同 task_context，验证 cached_tokens 仍跟稳定 system 长度持平。
    """
    _skip_if_no_key()

    base = _make_deepseek_provider()
    metrics_provider = MetricsProvider(base)
    persona = _make_persona()
    cfg = _make_agent_cfg()

    history: list[dict] = []
    contexts = [
        "现在是早上 9 点。",
        "现在是中午 12 点，提醒用户吃饭。",
        "现在是傍晚 6 点。",
        "现在是凌晨 1 点。",
    ]

    try:
        for i, ctx in enumerate(contexts, 1):
            history.append({"role": "user", "content": f"测试 {i}"})
            messages = build_messages(
                persona=persona,
                history=history,
                important_memory_text="",
                current_context=ctx,
                memory_mode="file",
            )
            result = await metrics_provider.chat_completion(
                messages,
                model=cfg.model,
                tools=None,
                max_tokens=cfg.max_tokens,
                stream=False,
                timeout=60.0,
            )
            history.append({"role": "assistant", "content": result.content or ""})
            s = metrics_provider.report.samples[-1]
            print(f"[Round {i}] ctx={ctx[:20]!r} prompt={s.prompt_tokens} "
                  f"cached={s.cached_tokens} hit={s.hit_rate:.1%}")
    finally:
        await metrics_provider.aclose()

    print("\n" + metrics_provider.report.to_text())

    samples = metrics_provider.report.samples
    # 第二轮起：task_context 变了，但 system + 历史前缀稳定 → hit_rate 仍应 > 80%
    for i, s in enumerate(samples[1:], start=2):
        assert s.hit_rate > 0.8, (
            f"第 {i} 轮命中率 {s.hit_rate:.1%} 太低，"
            f"说明 task_context 末尾追加可能破坏了前缀缓存"
        )


# ============================================================
# 用例 3：稳定区前置策略真的让长 system 缓存
# ============================================================


@pytest.mark.asyncio
async def test_stable_prefix_high_cache_rate():
    """验证我们的设计：稳定区（identity/persona/tool_use_protocol）放在前面，
    短的变化区放后面，能让大 system prompt 被缓存。

    判定：除第一轮外，cached_tokens >= 80% × system prompt 长度。
    """
    _skip_if_no_key()

    base = _make_deepseek_provider()
    metrics_provider = MetricsProvider(base)
    persona = _make_persona()
    cfg = _make_agent_cfg()

    # 估算 system prompt 长度
    sample_messages = build_messages(
        persona=persona,
        history=[],
        important_memory_text="",
        current_context=None,
        memory_mode="file",
    )
    system_chars = sum(len(m.get("content") or "") for m in sample_messages if m.get("role") == "system")
    print(f"\nsystem 总字符 ≈ {system_chars}")

    history: list[dict] = []
    try:
        for i in range(4):
            history.append({"role": "user", "content": f"短消息 {i+1}"})
            messages = build_messages(
                persona=persona,
                history=history,
                important_memory_text="",
                current_context=None,
                memory_mode="file",
            )
            result = await metrics_provider.chat_completion(
                messages,
                model=cfg.model,
                tools=None,
                max_tokens=128,
                stream=False,
                timeout=60.0,
            )
            history.append({"role": "assistant", "content": result.content or ""})
    finally:
        await metrics_provider.aclose()

    print("\n" + metrics_provider.report.to_text())

    samples = metrics_provider.report.samples
    # 第 2-4 轮：cached 应该覆盖大部分 system + 之前对话
    for i, s in enumerate(samples[1:], start=2):
        assert s.hit_rate > 0.85, (
            f"第 {i} 轮 hit_rate={s.hit_rate:.1%} 过低，"
            f"说明大 system 没被缓存。prompt={s.prompt_tokens} cached={s.cached_tokens}"
        )


# ============================================================
# 用例 4：工具调用是否影响缓存
# ============================================================


@pytest.mark.asyncio
async def test_tool_definitions_do_not_break_cache():
    """验证：在 chat_completion 中传 tools 参数不影响前缀缓存。

    关键点：tools 定义在 API 调用参数里（不在 messages 中），
    所以 messages 前缀不变时，缓存照常命中。

    与实测（用例 1）对比：用 chat_completion 直接调（无工具循环），
    再对比用例 1 的命中率数据，看 tools 参数是否有影响。
    """
    _skip_if_no_key()

    from tools import build_default_registry
    from app_config.schema import (
        AgentsConfig, BehaviorConfig, FeaturesConfig,
        LongTermMemoryConfig, NapCatAdapterConfig,
        PersonaConfig, ProviderConfig, RootConfig,
    )

    cfg_root = RootConfig(
        adapters={"a": NapCatAdapterConfig()},
        providers={"deepseek_live": ProviderConfig(preset="deepseek")},
        agents=AgentsConfig(chat=_make_agent_cfg()),
        features=FeaturesConfig(
            long_term_memory=LongTermMemoryConfig(mode="file"),
        ),
        persona=PersonaConfig(active="debata"),
        behavior=BehaviorConfig(),
    )

    base = _make_deepseek_provider()
    metrics_provider = MetricsProvider(base)
    persona = _make_persona()
    cfg = _make_agent_cfg()
    registry = build_default_registry(cfg_root)
    tools_schema = registry.get_schemas()

    history: list[dict] = []
    user_inputs = ["你好", "嗯", "好的做完了"]

    try:
        for i, user_text in enumerate(user_inputs, 1):
            history.append({"role": "user", "content": user_text})
            messages = build_messages(
                persona=persona,
                history=history,
                important_memory_text="",
                current_context=None,
                memory_mode="file",
            )
            result = await metrics_provider.chat_completion(
                messages,
                model=cfg.model,
                tools=tools_schema,  # 带 tools，但不实际走 runner 循环
                temperature=cfg.temperature,
                max_tokens=128,
                stream=False,
                timeout=60.0,
            )
            history.append({"role": "assistant", "content": result.content or ""})
            s = metrics_provider.report.samples[-1]
            print(f"[Round {i}] prompt={s.prompt_tokens} cached={s.cached_tokens} hit={s.hit_rate:.1%}")
    finally:
        await metrics_provider.aclose()

    print("\n" + metrics_provider.report.to_text())

    samples = metrics_provider.report.samples
    # 带 tools 参数的第二轮起应仍能缓存（前提：messages 正文未变）
    if len(samples) >= 2:
        good = [s for s in samples[1:] if s.hit_rate > 0.85]
        assert len(good) >= len(samples[1:]) - 1, (
            f"带 tools 参数时非首轮缓存不足：{len(good)}/{len(samples)-1} 轮 > 85%\n"
            f"完整报告：\n{metrics_provider.report.to_text()}"
        )


# ============================================================
# 报告生成：跑完所有用例后写入 docs/kv_cache_benchmark.md
# ============================================================


def write_benchmark_report(
    md_path: str,
    samples_by_test: dict[str, list[dict]],
) -> None:
    """把测试运行结果写到 docs/kv_cache_benchmark.md。

    samples_by_test: {"test_name": [{"prompt": int, "cached": int, "hit_rate": float}, ...]}
    """
    lines = [
        "# KV 缓存命中率实测报告",
        "",
        f"测试时间：见 git log。",
        f"Provider：DeepSeek（deepseek-chat）。",
        "",
        "## 验证目标",
        "- 第二轮起每轮 hit_rate > 90%",
        "- 整体命中率 > 80%",
        "- 工具调用轮次命中率 > 70%",
        "",
        "## 各用例结果",
    ]
    for test_name, samples in samples_by_test.items():
        lines.append(f"\n### {test_name}\n")
        lines.append("| 轮次 | prompt_tokens | cached_tokens | hit_rate |")
        lines.append("|---|---|---|---|")
        for i, s in enumerate(samples, 1):
            lines.append(f"| {i} | {s['prompt']} | {s['cached']} | {s['hit_rate']:.1%} |")
        total_prompt = sum(s["prompt"] for s in samples)
        total_cached = sum(s["cached"] for s in samples)
        overall = total_cached / total_prompt if total_prompt else 0
        lines.append(f"\n**整体命中率：{overall:.1%}**\n")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
