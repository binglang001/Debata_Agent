"""Runtime —— 整个应用的生命周期管理与依赖装配。

把 Phase 1.0~1.7 所有组件串起来：
    1. 加载配置（已加密的密钥从 SecretsManager 取）
    2. 初始化 SecretsManager + 解密所有密钥
    3. 加载人格 → 准备 Persona
    4. 初始化 HistoryManager / ImportantMemoryManager
    5. 实例化各 Provider（按配置）
    6. 实例化 ChatAgent / ProactiveRouterAgent / SummaryAgent
    7. 实例化 NapCatAdapter
    8. 构建 ToolRegistry
    9. 实例化 WakeupScheduler / PendingRequestStore / RateLimiter
    10. 实例化 MessagePipeline
    11. 实例化 RecallHandler / RequestHandler / ProactiveLoop
    12. 实例化 EventBus 并订阅
    13. 启动 adapter / proactive loop
    14. 等待 stop 信号
    15. 优雅关闭

main.py 调用此类即可。
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class Runtime:
    """应用运行时容器。

    最小用法：
        rt = Runtime(project_root=Path("."))
        await rt.start()
        await rt.wait_until_stop()
        await rt.shutdown()
    """

    def __init__(self, project_root: Path, config_file: Path | None = None) -> None:
        self.project_root = project_root
        self._config_file = config_file
        self._stop_event = asyncio.Event()

        # 组件占位（在 start() 中实例化）
        self.config: Any = None
        self.secrets: Any = None
        self.paths: Any = None
        self.persona: Any = None
        self.history: Any = None
        self.important: Any = None
        self.archive: Any = None
        self.rolling_summary: Any = None
        self.adapter: Any = None
        self.provider_registry: Any = None
        self.providers: dict[str, Any] = {}
        self.chat_agent: Any = None
        self.proactive_agent: Any = None
        self.summary_agent: Any = None
        self.tool_registry: Any = None
        self.wakeup_scheduler: Any = None
        self.pending_requests: Any = None
        self.rate_limiter: Any = None
        self.pipeline: Any = None
        self.recall_handler: Any = None
        self.request_handler: Any = None
        self.proactive_loop: Any = None
        self.event_bus: Any = None

        self.vision: Any = None
        self.web_search: Any = None
        self.weather: Any = None
        self.embedding_service: Any = None
        self.rag_store: Any = None
        self.rag_memory: Any = None
        self.plugin_manager: Any = None
        self.asr: Any = None
        self.tts: Any = None
        self.provider_health: dict[str, Any] = {}
        self.feature_failures: dict[str, str] = {}
        self.usage_stats: Any = None
        self.model_activity: dict[str, Any] = {
            "state": "idle",
            "text": "空闲",
            "model": "",
            "agent": "主模型",
            "updated_at": time.time(),
        }
        self._provider_health_task: asyncio.Task | None = None
        self._shutdown_started = False
        self._shutdown_complete = False

    # ============================================================
    # 启动流程
    # ============================================================

    async def start(self) -> None:
        """按顺序装配并启动所有组件。"""
        self._stop_event = asyncio.Event()
        self._shutdown_started = False
        self._shutdown_complete = False
        logger.info("Runtime 启动中...")
        start_t0 = time.monotonic()

        # ----- 1. 路径与配置 -----
        stage_t0 = time.monotonic()
        from app_config import AppPaths, SecretsManager, load_config

        self.paths = AppPaths(project_root=self.project_root, config_file=self._config_file)
        self.paths.ensure_data_dirs()
        os.environ.setdefault("DEBATA_MODELS_DIR", str(self.paths.MODELS_DIR.resolve()))
        self.secrets = SecretsManager(self.paths)
        self.secrets.initialize()
        self.config = load_config(self.paths)
        from .usage_stats import UsageStatsStore

        self.usage_stats = UsageStatsStore(self.paths.LOGS_DIR / "model_usage.jsonl")
        await self.usage_stats.load()
        self._apply_feature_provider_overrides()
        # 按 config 调整全局日志级别（main.py 启动时只设了 INFO）
        try:
            logging.getLogger().setLevel(
                getattr(logging, self.config.app.log_level, logging.INFO)
            )
        except Exception:
            logger.warning(f"无效的日志级别: {self.config.app.log_level}，回退 INFO")
        logger.debug(f"配置已加载（persona={self.config.persona.active}）")
        logger.debug("Runtime 阶段完成：配置和密钥 %.2fs", time.monotonic() - stage_t0)

        # ----- 2. 人格 -----
        stage_t0 = time.monotonic()
        from agents import list_available_personas, load_persona

        active = self.config.persona.active
        try:
            self.persona = load_persona(self.paths, active)
        except FileNotFoundError:
            available = list_available_personas(self.paths)
            if not available:
                raise RuntimeError(
                    f"未找到人格 {active!r}，且 {self.paths.PERSONAS_DIR} 下无任何可用人格。"
                ) from None
            raise RuntimeError(
                f"未找到人格 {active!r}。\n"
                f"  可用人格: {available}\n"
                f"  请编辑 {self.paths.CONFIG_FILE} 把 persona.active 改成上述之一，"
                f"然后重新启动。"
            ) from None
        logger.debug(f"人格已加载: {self.persona.name}")
        logger.debug("Runtime 阶段完成：人格加载 %.2fs", time.monotonic() - stage_t0)

        # ----- 3. 记忆 -----
        stage_t0 = time.monotonic()
        from memory import (
            ArchiveStore,
            HistoryManager,
            ImportantMemoryManager,
            RollingSummaryStore,
        )

        mem_dir = self.paths.memory_dir_for(self.persona.name)
        mem_dir.mkdir(parents=True, exist_ok=True)
        self.history = HistoryManager(mem_dir / "history.jsonl")
        self.important = ImportantMemoryManager(mem_dir / "important.json")
        self.archive = ArchiveStore(mem_dir / "archive.jsonl")
        self.rolling_summary = RollingSummaryStore(mem_dir / "rolling_summary.json")
        await self.history.load()
        await self.important.load()
        await self.archive.load()
        await self.rolling_summary.load()
        self._hist_len = await self.history.length()
        logger.debug(
            f"记忆已加载（history={self._hist_len} 条，important={len(self.important.items())} 条）"
        )
        logger.debug("Runtime 阶段完成：记忆加载 %.2fs", time.monotonic() - stage_t0)

        # ----- 4. Providers（用 ProviderRegistry 统一管理便于 close_all）-----
        stage_t0 = time.monotonic()
        from providers import ProviderRegistry

        self.provider_registry = ProviderRegistry()
        self.provider_registry.load_presets(self.paths.PROVIDER_PRESETS_DIR)
        self.provider_registry.build_from_config(self.config.providers, self.secrets)
        self.providers = {
            name: self.provider_registry.get(name)
            for name in self.provider_registry.list_names()
        }
        logger.debug(f"Provider 已实例化: {list(self.providers.keys())}")
        logger.debug("Runtime 阶段完成：Provider 构造 %.2fs", time.monotonic() - stage_t0)

        # ----- 5. Agents -----
        stage_t0 = time.monotonic()
        from agents import ChatAgent, ProactiveRouterAgent, SummaryAgent

        chat_cfg = self.config.agents.chat
        if chat_cfg.provider not in self.providers:
            raise RuntimeError(
                f"agents.chat.provider={chat_cfg.provider!r} 不在 providers 中。"
                f"已实例化: {list(self.providers.keys())}"
            )
        self.chat_agent = ChatAgent(
            self.providers[chat_cfg.provider],
            chat_cfg,
            usage_recorder=self._record_model_usage,
            status_callback=self._update_model_activity,
        )

        if self.config.agents.proactive is not None:
            pcfg = self.config.agents.proactive
            if pcfg.provider not in self.providers:
                raise RuntimeError(
                    f"agents.proactive.provider={pcfg.provider!r} 不在 providers 中"
                )
            self.proactive_agent = ProactiveRouterAgent(
                self.providers[pcfg.provider],
                pcfg,
                usage_recorder=self._record_model_usage,
                status_callback=self._update_model_activity,
            )

        if self.config.agents.summary is not None:
            scfg = self.config.agents.summary
            if scfg.provider not in self.providers:
                raise RuntimeError(
                    f"agents.summary.provider={scfg.provider!r} 不在 providers 中"
                )
            # SummaryAgent 签名: (provider, cfg, summarize_cfg)
            self.summary_agent = SummaryAgent(
                self.providers[scfg.provider],
                scfg,
                self.config.behavior.summarize,
                usage_recorder=self._record_model_usage,
                status_callback=self._update_model_activity,
            )

        logger.debug("Runtime 阶段完成：Agent 构造 %.2fs", time.monotonic() - stage_t0)

        # ----- 6. Features service（按 enabled 实例化）-----
        stage_t0 = time.monotonic()
        from features import VisionService, WeatherService, WebSearchService

        if self.config.features.vision.enabled:
            vcfg = self.config.features.vision
            if vcfg.type != "api":
                logger.warning(
                    "features.vision.type=local 当前未实装（P3），vision 服务跳过实例化"
                )
            elif vcfg.provider and vcfg.provider in self.providers:
                self.vision = VisionService(
                    provider=self.providers[vcfg.provider],
                    model=vcfg.model or "",
                )
                logger.info(
                    f"VisionService 已启用：provider={vcfg.provider}, model={vcfg.model}"
                )
            else:
                logger.warning(
                    f"features.vision.enabled=True 但 provider={vcfg.provider!r} "
                    f"不在 providers 中，vision 服务跳过实例化"
                )

        if self.config.features.web_search.enabled:
            wscfg = self.config.features.web_search
            self.web_search = WebSearchService(
                max_results=wscfg.max_results,
                timeout_seconds=wscfg.timeout_seconds,
            )
            logger.info("WebSearchService 已启用（DuckDuckGo）")

        if self.config.features.weather.enabled:
            wcfg = self.config.features.weather
            api_key = self.secrets.get(wcfg.api_key_id) if wcfg.api_key_id else None
            if api_key:
                self.weather = WeatherService(
                    api_key=api_key,
                    host=wcfg.host,
                )
                logger.info(f"WeatherService 已启用：host={wcfg.host}")
            else:
                logger.warning(
                    f"features.weather.enabled=True 但 api_key_id={wcfg.api_key_id!r} "
                    f"没找到密钥，weather 服务跳过实例化"
                )

        # ----- 6.5 RAG 长期记忆（embedding + 向量存储）-----
        if self.config.features.long_term_memory.mode == "rag":
            await self._setup_rag(mem_dir)
        logger.debug("Runtime 阶段完成：Feature 服务 %.2fs", time.monotonic() - stage_t0)

        # ----- 6.6 插件扫描（ASR / TTS / 本地 embedding，按需）-----
        stage_t0 = time.monotonic()
        await self._setup_plugins()
        logger.debug("Runtime 阶段完成：插件扫描/启用 %.2fs", time.monotonic() - stage_t0)

        # ----- 7. Adapter -----
        stage_t0 = time.monotonic()
        from adapters.napcat.adapter import NapCatAdapter

        if not self.config.adapters:
            raise RuntimeError("配置中没有任何 adapter")
        adapter_name, adapter_cfg = next(iter(self.config.adapters.items()))
        self.adapter = NapCatAdapter.from_config(adapter_name, adapter_cfg, self.secrets)
        logger.debug(f"Adapter 已实例化: {adapter_name}")
        logger.debug("Runtime 阶段完成：Adapter 构造 %.2fs", time.monotonic() - stage_t0)

        # ----- 8. Tools -----
        stage_t0 = time.monotonic()
        from tools import build_default_registry

        self.tool_registry = build_default_registry(self.config)
        logger.debug("Runtime 阶段完成：工具注册 %.2fs", time.monotonic() - stage_t0)

        # 启动摘要：一行涵盖人格/记忆/provider/adapter/tools
        logger.info(
            f"启动配置：persona={self.persona.name}, "
            f"adapter={adapter_name}, "
            f"providers={len(self.providers)}, "
            f"tools={len(self.tool_registry)}, "
            f"history={self._hist_len}条"
        )

        # ----- 9. State -----
        from .state import PendingRequestStore, RateLimiter
        from .wakeup import WakeupScheduler

        self.pending_requests = PendingRequestStore()
        if self.config.behavior.rate_limit.enabled:
            self.rate_limiter = RateLimiter(
                window_seconds=self.config.behavior.rate_limit.window_seconds,
                max_messages=self.config.behavior.rate_limit.max_messages,
                whitelist_provider=self._friend_whitelist_provider,
            )

        # ----- 10. WakeupScheduler（双向依赖：先用占位构造，pipeline 实例化后回填）-----
        async def _wakeup_placeholder(
            _reminder: str,
            _target: dict[str, Any] | None = None,
            _mode: str = "wakeup",
            _message_text: str | None = None,
        ) -> None:
            logger.warning("wakeup 触发时 pipeline 尚未就绪，跳过")

        self.wakeup_scheduler = WakeupScheduler(on_fire=_wakeup_placeholder)

        # ----- 11. Pipeline -----
        from .message_pipeline import MessagePipeline

        chat_context_length = self._model_context_length(chat_cfg.provider, chat_cfg.model)
        self.pipeline = MessagePipeline(
            adapter=self.adapter,
            chat_agent=self.chat_agent,
            persona=self.persona,
            history=self.history,
            important=self.important,
            archive=self.archive,
            rolling_summary=self.rolling_summary,
            tool_registry=self.tool_registry,
            wakeup_scheduler=self.wakeup_scheduler,
            pending_requests=self.pending_requests,
            behavior_cfg=self.config.behavior,
            features_cfg=self.config.features,
            whitelist=adapter_cfg.whitelist,
            emoji_dir=self.paths.EMOJI_DIR,
            workspace_dir=self.paths.WORKSPACE_DIR,
            rate_limiter=self.rate_limiter,
            summary_agent=self.summary_agent,
            model_context_length=chat_context_length,
            vision=self.vision,
            web_search=self.web_search,
            weather=self.weather,
            asr=self.asr,
            tts=self.tts,
            rag_memory=self.rag_memory,
        )
        # 回填 wakeup 双向依赖
        self.wakeup_scheduler._on_fire = self.pipeline.run_wakeup_turn

        # ----- 12. Notice/Request handlers -----
        from .recall_handler import RecallHandler
        from .request_handler import RequestHandler

        self.recall_handler = RecallHandler(
            pipeline=self.pipeline, behavior_cfg=self.config.behavior
        )
        self.request_handler = RequestHandler(
            pipeline=self.pipeline, pending_requests=self.pending_requests
        )

        # ----- 13. EventBus -----
        from .event_bus import EventBus

        self.event_bus = EventBus()
        self.event_bus.on_message(self.pipeline.enqueue)
        self.event_bus.on_notice(self.recall_handler.on_notice)
        self.event_bus.on_request(self.request_handler.on_request)
        self.event_bus.bind_adapter(self.adapter)

        # ----- 14. Proactive Loop -----
        from .proactive_loop import ProactiveLoop

        self.proactive_loop = ProactiveLoop(
            pipeline=self.pipeline,
            proactive_agent=self.proactive_agent,
            behavior_cfg=self.config.behavior,
        )

        # ----- 15. 启动 adapter + proactive loop -----
        stage_t0 = time.monotonic()
        await self.adapter.start()
        await self.proactive_loop.start()
        logger.debug("Runtime 阶段完成：Adapter/主动循环启动 %.2fs", time.monotonic() - stage_t0)

        logger.info("Runtime 启动完成（耗时 %.1fs）", time.monotonic() - start_t0)
        self._schedule_provider_health_check()

    def _model_context_length(self, provider_id: str, model_id: str) -> int | None:
        """从 provider preset 中读取模型上下文硬上限，找不到则返回 None。"""
        try:
            provider_cfg = self.config.providers.get(provider_id)
            preset_id = provider_cfg.preset.lower() if provider_cfg and provider_cfg.preset else ""
            preset = self.provider_registry.presets.get(preset_id) if preset_id else None
            if preset is None:
                return None
            for model in preset.models:
                if model.id == model_id:
                    return model.context_length or None
        except Exception:
            logger.debug(
                "读取模型上下文窗口失败：provider=%s model=%s",
                provider_id,
                model_id,
                exc_info=True,
            )
        return None

    # ============================================================
    # 等待停止信号（SIGINT / SIGTERM）
    # ============================================================

    async def wait_until_stop(self) -> None:
        """阻塞等待 stop 信号。"""
        loop = asyncio.get_running_loop()
        # Windows 不支持 add_signal_handler；suppress
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError, ValueError):
                loop.add_signal_handler(sig, self._stop_event.set)

        await self._stop_event.wait()

    def request_stop(self) -> None:
        """从外部触发停止。"""
        self._stop_event.set()

    # ============================================================
    # 优雅关闭（严格相反顺序）
    # ============================================================

    async def shutdown(self) -> None:
        """按相反顺序关闭所有组件，单个失败不影响其它。"""
        if self._shutdown_complete:
            return
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self.request_stop()
        logger.info("Runtime 关闭中...")

        warmup_tasks = getattr(self, "_warmup_tasks", set())
        for task in list(warmup_tasks):
            if task and not task.done():
                task.cancel()
        if warmup_tasks:
            with suppress(Exception):
                await asyncio.wait_for(
                    asyncio.gather(*list(warmup_tasks), return_exceptions=True),
                    timeout=2.0,
                )

        if self._provider_health_task is not None and not self._provider_health_task.done():
            self._provider_health_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._provider_health_task

        async def _close(label: str, coro_factory, timeout: float = 8.0) -> None:
            t0 = time.monotonic()
            try:
                await asyncio.wait_for(coro_factory(), timeout=timeout)
                logger.debug("关闭 %s 完成（%.2fs）", label, time.monotonic() - t0)
            except asyncio.TimeoutError:
                logger.warning(f"关闭 {label} 超时，跳过")
            except Exception as e:
                logger.warning(f"关闭 {label} 失败: {e}")

        if self.proactive_loop is not None:
            await _close("proactive_loop", self.proactive_loop.shutdown)
        if self.adapter is not None:
            await _close("adapter", self.adapter.stop)
        if self.recall_handler is not None:
            await _close("recall_handler", self.recall_handler.shutdown)
        if self.wakeup_scheduler is not None:
            await _close("wakeup_scheduler", self.wakeup_scheduler.cancel_all)
        if self.pipeline is not None:
            await _close("pipeline", self.pipeline.shutdown)
        if self.rag_memory is not None:
            await _close("rag_memory", self.rag_memory.shutdown)
        if self.embedding_service is not None:
            await _close("embedding_service", self.embedding_service.aclose)
        if self.asr is not None:
            await _close("asr", self.asr.aclose)
        if self.tts is not None:
            await _close("tts", self.tts.aclose)
        if self.plugin_manager is not None:
            await _close("plugin_manager", self.plugin_manager.shutdown_all)
        if self.provider_registry is not None:
            await _close("provider_registry", self.provider_registry.close_all)

        self._shutdown_complete = True
        logger.info("Runtime 已停止")

    # ============================================================
    # 辅助
    # ============================================================

    async def _friend_whitelist_provider(self) -> set[str]:
        """RateLimiter 用：返回当前好友 user_id 集合。"""
        if not self.adapter:
            return set()
        try:
            friends = await self.adapter.list_friends()
            return {f.user_id for f in friends}
        except Exception as e:
            logger.warning(f"获取好友列表失败: {e}")
            return set()

    async def _setup_rag(self, mem_dir) -> None:
        """RAG 模式装配 EmbeddingService + 会话向量检索服务。

        失败仅 warn，不阻塞主流程。RAG 模式不再复用 important.json。
        """
        ecfg = self.config.features.embedding
        if not ecfg.enabled:
            logger.warning("long_term_memory.mode=rag 但 features.embedding.enabled=False；RAG 召回不可用")
            return
        if ecfg.type == "local":
            try:
                from features.embedding import get_local_service

                model_dir = ecfg.local_model_dir
                if not model_dir:
                    if ecfg.local_quality == "quality":
                        model_dir = "data/models/embedding/bge-large-zh-v1.5"
                    else:
                        model_dir = "data/models/embedding/all-MiniLM-L6-v2"
                model_dir = self._resolve_project_path(model_dir)
                self.embedding_service = get_local_service(model_dir)
                from memory import RagMemoryService, SqliteVectorStore

                self.rag_store = SqliteVectorStore(mem_dir / "rag_memory.sqlite3")
                await self.rag_store.load()
                self.rag_memory = RagMemoryService(
                    embedding=self.embedding_service,
                    store=self.rag_store,
                    top_k=self.config.features.long_term_memory.rag_top_k,
                )
                await self.rag_memory.load()
                self.history.on_append(self.rag_memory.enqueue_records)
                archive_records = await self.archive.records()
                history_records = await self.history.records()
                self.rag_memory.schedule_bootstrap([*archive_records, *history_records])
                self._fire_warmup("embedding", self.embedding_service)
                logger.info(
                    f"RAG 已就位（本地 embedding）：quality={ecfg.local_quality}, "
                    f"索引条目={len(self.rag_store)}"
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"本地 embedding 初始化失败，fallback 到 text() 模式：{e}")
                self._disable_feature_after_failure("embedding", e)
                self.embedding_service = None
                self.rag_store = None
                self.rag_memory = None
        elif ecfg.type == "api":
            if not ecfg.provider or ecfg.provider not in self.providers:
                logger.warning(
                    f"features.embedding.provider={ecfg.provider!r} 未在 providers 中；RAG 召回跳过"
                )
                self._disable_feature_after_failure(
                    "embedding",
                    RuntimeError(f"provider={ecfg.provider!r} 未在 providers 中"),
                )
                return
            api_key = self.secrets.get(ecfg.api_key_id) if ecfg.api_key_id else None
            if ecfg.api_key_id and not api_key:
                logger.warning(f"embedding api_key_id={ecfg.api_key_id!r} 找不到密钥；RAG 召回跳过")
                self._disable_feature_after_failure(
                    "embedding",
                    RuntimeError(f"api_key_id={ecfg.api_key_id!r} 找不到密钥"),
                )
                return
            try:
                from features.embedding import OpenAICompatEmbeddingService
                provider = self.providers[ecfg.provider]
                base_url = getattr(provider, "base_url", None) or ""
                if not api_key:
                    api_key = getattr(provider, "api_key", "") or ""
                self.embedding_service = OpenAICompatEmbeddingService(
                    base_url=base_url,
                    api_key=api_key or "",
                    model=ecfg.api_model or "text-embedding-v1",
                )
                from memory import RagMemoryService, SqliteVectorStore

                self.rag_store = SqliteVectorStore(mem_dir / "rag_memory.sqlite3")
                await self.rag_store.load()
                self.rag_memory = RagMemoryService(
                    embedding=self.embedding_service,
                    store=self.rag_store,
                    top_k=self.config.features.long_term_memory.rag_top_k,
                )
                await self.rag_memory.load()
                self.history.on_append(self.rag_memory.enqueue_records)
                archive_records = await self.archive.records()
                history_records = await self.history.records()
                self.rag_memory.schedule_bootstrap([*archive_records, *history_records])
                logger.info(
                    f"RAG 已就位：provider={ecfg.provider}, model={ecfg.api_model}, "
                    f"索引条目={len(self.rag_store)}"
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"RAG 装配失败，fallback 到 text() 模式：{e}")
                self._disable_feature_after_failure("embedding", e)
                self.embedding_service = None
                self.rag_store = None
                self.rag_memory = None

    async def _setup_plugins(self) -> None:
        """扫描 plugins/ 并按 features.asr/tts 决定要不要 build。

        失败仅 warn，不阻塞主流程。
        """
        from plugins import PluginManager

        plugins_dir = self.project_root / "plugins"
        self.plugin_manager = PluginManager(plugins_dir)
        try:
            self.plugin_manager.scan()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"插件扫描失败：{e}")
            return
        records = self.plugin_manager.list_all()
        if records:
            logger.info(f"已扫描 {len(records)} 个插件：{[r.meta.name for r in records]}")

        # ASR：不再加载 Whisper/云端 ASR。NapCat 自带 fetch_ptt_text 足够覆盖 QQ 语音。
        acfg = self.config.features.asr
        if acfg.enabled:
            logger.info("ASR 本地/API 配置已忽略：使用 NapCat 内置语音转文字")
            self.asr = None

        # TTS：同理
        tcfg = self.config.features.tts
        if tcfg.enabled and tcfg.type == "local":
            plugin_name = tcfg.local_model.lower()
            if not self.plugin_manager.get(plugin_name):
                plugin_name = "voxcpm2"
            try:
                self.tts = self.plugin_manager.build(
                    plugin_name,
                    {
                        "model_dir": self._resolve_project_path(
                            tcfg.model_dir or "data/models/VoxCPM2"
                        ),
                        "reference_audio": tcfg.reference_audio,
                        "default_prompt": tcfg.default_prompt,
                        "device": tcfg.device,
                        "load_denoiser": tcfg.load_denoiser,
                        "cfg_value": tcfg.cfg_value,
                        "inference_timesteps": tcfg.inference_timesteps,
                    },
                )
                self._fire_warmup("tts", self.tts)
                logger.info(f"TTS 插件已启用：{plugin_name}（后台预热中）")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"TTS 插件 {plugin_name!r} 启用失败：{e}")
                self._disable_feature_after_failure("tts", e)
        elif tcfg.enabled and tcfg.type == "api":
            api_key = self.secrets.get(tcfg.api_key_id) if tcfg.api_key_id else None
            provider = tcfg.provider or "edge"
            if provider == "edge":
                try:
                    from features.tts import _get_edge_service

                    voice = tcfg.extra_credentials.get("voice", "") or "zh-CN-XiaoxiaoNeural"
                    self.tts = _get_edge_service()(
                        voice=voice,
                        rate=tcfg.extra_credentials.get("rate", "+0%"),
                        volume=tcfg.extra_credentials.get("volume", "+0%"),
                        pitch=tcfg.extra_credentials.get("pitch", "+0Hz"),
                        output_dir=self.paths.WORKSPACE_DIR / ".run",
                    )
                    logger.info(
                        "TTS 已启用：EdgeTTS（免费在线服务，可能因网络或服务策略失败）voice=%s",
                        voice,
                    )
                    self._fire_warmup("tts", self.tts)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"EdgeTTS 初始化失败：{e}")
                    self._disable_feature_after_failure("tts", e)
            elif provider == "xfyun":
                try:
                    from features.tts import _get_iflytek_service
                    voice = tcfg.extra_credentials.get("voice", "") or "x4_xiaoyan"
                    self.tts = _get_iflytek_service()(
                        app_id=tcfg.extra_credentials.get("app_id", ""),
                        api_key=api_key or "",
                        api_secret=tcfg.extra_credentials.get("api_secret", ""),
                        voice_name=voice,
                        speed=int(tcfg.extra_credentials.get("speed", "50") or 50),
                        volume=int(tcfg.extra_credentials.get("volume", "50") or 50),
                        pitch=int(tcfg.extra_credentials.get("pitch", "50") or 50),
                        aue=tcfg.extra_credentials.get("aue", "lame") or "lame",
                        output_dir=self.paths.WORKSPACE_DIR / ".run",
                    )
                    logger.info("TTS 已启用：讯飞云端 voice=%s", voice)
                    self._fire_warmup("tts", self.tts)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"讯飞 TTS 初始化失败：{e}")
                    self._disable_feature_after_failure("tts", e)
            elif provider in {"baidu", "volcengine"}:
                logger.warning(
                    "TTS provider=%s 已从新运行时入口移除；请改用 edge 或 xfyun",
                    provider,
                )
                self._disable_feature_after_failure(
                    "tts",
                    RuntimeError(f"TTS provider={provider!r} 已移除，请改用 edge 或 xfyun"),
                )
            else:
                logger.warning(f"TTS provider={provider!r} 未知，跳过")
                self._disable_feature_after_failure(
                    "tts", RuntimeError(f"TTS provider={provider!r} 未知")
                )

        # 本地模型在 Runtime 启动后后台预热；首次调用若未完成则等待同一个加载任务。

    def _fire_warmup(self, label: str, service: Any) -> None:
        """fire-and-forget 调 service.warmup()，加载模型到内存。

        失败仅 warn，绝不影响 Runtime 启动流程。
        task 引用保留在 self._warmup_tasks，避免被 GC。
        """
        warmup = getattr(service, "warmup", None)
        if warmup is None or not callable(warmup):
            return
        if not hasattr(self, "_warmup_tasks"):
            self._warmup_tasks: set[asyncio.Task] = set()

        async def _do() -> None:
            import time
            start = time.monotonic()
            try:
                await warmup()
                logger.info(f"{label} 预热完成（耗时 {time.monotonic() - start:.1f}s）")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{label} 预热失败：{e}")
                self._disable_feature_after_failure(label, e)

        task = asyncio.create_task(_do(), name=f"warmup-{label}")
        self._warmup_tasks.add(task)
        task.add_done_callback(self._warmup_tasks.discard)

    def _resolve_project_path(self, path: str) -> str:
        p = Path(path)
        if p.is_absolute():
            return str(p)
        return str((self.project_root / p).resolve())

    async def _check_provider_health(self) -> None:
        """启动时并发做短超时 provider 检测，供总览页/设置页显示。"""
        if not self.providers:
            return

        from providers.health import (
            ProviderHealth,
            probe_embedding_provider_instance,
            probe_provider_instance,
        )

        chat_models = self._provider_chat_model_map()
        embedding_models = self._provider_embedding_model_map()
        for name in self.providers:
            self.provider_health[name] = ProviderHealth("checking", "检测中")

        async def _one(name: str, provider: Any) -> None:
            try:
                model = chat_models.get(name, "")
                is_embedding_probe = False
                if not model and name in embedding_models:
                    model = embedding_models[name]
                    is_embedding_probe = True
                if not model:
                    self.provider_health[name] = ProviderHealth("error", "没有绑定模型")
                    return
                api_key = getattr(provider, "api_key", "") or ""
                if is_embedding_probe:
                    ecfg = self.config.features.embedding
                    if ecfg.api_key_id:
                        api_key = self.secrets.get(ecfg.api_key_id) or api_key
                if not api_key:
                    self.provider_health[name] = ProviderHealth("error", "缺 API 密钥")
                    return
                if is_embedding_probe:
                    result = await probe_embedding_provider_instance(
                        provider,
                        model=model,
                        api_key=api_key,
                        timeout_seconds=15.0,
                    )
                else:
                    protocol = self._provider_protocol(name)
                    result = await probe_provider_instance(
                        provider,
                        model=model,
                        protocol=protocol,
                        timeout_seconds=15.0,
                    )
                self.provider_health[name] = result
            except Exception as e:  # noqa: BLE001
                self.provider_health[name] = ProviderHealth("error", f"检测失败：{e}")

        await asyncio.gather(
            *(_one(name, provider) for name, provider in self.providers.items()),
            return_exceptions=True,
        )

    def _schedule_provider_health_check(self) -> None:
        """后台做 provider 健康检查，不阻塞 Runtime 启动和 Dashboard 创建。"""
        if not self.providers:
            return
        try:
            from providers.health import ProviderHealth

            for name in self.providers:
                self.provider_health[name] = ProviderHealth("checking", "检测中")
        except Exception:
            logger.debug("初始化 provider_health 状态失败", exc_info=True)
        if self._provider_health_task is not None and not self._provider_health_task.done():
            return
        self._provider_health_task = asyncio.create_task(
            self._check_provider_health(),
            name="provider-health-check",
        )
        self._provider_health_task.add_done_callback(self._on_provider_health_done)

    @staticmethod
    def _on_provider_health_done(task: asyncio.Task) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.debug("Provider 健康检查后台任务失败：%s", e, exc_info=True)

    async def _record_model_usage(self, usage: Any, metadata: dict[str, Any]) -> None:
        if self.usage_stats is None:
            return
        await self.usage_stats.record(
            usage,
            provider=str(metadata.get("provider") or ""),
            model=str(metadata.get("model") or ""),
            agent=str(metadata.get("agent") or ""),
            operation=str(metadata.get("operation") or ""),
        )

    def _update_model_activity(self, payload: dict[str, Any]) -> None:
        state = str(payload.get("state") or "idle")
        text = str(payload.get("text") or "空闲")
        self.model_activity = {
            "state": state,
            "text": text,
            "model": str(payload.get("model") or ""),
            "agent": str(payload.get("agent") or "主模型"),
            "loop": payload.get("loop"),
            "tool_names": list(payload.get("tool_names") or []),
            "finish_reason": str(payload.get("finish_reason") or ""),
            "updated_at": time.time(),
        }

    def _provider_chat_model_map(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for _name, agent in self.config._iter_agents():
            result.setdefault(agent.provider, agent.model)
        vision = self.config.features.vision
        if vision.enabled and vision.type == "api" and vision.provider and vision.model:
            result.setdefault(vision.provider, vision.model)
        return result

    def _provider_embedding_model_map(self) -> dict[str, str]:
        result: dict[str, str] = {}
        embedding = self.config.features.embedding
        if (
            self.config.features.long_term_memory.mode == "rag"
            and embedding.enabled
            and embedding.type == "api"
            and embedding.provider
            and embedding.api_model
        ):
            result.setdefault(embedding.provider, embedding.api_model)
        return result

    def _apply_feature_provider_overrides(self) -> None:
        """把 feature 独立密钥/地址同步到 provider 构造配置。

        设置页允许在 Vision/Embedding 功能里单独填密钥。Provider 实例构造发生在
        Runtime 启动早期，因此要在 build_from_config 前把这些覆盖项合入对应 provider。
        """
        vision = self.config.features.vision
        if (
            vision.enabled
            and vision.type == "api"
            and vision.provider
            and vision.provider in self.config.providers
        ):
            pcfg = self.config.providers[vision.provider]
            if vision.api_key_id:
                pcfg.api_key_id = vision.api_key_id
            if vision.base_url:
                pcfg.base_url = vision.base_url

        embedding = self.config.features.embedding
        if (
            self.config.features.long_term_memory.mode == "rag"
            and embedding.enabled
            and embedding.type == "api"
            and embedding.provider
            and embedding.provider.startswith("embedding_")
            and embedding.provider in self.config.providers
            and embedding.api_key_id
        ):
            self.config.providers[embedding.provider].api_key_id = embedding.api_key_id

    def _provider_protocol(self, name: str) -> str:
        cfg = self.config.providers.get(name)
        if cfg is None:
            return "openai_compat"
        if cfg.protocol:
            return cfg.protocol
        preset_name = (cfg.preset or "").lower()
        preset = getattr(self.provider_registry, "presets", {}).get(preset_name)
        return getattr(preset, "protocol", "openai_compat")

    def _disable_feature_after_failure(self, label: str, exc: BaseException) -> None:
        """本地/云端能力加载失败后禁用配置，避免下次启动继续卡住。"""
        if label not in {"asr", "tts", "embedding"}:
            return
        message = str(exc)
        self.feature_failures[label] = message
        try:
            if label == "embedding":
                self.config.features.embedding.enabled = False
                self.config.features.long_term_memory.mode = "file"
                self.embedding_service = None
                self.rag_store = None
                self.rag_memory = None
                if self.important is not None:
                    self.important._embedding = None
                    self.important._rag_store = None
            else:
                getattr(self.config.features, label).enabled = False
                setattr(self, label, None)
                if self.pipeline is not None:
                    setattr(self.pipeline, label, None)
            from app_config.loader import save_config

            save_config(self.paths, self.config)
        except Exception as save_exc:  # noqa: BLE001
            logger.warning(f"{label} 加载失败后禁用配置未能保存：{save_exc}")
