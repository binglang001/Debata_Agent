"""Runtime 装配烟雾测试 —— 验证 16 步装配代码本身调用链通畅。

不连真 NapCat（monkeypatch adapter.start），但其它所有组件都真实实例化：
    paths → secrets → config → persona → memory → providers → agents →
    adapter → tools → state → wakeup → pipeline → handlers → event_bus →
    proactive_loop → 启动 → shutdown

任何一步签名不一致 / 字段名错 / import 失败，这里立刻挂。
"""

from __future__ import annotations

import pytest
import yaml

from core.runtime import Runtime


# ============================================================
# 准备一个最小可用的项目根（personas + config + secrets）
# ============================================================


def _write_minimal_persona(personas_dir):
    """在 personas_dir/test_bot/persona_prompt.py 写一份最小人格。"""
    persona_dir = personas_dir / "test_bot"
    persona_dir.mkdir(parents=True, exist_ok=True)
    (persona_dir / "persona_prompt.py").write_text(
        'PERSONA_PROMPT = "<identity>测试用 AI</identity>"\n'
        'PERSONA_VARS = {"name": "测试机器人", "admins": []}\n',
        encoding="utf-8",
    )


def _write_minimal_config(paths):
    """写一份能跑通 Runtime 装配的最小 config.yaml。"""
    data = {
        "version": 2,
        "app": {"name": "Diana_Agent", "log_level": "INFO"},
        "adapters": {
            "default": {
                "type": "napcat",
                "enabled": True,
                "mode": "client",
                "host": "127.0.0.1",
                "port": 3001,
                "path": "/",
                "access_token_id": None,
                "manage_process": False,
                "whitelist": {"mode": "verify", "qq_ids": [], "group_ids": []},
            }
        },
        "providers": {
            "fake_main": {
                "preset": None,
                "protocol": "openai_compat",
                "base_url": "https://example.com",
                "api_key_id": None,
                "timeout": 120.0,
            }
        },
        "agents": {
            "chat": {
                "provider": "fake_main",
                "model": "fake-chat",
                "temperature": 0.6,
                "max_tokens": 1024,
                "max_loops": 3,
                "refocus_interval": 0,
                "first_token_timeout": 5.0,
            },
            "proactive": {
                "provider": "fake_main",
                "model": "fake-chat",
                "temperature": 0.3,
                "max_tokens": 64,
            },
            "summary": {
                "provider": "fake_main",
                "model": "fake-chat",
                "temperature": 0.1,
                "max_tokens": 256,
            },
        },
        "features": {
            "long_term_memory": {"mode": "file", "keyword_force_save": True},
            "web_search": {"enabled": False},
        },
        "persona": {"active": "test_bot"},
        "behavior": {
            "merge_window": 0.05,
            "recall_merge_window": 0.05,
            "greeting_interval": 9999.0,
            "rate_limit": {"window": 60, "max_messages": 100, "enabled": False},
        },
    }
    paths.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(paths.CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


# ============================================================
# fixture：完整准备一个能装配的项目根
# ============================================================


@pytest.fixture
def assembled_project(tmp_path, fake_keyring, monkeypatch):
    """tmp_path 是项目根。返回 (project_root, paths)。"""
    from app_config import AppPaths

    paths = AppPaths(project_root=tmp_path)
    paths.ensure_data_dirs()
    _write_minimal_persona(paths.PERSONAS_DIR)
    _write_minimal_config(paths)

    # 准备空 presets 目录（providers/presets/）
    paths.PROVIDER_PRESETS_DIR.mkdir(parents=True, exist_ok=True)

    # monkeypatch：NapCatAdapter.start/stop 短路（不连真 WS）
    from adapters.napcat.adapter import NapCatAdapter

    async def fake_start(self):
        # 标记成已连接（让 is_connected 返回 True）
        self._connection._connected = True  # type: ignore[attr-defined]

    async def fake_stop(self):
        self._connection._connected = False  # type: ignore[attr-defined]

    monkeypatch.setattr(NapCatAdapter, "start", fake_start)
    monkeypatch.setattr(NapCatAdapter, "stop", fake_stop)

    return tmp_path, paths


# ============================================================
# 烟雾测试
# ============================================================


@pytest.mark.asyncio
async def test_runtime_start_assembles_all_components(assembled_project):
    """Runtime.start() 应完成 16 步装配，所有组件就位。"""
    project_root, paths = assembled_project
    rt = Runtime(project_root=project_root)

    try:
        await rt.start()

        # 验证关键组件已实例化
        assert rt.paths is not None, "paths 未装配"
        assert rt.secrets is not None, "secrets 未装配"
        assert rt.config is not None, "config 未装配"
        assert rt.persona is not None and rt.persona.name == "test_bot"
        assert rt.history is not None
        assert rt.important is not None
        assert "fake_main" in rt.providers, "provider 未实例化"
        assert rt.chat_agent is not None
        assert rt.proactive_agent is not None
        assert rt.summary_agent is not None, "summary_agent 应该实例化（config 已给）"
        assert rt.adapter is not None
        assert rt.tool_registry is not None
        assert rt.pending_requests is not None
        assert rt.wakeup_scheduler is not None
        assert rt.pipeline is not None
        assert rt.recall_handler is not None
        assert rt.request_handler is not None
        assert rt.event_bus is not None
        assert rt.proactive_loop is not None

        # 验证 wakeup 双向依赖已回填
        assert rt.wakeup_scheduler._on_fire == rt.pipeline.run_wakeup_turn

        # 验证 pipeline 拿到了 summary_agent
        assert rt.pipeline.summary_agent is rt.summary_agent

    finally:
        await rt.shutdown()


@pytest.mark.asyncio
async def test_runtime_shutdown_cleanly(assembled_project):
    """shutdown 应能按相反顺序关闭所有组件，不抛异常。"""
    project_root, _ = assembled_project
    rt = Runtime(project_root=project_root)
    await rt.start()
    # 不应抛
    await rt.shutdown()


@pytest.mark.asyncio
async def test_runtime_handles_missing_provider_reference(assembled_project):
    """agents.chat.provider 指向不存在的 provider 时应给明确错误。"""
    project_root, paths = assembled_project

    # 改 config.yaml 让 chat.provider 指向不存在的 ID
    with open(paths.CONFIG_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data["agents"]["chat"]["provider"] = "ghost_provider"
    with open(paths.CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    rt = Runtime(project_root=project_root)
    with pytest.raises((RuntimeError, Exception)):
        await rt.start()
    # 不强求清理（异常状态）；调用方应自己捕获
