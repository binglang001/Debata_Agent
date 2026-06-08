"""Runtime 装配烟雾测试 —— 验证 16 步装配代码本身调用链通畅。

不连真 NapCat（monkeypatch adapter.start），但其它所有组件都真实实例化：
    paths → secrets → config → persona → memory → providers → agents →
    adapter → tools → state → wakeup → pipeline → handlers → event_bus →
    proactive_loop → 启动 → shutdown

任何一步签名不一致 / 字段名错 / import 失败，这里立刻挂。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import yaml

from core.runtime import Runtime

# ============================================================
# 准备一个最小可用的项目根（personas + config + secrets）
# ============================================================


def test_provider_health_model_maps_include_feature_providers():
    from app_config.schema import (
        AgentConfig,
        AgentsConfig,
        EmbeddingFeatureConfig,
        FeaturesConfig,
        LongTermMemoryConfig,
        ProviderConfig,
        RootConfig,
        VisionFeatureConfig,
    )

    cfg = RootConfig(
        providers={
            "chat_p": ProviderConfig(protocol="openai_compat", base_url="https://chat.example.com"),
            "vision_p": ProviderConfig(protocol="openai_compat", base_url="https://vision.example.com"),
            "embedding_p": ProviderConfig(protocol="openai_compat", base_url="https://embedding.example.com"),
        },
        agents=AgentsConfig(chat=AgentConfig(provider="chat_p", model="chat-model")),
        features=FeaturesConfig(
            vision=VisionFeatureConfig(enabled=True, provider="vision_p", model="vision-model"),
            embedding=EmbeddingFeatureConfig(
                enabled=True,
                type="api",
                provider="embedding_p",
                api_model="embedding-model",
            ),
            long_term_memory=LongTermMemoryConfig(mode="rag"),
        ),
    )
    rt = object.__new__(Runtime)
    rt.config = cfg

    assert Runtime._provider_chat_model_map(rt) == {
        "chat_p": "chat-model",
        "vision_p": "vision-model",
    }
    assert Runtime._provider_embedding_model_map(rt) == {
        "embedding_p": "embedding-model",
    }


def test_feature_provider_overrides_apply_before_provider_build():
    from app_config.schema import (
        AgentConfig,
        AgentsConfig,
        FeaturesConfig,
        ProviderConfig,
        RootConfig,
        VisionFeatureConfig,
    )

    cfg = RootConfig(
        providers={
            "chat_p": ProviderConfig(protocol="openai_compat", base_url="https://chat.example.com"),
            "vision_p": ProviderConfig(protocol="openai_compat", base_url="https://vision.example.com"),
        },
        agents=AgentsConfig(chat=AgentConfig(provider="chat_p", model="chat-model")),
        features=FeaturesConfig(
            vision=VisionFeatureConfig(
                enabled=True,
                provider="vision_p",
                model="vision-model",
                api_key_id="vision_key",
                base_url="https://vision-override.example.com/v1",
            ),
        ),
    )
    rt = object.__new__(Runtime)
    rt.config = cfg

    Runtime._apply_feature_provider_overrides(rt)

    provider = cfg.providers["vision_p"]
    assert provider.api_key_id == "vision_key"
    assert provider.base_url == "https://vision-override.example.com/v1"


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
        "app": {"name": "Debata_Agent", "log_level": "INFO"},
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
                "timeout_seconds": 120.0,
            }
        },
        "agents": {
            "chat": {
                "provider": "fake_main",
                "model": "fake-chat",
                "temperature": 0.6,
                "max_tokens": 1024,
                "max_loops": 3,
                "tool_loop_reminder_interval": 8,
                "tool_loop_final_warning_count": 4,
                "tool_loop_final_grace_loops": 2,
                "refocus_interval": 0,
                "first_token_timeout_seconds": 5.0,
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
            "long_term_memory": {"mode": "file", "keyword_trigger_save": True},
            "web_search": {"enabled": False},
        },
        "persona": {"active": "test_bot"},
        "behavior": {
            "merge_window_seconds": 0.05,
            "recall_merge_window_seconds": 0.05,
            "proactive_think_interval_seconds": 9999.0,
            "rate_limit": {"window_seconds": 60, "max_messages": 100, "enabled": False},
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
        assert rt.usage_stats is not None
        assert rt.model_activity["state"] == "idle"
        assert "fake_main" in rt.providers, "provider 未实例化"
        if rt._provider_health_task is not None:
            await rt._provider_health_task
        assert rt.provider_health["fake_main"].status == "error"
        assert "缺 API 密钥" in rt.provider_health["fake_main"].message
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
async def test_runtime_provider_health_check_does_not_block_start(
    assembled_project,
    monkeypatch,
):
    project_root, _paths = assembled_project
    started = asyncio.Event()
    never_finish = asyncio.Event()

    async def slow_health_check(self):
        started.set()
        await never_finish.wait()

    monkeypatch.setattr(Runtime, "_check_provider_health", slow_health_check)
    rt = Runtime(project_root=project_root)

    try:
        await asyncio.wait_for(rt.start(), timeout=1.0)
        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert rt._provider_health_task is not None
        assert not rt._provider_health_task.done()
    finally:
        await rt.shutdown()


@pytest.mark.asyncio
async def test_runtime_ignores_configured_asr_and_uses_napcat(
    assembled_project, fake_keyring
):
    """ASR 配置不再实例化 Whisper/云端服务，语音识别交给 NapCat fallback。"""
    project_root, paths = assembled_project

    from app_config import SecretsManager

    sm = SecretsManager(paths)
    sm.initialize()
    sm.set("asr_xfyun", "asr-api-key")

    with open(paths.CONFIG_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data.setdefault("features", {})["asr"] = {
        "enabled": True,
        "type": "api",
        "provider": "xfyun",
        "api_key_id": "asr_xfyun",
        "extra_credentials": {
            "app_id": "app-123",
            "api_secret": "secret-456",
        },
    }
    with open(paths.CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    rt = Runtime(project_root=project_root)
    try:
        await rt.start()

        assert rt.asr is None
        assert rt.pipeline.asr is None
    finally:
        await rt.shutdown()


@pytest.mark.asyncio
async def test_runtime_asr_ignore_does_not_record_failure(
    assembled_project, fake_keyring
):
    project_root, paths = assembled_project

    from app_config import SecretsManager

    sm = SecretsManager(paths)
    sm.initialize()
    sm.set("asr_xfyun", "asr-api-key")

    with open(paths.CONFIG_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data.setdefault("features", {})["asr"] = {
        "enabled": True,
        "type": "api",
        "provider": "xfyun",
        "api_key_id": "asr_xfyun",
        "extra_credentials": {
            "app_id": "app-123",
            "api_secret": "secret-456",
        },
    }
    with open(paths.CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    rt = Runtime(project_root=project_root)
    try:
        await rt.start()
        assert "asr" not in rt.feature_failures
        assert rt.config.features.asr.enabled is True
        assert rt.pipeline.asr is None
    finally:
        await rt.shutdown()


@pytest.mark.asyncio
async def test_runtime_starts_local_tts_warmup_in_background(
    assembled_project, monkeypatch
):
    project_root, paths = assembled_project

    with open(paths.CONFIG_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data.setdefault("features", {})["tts"] = {
        "enabled": True,
        "type": "local",
        "local_model": "voxcpm2",
        "model_dir": "data/models/VoxCPM2",
        "device": "cuda",
        "load_denoiser": False,
        "cfg_value": 2.0,
        "inference_timesteps": 10,
    }
    with open(paths.CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    class FakeTTS:
        def __init__(self):
            self.warmups = 0
            self.closed = False

        async def warmup(self):
            self.warmups += 1

        async def synthesize(self, text, *, reference_audio=None, prompt=""):
            return paths.WORKSPACE_DIR / "fake.wav"

        async def aclose(self):
            self.closed = True

    fake_tts = FakeTTS()
    build_configs = []

    class FakePluginManager:
        def __init__(self, plugins_dir):
            self.plugins_dir = plugins_dir

        def scan(self):
            pass

        def list_all(self):
            return [SimpleNamespace(meta=SimpleNamespace(name="voxcpm2"))]

        def get(self, name):
            return object() if name == "voxcpm2" else None

        def build(self, name, config):
            build_configs.append((name, config))
            return fake_tts

        async def shutdown_all(self):
            pass

    import plugins

    monkeypatch.setattr(plugins, "PluginManager", FakePluginManager)

    rt = Runtime(project_root=project_root)
    try:
        await rt.start()
        for _ in range(50):
            if fake_tts.warmups:
                break
            await asyncio.sleep(0.02)

        assert rt.tts is fake_tts
        assert rt.pipeline.tts is fake_tts
        assert fake_tts.warmups == 1
        assert build_configs[0][0] == "voxcpm2"
        assert build_configs[0][1]["device"] == "cuda"
        assert build_configs[0][1]["load_denoiser"] is False
        assert build_configs[0][1]["cfg_value"] == 2.0
        assert build_configs[0][1]["inference_timesteps"] == 10
    finally:
        await rt.shutdown()


@pytest.mark.asyncio
async def test_runtime_starts_edge_tts_with_configured_voice(
    assembled_project, monkeypatch
):
    project_root, paths = assembled_project

    with open(paths.CONFIG_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data.setdefault("features", {})["tts"] = {
        "enabled": True,
        "type": "api",
        "provider": "edge",
        "api_key_id": None,
        "extra_credentials": {
            "voice": "zh-CN-XiaoyiNeural",
            "rate": "+10%",
            "volume": "+5%",
            "pitch": "+0Hz",
        },
    }
    with open(paths.CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    instances = []

    class FakeEdgeTTS:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.warmups = 0
            instances.append(self)

        async def warmup(self):
            self.warmups += 1

        async def synthesize(self, text, *, reference_audio=None, prompt=""):
            return paths.WORKSPACE_DIR / "fake.mp3"

        async def aclose(self):
            pass

    import features.tts

    monkeypatch.setattr(features.tts, "_get_edge_service", lambda: FakeEdgeTTS)

    rt = Runtime(project_root=project_root)
    try:
        await rt.start()
        for _ in range(50):
            if instances and instances[0].warmups:
                break
            await asyncio.sleep(0.02)

        assert instances
        assert rt.tts is instances[0]
        assert rt.pipeline.tts is instances[0]
        assert instances[0].kwargs["voice"] == "zh-CN-XiaoyiNeural"
        assert instances[0].kwargs["rate"] == "+10%"
        assert instances[0].kwargs["volume"] == "+5%"
        assert instances[0].kwargs["pitch"] == "+0Hz"
        assert instances[0].warmups == 1
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
    with open(paths.CONFIG_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data["agents"]["chat"]["provider"] = "ghost_provider"
    with open(paths.CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    rt = Runtime(project_root=project_root)
    with pytest.raises((RuntimeError, Exception)):
        await rt.start()
    # 不强求清理（异常状态）；调用方应自己捕获
