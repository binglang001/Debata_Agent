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
import signal
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

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self._stop_event = asyncio.Event()

        # 组件占位（在 start() 中实例化）
        self.config: Any = None
        self.secrets: Any = None
        self.paths: Any = None
        self.persona: Any = None
        self.history: Any = None
        self.important: Any = None
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

    # ============================================================
    # 启动流程
    # ============================================================

    async def start(self) -> None:
        """按顺序装配并启动所有组件。"""
        logger.info("Runtime 启动中...")

        # ----- 1. 路径与配置 -----
        from app_config import AppPaths, SecretsManager, load_config

        self.paths = AppPaths(project_root=self.project_root)
        self.paths.ensure_data_dirs()
        self.secrets = SecretsManager(self.paths)
        self.secrets.initialize()
        self.config = load_config(self.paths)
        # 按 config 调整全局日志级别（main.py 启动时只设了 INFO）
        try:
            logging.getLogger().setLevel(
                getattr(logging, self.config.app.log_level, logging.INFO)
            )
        except Exception:
            pass
        logger.debug(f"配置已加载（persona={self.config.persona.active}）")

        # ----- 2. 人格 -----
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

        # ----- 3. 记忆 -----
        from memory import HistoryManager, ImportantMemoryManager

        mem_dir = self.paths.memory_dir_for(self.persona.name)
        mem_dir.mkdir(parents=True, exist_ok=True)
        self.history = HistoryManager(mem_dir / "history.jsonl")
        self.important = ImportantMemoryManager(mem_dir / "important.json")
        await self.history.load()
        await self.important.load()
        self._hist_len = await self.history.length()
        logger.debug(
            f"记忆已加载（history={self._hist_len} 条，important={len(self.important.items())} 条）"
        )

        # ----- 4. Providers（用 ProviderRegistry 统一管理便于 close_all）-----
        from providers import ProviderRegistry

        self.provider_registry = ProviderRegistry()
        self.provider_registry.load_presets(self.paths.PROVIDER_PRESETS_DIR)
        self.provider_registry.build_from_config(self.config.providers, self.secrets)
        self.providers = {
            name: self.provider_registry.get(name)
            for name in self.provider_registry.list_names()
        }
        logger.debug(f"Provider 已实例化: {list(self.providers.keys())}")

        # ----- 5. Agents -----
        from agents import ChatAgent, ProactiveRouterAgent, SummaryAgent

        chat_cfg = self.config.agents.chat
        if chat_cfg.provider not in self.providers:
            raise RuntimeError(
                f"agents.chat.provider={chat_cfg.provider!r} 不在 providers 中。"
                f"已实例化: {list(self.providers.keys())}"
            )
        self.chat_agent = ChatAgent(self.providers[chat_cfg.provider], chat_cfg)

        if self.config.agents.proactive is not None:
            pcfg = self.config.agents.proactive
            if pcfg.provider not in self.providers:
                raise RuntimeError(
                    f"agents.proactive.provider={pcfg.provider!r} 不在 providers 中"
                )
            self.proactive_agent = ProactiveRouterAgent(
                self.providers[pcfg.provider], pcfg
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
            )

        # ----- 6. Features service（按 enabled 实例化；asr/tts/embedding 是 P3 占位）-----
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

        # ----- 7. Adapter -----
        from adapters.napcat.adapter import NapCatAdapter

        if not self.config.adapters:
            raise RuntimeError("配置中没有任何 adapter")
        adapter_name, adapter_cfg = next(iter(self.config.adapters.items()))
        self.adapter = NapCatAdapter.from_config(adapter_name, adapter_cfg, self.secrets)
        logger.debug(f"Adapter 已实例化: {adapter_name}")

        # ----- 8. Tools -----
        from tools import build_default_registry

        self.tool_registry = build_default_registry(self.config)

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
        async def _wakeup_placeholder(_reminder: str) -> None:
            logger.warning("wakeup 触发时 pipeline 尚未就绪，跳过")

        self.wakeup_scheduler = WakeupScheduler(on_fire=_wakeup_placeholder)

        # ----- 11. Pipeline -----
        from .message_pipeline import MessagePipeline

        self.pipeline = MessagePipeline(
            adapter=self.adapter,
            chat_agent=self.chat_agent,
            persona=self.persona,
            history=self.history,
            important=self.important,
            tool_registry=self.tool_registry,
            wakeup_scheduler=self.wakeup_scheduler,
            pending_requests=self.pending_requests,
            behavior_cfg=self.config.behavior,
            features_cfg=self.config.features,
            whitelist=adapter_cfg.whitelist,
            emoji_dir=self.paths.EMOJI_DIR,
            upload_allowed_dir=self.paths.UPLOADS_DIR,
            rate_limiter=self.rate_limiter,
            summary_agent=self.summary_agent,
            vision=self.vision,
            web_search=self.web_search,
            weather=self.weather,
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
        await self.adapter.start()
        await self.proactive_loop.start()

        logger.info("Runtime 启动完成")

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
        logger.info("Runtime 关闭中...")

        async def _close(label: str, coro_factory) -> None:
            try:
                await coro_factory()
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
        if self.provider_registry is not None:
            await _close("provider_registry", self.provider_registry.close_all)

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
