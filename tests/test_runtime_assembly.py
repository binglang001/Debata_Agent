"""Runtime 装配烟雾测试 —— 验证 16 步装配代码本身调用链通畅。

不连真 NapCat（monkeypatch adapter.start），但其它所有组件都真实实例化：
    paths → secrets → config → persona → memory → providers → agents →
    adapter → tools → state → wakeup → pipeline → handlers → event_bus →
    proactive_loop → 启动 → shutdown

任何一步签名不一致 / 字段名错 / import 失败，这里立刻挂。
"""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import orjson
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
            "long_term_memory": {"mode": "file"},
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


def _enable_rag_config(paths):
    with open(paths.CONFIG_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data.setdefault("features", {})["long_term_memory"] = {
        "mode": "rag",
        "rag_top_k": 3,
    }
    data["features"]["embedding"] = {
        "enabled": True,
        "type": "api",
        "provider": "fake_main",
        "api_model": "fake-embedding",
    }
    with open(paths.CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def _patch_rag_runtime_network(monkeypatch):
    class FakeEmbeddingService:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False

        async def warmup(self):
            pass

        async def embed_one(self, text):
            return [1.0, 0.0, 0.0]

        async def embed_batch(self, texts):
            return [[1.0, 0.0, 0.0] for _text in texts]

        @property
        def dimension(self):
            return 3

        async def aclose(self):
            self.closed = True

    import features.embedding

    monkeypatch.setattr(
        features.embedding,
        "OpenAICompatEmbeddingService",
        FakeEmbeddingService,
    )
    monkeypatch.setattr(Runtime, "_schedule_provider_health_check", lambda self: None)


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
        assert rt.persona_agent is None
        assert rt.social_agent is None
        assert rt.subconscious_agent is None
        assert rt.persona_db is None
        from memory import (
            DebataArchiveStore,
            DebataEventStore,
            DebataHistoryStore,
            DebataImportantStore,
            DebataRollingSummaryStore,
            DebataUsageStatsStore,
        )

        runtime_db_path = paths.memory_dir_for("test_bot") / "test_bot.db"
        assert runtime_db_path.exists()
        assert not (paths.memory_dir_for("test_bot") / "debata.db").exists()
        assert isinstance(rt.history._store, DebataHistoryStore)
        assert isinstance(rt.event_store.store, DebataEventStore)
        assert isinstance(rt.important._store, DebataImportantStore)
        assert isinstance(rt.archive._store, DebataArchiveStore)
        assert isinstance(rt.rolling_summary, DebataRollingSummaryStore)
        assert isinstance(rt.usage_stats, DebataUsageStatsStore)
        assert rt.history._store.db.path == runtime_db_path
        assert rt.event_store.store.db.path == runtime_db_path
        assert rt.important._store.db.path == runtime_db_path
        assert rt.archive._store.db.path == runtime_db_path
        assert rt.rolling_summary.db.path == runtime_db_path
        assert rt.usage_stats.db.path == runtime_db_path
        assert not (paths.memory_dir_for("test_bot") / "persona.db").exists()
        assert rt.adapter is not None
        assert rt.tool_registry is not None
        assert rt.pending_requests is not None
        assert rt.wakeup_scheduler is not None
        assert rt.pipeline is not None
        assert rt.recall_handler is not None
        assert rt.request_handler is not None
        assert rt.event_bus is not None
        assert rt.proactive_loop is not None
        assert rt.proactive_loop.proactive_agent is rt.proactive_agent

        # 验证 wakeup 双向依赖已回填
        assert rt.wakeup_scheduler._on_fire == rt.pipeline.run_wakeup_turn

        # 验证 pipeline 拿到了 summary_agent
        assert rt.pipeline.summary_agent is rt.summary_agent

    finally:
        await rt.shutdown()


@pytest.mark.asyncio
async def test_runtime_rag_uses_instance_vector_dir(assembled_project, monkeypatch):
    project_root, paths = assembled_project
    _enable_rag_config(paths)
    _patch_rag_runtime_network(monkeypatch)

    rt = Runtime(project_root=project_root)
    try:
        await rt.start()

        expected = paths.vector_dir_for("test_bot") / "rag_memory.sqlite3"
        old_path = paths.memory_dir_for("test_bot") / "rag_memory.sqlite3"
        assert rt.rag_store is not None
        assert rt.rag_store.path == expected
        assert rt.rag_store.path != old_path
        assert expected.exists()
        assert not old_path.exists()
    finally:
        await rt.shutdown()


@pytest.mark.asyncio
async def test_runtime_copies_legacy_rag_vector_db_once(
    assembled_project,
    monkeypatch,
):
    project_root, paths = assembled_project
    _enable_rag_config(paths)
    _patch_rag_runtime_network(monkeypatch)

    old_path = paths.memory_dir_for("test_bot") / "rag_memory.sqlite3"
    new_path = paths.vector_dir_for("test_bot") / "rag_memory.sqlite3"
    old_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(old_path) as conn:
        conn.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        conn.execute("INSERT INTO marker (value) VALUES ('legacy')")
        conn.commit()

    rt = Runtime(project_root=project_root)
    try:
        await rt.start()
        assert rt.rag_store.path == new_path
        assert old_path.exists()
        assert new_path.exists()
    finally:
        await rt.shutdown()

    with sqlite3.connect(new_path) as conn:
        assert conn.execute("SELECT value FROM marker").fetchone()[0] == "legacy"
        conn.execute("UPDATE marker SET value = 'new'")
        conn.commit()

    rt2 = Runtime(project_root=project_root)
    try:
        await rt2.start()
        assert rt2.rag_store.path == new_path
    finally:
        await rt2.shutdown()

    with sqlite3.connect(new_path) as conn:
        assert conn.execute("SELECT value FROM marker").fetchone()[0] == "new"
    with sqlite3.connect(old_path) as conn:
        assert conn.execute("SELECT value FROM marker").fetchone()[0] == "legacy"


@pytest.mark.asyncio
async def test_runtime_rag_copy_failure_uses_legacy_path_and_retries(
    assembled_project,
    monkeypatch,
):
    project_root, paths = assembled_project
    _enable_rag_config(paths)
    _patch_rag_runtime_network(monkeypatch)

    old_path = paths.memory_dir_for("test_bot") / "rag_memory.sqlite3"
    new_path = paths.vector_dir_for("test_bot") / "rag_memory.sqlite3"
    old_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(old_path) as conn:
        conn.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        conn.execute("INSERT INTO marker (value) VALUES ('legacy')")
        conn.commit()

    import core.runtime as runtime_module

    original_copy2 = runtime_module.shutil.copy2

    def fail_copy2(src, dst, *, follow_symlinks=True):
        raise OSError("forced copy failure")

    monkeypatch.setattr(runtime_module.shutil, "copy2", fail_copy2)

    rt = Runtime(project_root=project_root)
    try:
        await rt.start()
        assert rt.rag_store.path == old_path
        assert old_path.exists()
        assert not new_path.exists()
    finally:
        await rt.shutdown()

    monkeypatch.setattr(runtime_module.shutil, "copy2", original_copy2)

    rt2 = Runtime(project_root=project_root)
    try:
        await rt2.start()
        assert rt2.rag_store.path == new_path
        assert new_path.exists()
    finally:
        await rt2.shutdown()

    with sqlite3.connect(new_path) as conn:
        assert conn.execute("SELECT value FROM marker").fetchone()[0] == "legacy"


@pytest.mark.asyncio
async def test_runtime_imports_legacy_memory_into_persona_db_idempotently(assembled_project):
    project_root, paths = assembled_project
    mem_dir = paths.memory_dir_for("test_bot")
    runtime_db_path = mem_dir / "test_bot.db"
    history_records = [
        {"role": "user", "content": "旧历史 1", "conversation_id": "private:1001"},
        {"role": "assistant", "content": "旧历史 2", "conversation_id": "private:1001"},
    ]
    important_json_items = [{"id": "json_only", "content": "important.json 记忆"}]
    persona_items = [{"id": "persona_only", "content": "persona.db 记忆"}]
    usage_record = {
        "ts": 1_782_000_000.0,
        "provider": "fake_main",
        "model": "fake-chat",
        "agent": "主模型",
        "operation": "agent_loop",
        "prompt_tokens": 3,
        "completion_tokens": 4,
        "total_tokens": 7,
    }

    _write_jsonl(mem_dir / "history.jsonl", history_records)
    _write_json(mem_dir / "important.json", important_json_items)
    _write_json(
        mem_dir / "rolling_summary.json",
        {
            "summary_text": "旧滚动摘要",
            "archived_until": {"history_index": 1},
            "active_start_index": 2,
            "updated_at": "2026-06-18T12:00:00Z",
        },
    )
    _write_legacy_persona_db(mem_dir / "persona.db", persona_items)
    _write_jsonl(paths.LOGS_DIR / "model_usage.jsonl", [usage_record])

    rt = Runtime(project_root=project_root)
    try:
        await rt.start()

        assert runtime_db_path.exists()
        assert not (mem_dir / "debata.db").exists()
        assert await rt.history.records() == history_records
        assert _memory_id_content_pairs(rt.important.items()) == [
            ("persona_only", "persona.db 记忆"),
            ("json_only", "important.json 记忆"),
        ]
        assert rt.rolling_summary.text() == "旧滚动摘要"
        assert rt.rolling_summary.active_start_index() == 2
        assert rt.usage_stats.count == 1
        assert rt.usage_stats.summarize("all").total_tokens == 7

        from memory import DebataPersonaDB

        persona_db = DebataPersonaDB(runtime_db_path, "test_bot")
        assert await persona_db.read_important(default=[]) == persona_items
        runtime_only_record = {
            "role": "user",
            "content": "只写入 test_bot.db 的新历史",
            "conversation_id": "private:1001",
        }
        await rt.history.add_records([runtime_only_record])
    finally:
        await rt.shutdown()

    backup_dir = runtime_db_path.parent / "backups"
    backup_count_before_second_start = _backup_count(backup_dir)
    second = Runtime(project_root=project_root)
    try:
        await second.start()

        expected_history = [*history_records, runtime_only_record]
        assert _debata_row_count(runtime_db_path, "history_records", "test_bot") == 3
        assert _debata_row_count(runtime_db_path, "important_memories", "test_bot") == 2
        assert _debata_usage_count(runtime_db_path, "test_bot") == 1
        assert await second.history.records() == expected_history
        assert _memory_id_content_pairs(second.important.items()) == [
            ("persona_only", "persona.db 记忆"),
            ("json_only", "important.json 记忆"),
        ]
    finally:
        await second.shutdown()

    assert _backup_count(backup_dir) == backup_count_before_second_start
    assert (mem_dir / "history.jsonl").exists()
    assert (mem_dir / "important.json").exists()
    assert (mem_dir / "rolling_summary.json").exists()
    assert (mem_dir / "persona.db").exists()
    assert (paths.LOGS_DIR / "model_usage.jsonl").exists()


@pytest.mark.asyncio
async def test_runtime_import_gate_includes_event_append_log(assembled_project):
    project_root, paths = assembled_project
    mem_dir = paths.memory_dir_for("test_bot")
    event = _legacy_event(9, payload={"text": "append only"})
    _write_jsonl(mem_dir / "events.sqlite3.append.jsonl", [event])

    rt = Runtime(project_root=project_root)
    try:
        await rt.start()

        loaded = await rt.event_store.get_event(9)
        assert loaded is not None
        assert loaded["payload"] == {"text": "append only"}
        assert loaded["event_uuid"] == "uuid-9"
    finally:
        await rt.shutdown()

    assert not (mem_dir / "events.sqlite3").exists()
    assert (mem_dir / "events.sqlite3.append.jsonl").exists()


@pytest.mark.asyncio
async def test_runtime_import_gate_merges_persona_db_important_source_when_persona_domain_exists(
    assembled_project,
):
    project_root, paths = assembled_project
    mem_dir = paths.memory_dir_for("test_bot")
    runtime_db_path = mem_dir / "test_bot.db"
    persona_items = [{"id": "persona_only", "content": "persona.db 记忆"}]
    _write_legacy_persona_db(mem_dir / "persona.db", persona_items)

    from memory import DebataImportantStore, DebataPersonaDB

    persona_db = DebataPersonaDB(runtime_db_path, "test_bot")
    await persona_db.load()
    assert await DebataImportantStore(runtime_db_path, "test_bot").read(default=[]) == []

    rt = Runtime(project_root=project_root)
    try:
        await rt.start()

        assert _memory_id_content_pairs(rt.important.items()) == [
            ("persona_only", "persona.db 记忆")
        ]
        assert await persona_db.read_important(default=[]) == []
    finally:
        await rt.shutdown()

    assert _debata_row_count(runtime_db_path, "important_memories", "test_bot") == 1
    assert (mem_dir / "persona.db").exists()


def test_runtime_idle_model_activity_text_is_realtime_idle(tmp_path):
    rt = Runtime(project_root=tmp_path)

    rt._update_model_activity(
        {
            "state": "idle",
            "text": "社交决策完成",
            "model": "unit-model",
            "agent": "社交决策",
        }
    )

    assert rt.model_activity["state"] == "idle"
    assert rt.model_activity["text"] == "空闲"
    assert rt.model_activity["model"] == "unit-model"
    assert rt.model_activity["agent"] == "社交决策"


def test_persona_management_agent_config_inherits_only_provider_and_model():
    from app_config.schema import AgentConfig, PersonaManagementPersonaAgentConfig

    chat_cfg = AgentConfig(
        provider="fake_main",
        model="fake-chat",
        temperature=1.4,
        top_p=0.2,
        max_tokens=777,
        first_token_timeout_seconds=4.0,
    )
    persona_cfg = PersonaManagementPersonaAgentConfig(provider="", model="")

    resolved = Runtime._resolve_persona_management_agent_config(persona_cfg, chat_cfg)

    assert resolved.provider == "fake_main"
    assert resolved.model == "fake-chat"
    assert resolved.temperature == persona_cfg.temperature
    assert resolved.top_p == persona_cfg.top_p
    assert resolved.max_tokens == persona_cfg.max_tokens
    assert resolved.reasoning == persona_cfg.reasoning
    assert (
        resolved.first_token_timeout_seconds
        == persona_cfg.first_token_timeout_seconds
    )


@pytest.mark.asyncio
async def test_runtime_persona_management_assembles_agents_and_pipeline(
    assembled_project,
):
    project_root, paths = assembled_project
    with open(paths.CONFIG_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data["persona_management"] = {
        "enabled": True,
        "persona_agent": {
            "provider": "",
            "model": "",
            "temperature": 0.2,
            "max_tokens": 321,
            "first_token_timeout_seconds": 7.0,
        },
        "social_agent": {
            "enabled": True,
            "provider": "",
            "model": "",
        },
        "subconscious": {
            "enabled": True,
            "provider": "",
            "model": "",
        },
        "physiology": {
            "energy": {"mode": "tool"},
            "satiety": {"mode": "tool"},
        },
    }
    with open(paths.CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    rt = Runtime(project_root=project_root)
    try:
        await rt.start()

        persona_db_path = paths.memory_dir_for("test_bot") / "persona.db"
        runtime_db_path = paths.memory_dir_for("test_bot") / "test_bot.db"
        assert not persona_db_path.exists()
        assert runtime_db_path.exists()
        assert not (paths.memory_dir_for("test_bot") / "debata.db").exists()
        assert rt.persona_db is not None
        assert rt.persona_agent is not None
        assert rt.social_agent is not None
        assert rt.subconscious_agent is not None
        assert rt.age_profile is None

        from memory import DebataImportantStore, DebataPersonaDB

        assert isinstance(rt.persona_db, DebataPersonaDB)
        assert isinstance(rt.important._store, DebataImportantStore)
        assert rt.persona_agent.cfg.provider == "fake_main"
        assert rt.persona_agent.cfg.model == "fake-chat"
        assert rt.persona_agent.cfg.temperature == 0.2
        assert rt.persona_agent.cfg.max_tokens == 321
        assert rt.persona_agent.cfg.first_token_timeout_seconds == 7.0
        assert rt.social_agent.cfg.provider == "fake_main"
        assert rt.social_agent.cfg.model == "fake-chat"
        assert rt.subconscious_agent.cfg.provider == "fake_main"
        assert rt.subconscious_agent.cfg.model == "fake-chat"

        assert "eat" in rt.tool_registry
        assert "sleep" in rt.tool_registry
        assert rt.eat_tool is True
        assert rt.sleep_tool is True
        assert rt.pipeline.persona_agent is rt.persona_agent
        assert rt.pipeline.subconscious_agent is rt.subconscious_agent
        assert rt.pipeline.persona_db is rt.persona_db
        assert rt.pipeline.eat_tool is True
        assert rt.pipeline.sleep_tool is True
        assert rt.proactive_loop.proactive_agent is rt.social_agent
    finally:
        await rt.shutdown()


@pytest.mark.asyncio
async def test_runtime_passes_vision_max_tokens_to_service(assembled_project):
    project_root, paths = assembled_project
    with open(paths.CONFIG_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data.setdefault("features", {})["vision"] = {
        "enabled": True,
        "type": "api",
        "provider": "fake_main",
        "model": "fake-vision",
        "max_tokens": 1536,
    }
    with open(paths.CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    rt = Runtime(project_root=project_root)
    try:
        await rt.start()

        assert rt.vision is not None
        assert rt.vision.max_tokens == 1536
    finally:
        await rt.shutdown()


@pytest.mark.asyncio
async def test_runtime_binds_friend_confirmed_hook_to_rate_limiter(
    assembled_project,
):
    project_root, paths = assembled_project
    with open(paths.CONFIG_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data["behavior"]["rate_limit"] = {
        "window_seconds": 60,
        "max_messages": 0,
        "enabled": True,
    }
    with open(paths.CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    rt = Runtime(project_root=project_root)
    try:
        await rt.start()

        await rt.adapter._emit_friend_confirmed("1001")

        assert await rt.rate_limiter.check_and_log("1001") is False
        assert await rt.rate_limiter.check_and_log("2002") is True
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


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps(data))


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(orjson.dumps(record) + b"\n" for record in records))


def _write_legacy_persona_db(path, memories):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE important_memories (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                memories_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO schema_version (id, version, updated_at)
            VALUES (1, 1, '2026-06-18 12:00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO important_memories (id, memories_json, updated_at)
            VALUES (1, ?, '2026-06-18 12:00:00')
            """,
            (orjson.dumps(memories).decode("utf-8"),),
        )


def _legacy_event(event_id, *, payload=None):
    payload_data = {"event_id": event_id} if payload is None else payload
    return {
        "event_id": event_id,
        "event_type": "message",
        "event_uuid": f"uuid-{event_id}",
        "conversation_id": "private:1",
        "session_id": "session-1",
        "turn_id": str(event_id),
        "source": "runtime",
        "external_id": f"external-{event_id}",
        "tool_call_id": None,
        "parent_event_id": None,
        "idempotency_key": None,
        "timestamp_unix": float(1000 + event_id),
        "created_at_unix": float(2000 + event_id),
        "payload_json": orjson.dumps(payload_data).decode("utf-8"),
        "payload_hash": f"hash-{event_id}",
        "schema_version": 2,
    }


def _debata_row_count(db_path, table, persona_id):
    allowed_tables = {
        "history_records": "history_records",
        "important_memories": "important_memories",
    }
    table_name = allowed_tables[table]
    with sqlite3.connect(db_path) as conn:
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table_name} WHERE persona_id = ?",
                (persona_id,),
            ).fetchone()[0]
        )


def _debata_usage_count(db_path, persona_id):
    with sqlite3.connect(db_path) as conn:
        return int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM usage_records
                WHERE persona_id = ?
                """,
                (persona_id,),
            ).fetchone()[0]
        )


def _memory_id_content_pairs(items):
    return [(item.get("id"), item.get("content")) for item in items]


def _backup_count(backup_dir):
    if not backup_dir.exists():
        return 0
    return len(list(backup_dir.glob("*.db")))
