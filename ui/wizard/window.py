"""向导主窗口 —— 顶部进度、主体 step 视图、底部按钮。

按 flow.py 的 step 流转：
    welcome → main_model_(quick|custom) → [other_agents] → features
    → [embedding] → adapter → persona → [persona_create] → summary

向导完成时把 WizardContext 转成 RootConfig + 写 SecretsManager + 保存 config.yaml，
然后 emit completed 信号让 main.py 决定下一步（启动 Runtime 或退出）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app_config import AppPaths, SecretsManager
from app_config.loader import save_config
from app_config.schema import (
    AgentConfig,
    AgentsConfig,
    ASRFeatureConfig,
    BehaviorConfig,
    EmbeddingFeatureConfig,
    FeaturesConfig,
    LongTermMemoryConfig,
    NapCatAdapterConfig,
    PersonaConfig,
    ProviderConfig,
    ReasoningConfig,
    RootConfig,
    TTSFeatureConfig,
    VisionFeatureConfig,
    WeatherFeatureConfig,
    WebSearchFeatureConfig,
    WhitelistConfig,
)

from ..theme import Spacing
from .components import WhitelistState
from .context import WizardContext
from .copy import COPY
from .flow import (
    WIZARD_PATH_CUSTOM,
    WIZARD_PATH_RECOMMENDED,
    is_last_step,
    next_step,
    prev_step,
    progress,
)
from .persona_creator import PersonaCreatorStepView
from .step_views import (
    AdapterStepView,
    EmbeddingStepView,
    FeaturesStepView,
    MainModelCustomStepView,
    MainModelQuickStepView,
    OtherAgentsStepView,
    PersonaStepView,
    SummaryStepView,
    WelcomeStepView,
)
from .steps import STEPS, StepId

logger = logging.getLogger(__name__)


class WizardWindow(QMainWindow):
    """向导主窗口。配置写入完成时 emit completed。"""

    completed = Signal()
    """配置已写入磁盘 + 密钥已存入 secrets。main.py 接此信号决定下一步。"""

    cancelled = Signal()
    """用户关闭窗口未完成。"""

    def __init__(
        self,
        paths: AppPaths,
        secrets: SecretsManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Debata_Agent · 首次配置")
        self.setMinimumSize(960, 720)
        self.resize(1080, 800)

        self._paths = paths
        self._secrets = secrets
        self._context = WizardContext()
        self._current: StepId = StepId.WELCOME
        self._completed_emitted = False
        self._navigating = False

        # 无边框 + 自定义标题栏 + 透明 root 让 WindowFrame 的圆角 QSS 生效
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setProperty("frameless", True)

        # 整窗用 WindowFrame 包一层：QSS 里 QFrame#WindowFrame 设了 border-radius
        root = QFrame()
        root.setObjectName("WindowFrame")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # 顶部：进度条 + 步骤标题
        topbar = self._build_topbar()
        root_layout.addWidget(topbar)

        # 中间：step 视图栈包 ScrollArea，仅在当前 step 溢出时滚动
        from PySide6.QtWidgets import QScrollArea
        self._active_view: QWidget | None = None
        self._page_host = QWidget()
        self._page_lay = QVBoxLayout(self._page_host)
        self._page_lay.setContentsMargins(0, 0, 0, 0)
        self._page_lay.setSpacing(0)
        self._page_lay.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._wizard_scroll = QScrollArea()
        self._wizard_scroll.setWidgetResizable(True)
        self._wizard_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._wizard_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._wizard_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._wizard_scroll.setWidget(self._page_host)

        wrap = QWidget()
        wrap_lay = QVBoxLayout(wrap)
        wrap_lay.setContentsMargins(Spacing.XL, Spacing.LG, Spacing.XL, Spacing.LG)
        wrap_lay.setSpacing(0)
        wrap_lay.addWidget(self._wizard_scroll, 1)
        root_layout.addWidget(wrap, 1)

        # 底部：按钮区
        bottom = self._build_bottom_bar()
        root_layout.addWidget(bottom)

        self.setCentralWidget(root)
        from ..widgets import attach_size_grip, install_window_drag, install_window_resize

        self._size_grip = attach_size_grip(self)
        self._window_resize_filter = install_window_resize(root, self)
        self._window_drag_filter = install_window_drag(root, self)

        # 实例化所有 view
        self._views: dict[StepId, QWidget] = {
            StepId.WELCOME: WelcomeStepView(self._context),
            StepId.MAIN_MODEL_QUICK: MainModelQuickStepView(self._context),
            StepId.MAIN_MODEL_CUSTOM: MainModelCustomStepView(self._context),
            StepId.OTHER_AGENTS: OtherAgentsStepView(self._context),
            StepId.FEATURES: FeaturesStepView(self._context),
            StepId.EMBEDDING: EmbeddingStepView(self._context),
            StepId.ADAPTER: AdapterStepView(self._context),
            StepId.PERSONA: PersonaStepView(self._context),
            StepId.PERSONA_CREATE: PersonaCreatorStepView(self._context),
            StepId.SUMMARY: SummaryStepView(self._context),
        }
        for v in self._views.values():
            if hasattr(v, "invalid_input"):
                v.invalid_input.connect(self._show_error)
            if hasattr(v, "request_advance"):
                v.request_advance.connect(self._on_next)

        self._jump_to(StepId.WELCOME)

    # ============================================================
    # UI 构造
    # ============================================================

    def _build_topbar(self) -> QWidget:
        from ..widgets import DragBar, make_window_controls

        bar = DragBar(self)
        bar.setObjectName("Topbar")
        bar.setMinimumHeight(76)
        bar.setMaximumHeight(76)

        layout = QVBoxLayout(bar)
        layout.setContentsMargins(Spacing.XL, Spacing.SM, 0, Spacing.SM)
        layout.setSpacing(Spacing.XS)

        head = QHBoxLayout()
        head.setSpacing(Spacing.MD)
        self._title_label = QLabel("")
        self._title_label.setProperty("role", "title-3")
        head.addWidget(self._title_label)
        head.addStretch(1)
        self._step_indicator = QLabel("")
        self._step_indicator.setProperty("role", "secondary")
        head.addWidget(self._step_indicator)

        # 窗口控制按钮（最小化 / 最大化 / 关闭）
        head.addSpacing(Spacing.MD)
        head.addWidget(make_window_controls(self))
        layout.addLayout(head)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(4)
        self._progress.setContentsMargins(0, 0, Spacing.XL, 0)
        layout.addWidget(self._progress)
        layout.setContentsMargins(Spacing.XL, Spacing.SM, Spacing.XL, Spacing.SM)

        return bar

    def _build_bottom_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("Topbar")  # 复用同样的边框样式
        bar.setMinimumHeight(80)
        bar.setMaximumHeight(80)

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(Spacing.XL, Spacing.SM, Spacing.XL, Spacing.LG)
        layout.setSpacing(Spacing.MD)

        self._back_btn = QPushButton(COPY["button.back"])
        self._back_btn.setProperty("role", "secondary")
        self._back_btn.clicked.connect(self._on_back)
        layout.addWidget(self._back_btn)

        self._skip_btn = QPushButton(COPY["button.skip"])
        self._skip_btn.setProperty("role", "text")
        self._skip_btn.clicked.connect(self._on_skip)
        layout.addWidget(self._skip_btn)

        layout.addStretch(1)

        self._next_btn = QPushButton(COPY["button.next"])
        self._next_btn.setProperty("role", "primary")
        self._next_btn.clicked.connect(self._on_next)
        layout.addWidget(self._next_btn)

        return bar

    # ============================================================
    # 流转
    # ============================================================

    def _jump_to(self, step: StepId) -> None:
        if step not in self._views:
            return
        self._current = step
        view = self._views[step]
        if self._active_view is not view:
            if self._active_view is not None:
                self._page_lay.removeWidget(self._active_view)
                self._active_view.hide()
                self._active_view.setParent(None)
            self._page_lay.addWidget(view)
            self._active_view = view
            view.show()
            self._animate_view(view)
        self._wizard_scroll.verticalScrollBar().setValue(0)  # 切步回顶
        if hasattr(view, "refresh"):
            view.refresh()
        self._update_topbar()
        self._update_buttons()
        self._sync_scroll_height()

    def _sync_scroll_height(self) -> None:
        view = self._active_view
        if view is not None:
            view.updateGeometry()
            QTimer.singleShot(0, view.updateGeometry)
        self._wizard_scroll.widget().adjustSize()

    def _update_topbar(self) -> None:
        s = STEPS.get(self._current)
        title = s.title if s else ""
        self._title_label.setText(title)
        idx, total = progress(
            self._current,
            self._context.path,
            self._context_as_dict(),
        )
        self._step_indicator.setText(f"{idx} / {total}")
        if total > 0:
            self._progress.setValue(int(idx * 100 / total))

    def _update_buttons(self) -> None:
        is_first = self._current == StepId.WELCOME
        is_last = is_last_step(self._current)
        is_skip = self._current == StepId.OTHER_AGENTS  # 仅 OTHER_AGENTS 可跳

        self._back_btn.setEnabled(not is_first)
        self._skip_btn.setVisible(is_skip)
        if is_last:
            self._next_btn.setText(COPY["button.finish"])
        elif self._current == StepId.WELCOME:
            # Welcome 自带"就走这条"按钮触发 advance；底部 next 也可用
            self._next_btn.setText(COPY["button.next"])
        else:
            self._next_btn.setText(COPY["button.next"])

    def _context_as_dict(self) -> dict:
        return {
            "long_term_memory_mode": self._context.long_term_memory_mode,
            "persona_source": self._context.persona.source,
        }

    def _on_back(self) -> None:
        prev = prev_step(self._current, self._context.path, self._context_as_dict())
        if prev is None:
            return
        self._jump_to(prev)

    def _on_skip(self) -> None:
        # 跳过当前步：直接拿 next
        nxt = next_step(self._current, self._context.path, self._context_as_dict())
        if nxt is None:
            self._finish()
        else:
            self._jump_to(nxt)

    def _on_next(self) -> None:
        if self._navigating:
            return
        view = self._views.get(self._current)
        if view is None:
            return
        if hasattr(view, "save") and not view.save():
            return
        validate = getattr(view, "validate_before_next", None)
        if validate is not None and callable(validate):
            self._navigating = True
            self._next_btn.setEnabled(False)

            async def _validate_and_continue() -> None:
                try:
                    ok = await validate()
                except Exception as e:  # noqa: BLE001
                    logger.exception("向导切页校验失败")
                    self._show_error(str(e))
                    ok = False
                finally:
                    self._navigating = False
                    self._next_btn.setEnabled(True)
                if ok:
                    self._advance_after_save()

            try:
                asyncio.get_event_loop().create_task(_validate_and_continue())
            except RuntimeError:
                self._navigating = False
                self._next_btn.setEnabled(True)
                self._show_error("事件循环未就绪")
            return

        self._advance_after_save()

    def _advance_after_save(self) -> None:
        if is_last_step(self._current):
            self._finish()
            return
        nxt = next_step(self._current, self._context.path, self._context_as_dict())
        if nxt is None:
            self._finish()
        else:
            self._jump_to(nxt)

    def _show_error(self, msg: str) -> None:
        from ..widgets import show_message

        show_message(self, "稍等一下", msg)

    # ============================================================
    # 完成 —— 写配置 + 密钥
    # ============================================================

    def _finish(self) -> None:
        try:
            self._persist()
        except Exception as e:  # noqa: BLE001
            logger.exception("向导写入配置失败")
            from ..widgets import show_message

            show_message(
                self,
                "写入配置时出错",
                f"{e}\n\n配置未保存。可以再试一次，或者关闭后用命令行配置。",
                is_danger=True,
            )
            return

        self._completed_emitted = True
        # 不弹 messagebox —— 直接触发 dashboard 接力启动
        self.completed.emit()
        self.close()

    def _persist(self) -> None:
        """把 WizardContext 转成 config + secrets 写入。"""
        c = self._context

        # 主模型 key
        main_key_id = f"{c.main.preset}_main"
        if c.main.api_key:
            self._secrets.set(main_key_id, c.main.api_key)

        proactive_key_id = None
        if c.path == WIZARD_PATH_CUSTOM and c.proactive.enabled and not c.proactive.use_main:
            proactive_key_id = f"{c.proactive.preset}_proactive"
            if c.proactive.api_key:
                self._secrets.set(proactive_key_id, c.proactive.api_key)

        summary_key_id = None
        if c.path == WIZARD_PATH_CUSTOM and c.summary.enabled and not c.summary.use_main:
            summary_key_id = f"{c.summary.preset}_summary"
            if c.summary.api_key:
                self._secrets.set(summary_key_id, c.summary.api_key)

        # vision 密钥：仅在 preset != "main" 时单独存
        vision_key_id = None
        vision_preset = (c.vision.extra or {}).get("preset", "volcengine") if c.vision.enabled else ""
        if c.vision.enabled and vision_preset != "main" and c.vision.api_key:
            vision_key_id = f"vision_{vision_preset}"
            self._secrets.set(vision_key_id, c.vision.api_key)

        weather_key_id = None
        if c.weather.enabled and c.weather.api_key:
            weather_key_id = "qweather"
            self._secrets.set(weather_key_id, c.weather.api_key)

        embedding_key_id = None
        if c.long_term_memory_mode == "rag" and c.embedding_type == "api" and c.embedding_api_key:
            embedding_key_id = (
                c.embedding_provider
                if c.embedding_provider.startswith("embedding_")
                else f"embedding_{c.embedding_provider}"
            )
            self._secrets.set(embedding_key_id, c.embedding_api_key)

        asr_key_id = None
        if c.asr.enabled:
            aextra = c.asr.extra or {}
            if aextra.get("type") == "api" and aextra.get("api_key"):
                asr_provider = aextra.get("provider") or "api"
                asr_key_id = f"asr_{asr_provider}"
                self._secrets.set(asr_key_id, aextra["api_key"])

        tts_key_id = None
        if c.tts.enabled:
            textra = c.tts.extra or {}
            if textra.get("type") == "api" and textra.get("api_key"):
                tts_provider = textra.get("provider") or "api"
                tts_key_id = f"tts_{tts_provider}"
                self._secrets.set(tts_key_id, textra["api_key"])

        napcat_token_id = None
        if c.adapter.token:
            napcat_token_id = "napcat_default_token"
            self._secrets.set(napcat_token_id, c.adapter.token)

        # 2. providers ——
        providers: dict[str, ProviderConfig] = {}
        main_provider_id = f"{c.main.preset}_main"
        providers[main_provider_id] = self._make_provider(c.main, main_key_id)

        if proactive_key_id:
            providers[f"{c.proactive.preset}_proactive"] = ProviderConfig(
                preset=c.proactive.preset,
                api_key_id=proactive_key_id,
            )

        if summary_key_id:
            providers[f"{c.summary.preset}_summary"] = ProviderConfig(
                preset=c.summary.preset,
                api_key_id=summary_key_id,
            )

        # vision feature 若启用，需要一个 provider
        # - extra.preset == "main"：复用主模型 provider（不新建）
        # - 其它：新建一个独立的 vision provider
        vision_provider_id = main_provider_id
        if c.vision.enabled:
            vextra = c.vision.extra or {}
            vpreset = vextra.get("preset", "volcengine")
            if vpreset == "main":
                vision_provider_id = main_provider_id
            else:
                vision_provider_id = f"vision_{vpreset}"
                if vpreset == "custom":
                    providers[vision_provider_id] = ProviderConfig(
                        display_name=f"Vision · {vpreset}",
                        protocol="openai_compat",
                        base_url=vextra.get("base_url", ""),
                        api_key_id=vision_key_id,
                    )
                else:
                    # 用 preset
                    proto = "anthropic" if vpreset == "anthropic" else "openai_compat"
                    providers[vision_provider_id] = ProviderConfig(
                        preset=vpreset,
                        display_name=f"Vision · {vpreset}",
                        protocol=proto,
                        api_key_id=vision_key_id,
                    )

        if (
            c.long_term_memory_mode == "rag"
            and c.embedding_type == "api"
            and c.embedding_provider_preset
        ):
            epreset = c.embedding_provider_preset
            if epreset == "custom":
                providers[c.embedding_provider] = ProviderConfig(
                    display_name=c.embedding_provider_display_name or "Embedding",
                    protocol=c.embedding_provider_protocol,
                    base_url=c.embedding_provider_base_url,
                    api_key_id=embedding_key_id,
                )
            else:
                providers[c.embedding_provider] = ProviderConfig(
                    preset=epreset,
                    display_name=c.embedding_provider_display_name or f"Embedding · {epreset}",
                    api_key_id=embedding_key_id,
                )

        # 3. agents ——
        chat_cfg = self._make_agent_cfg(
            provider_id=main_provider_id,
            model=c.main.model,
            temperature=c.main.temperature,
            top_p=c.main.top_p,
            max_tokens=c.main.max_tokens,
            reasoning=c.main.reasoning_enabled,
            reasoning_budget=c.main.reasoning_budget,
            reasoning_max_tokens=c.main.reasoning_max_tokens,
        )

        proactive_cfg = None
        if c.proactive.enabled:
            if c.proactive.use_main or c.path == WIZARD_PATH_RECOMMENDED:
                proactive_cfg = self._make_agent_cfg(
                    provider_id=main_provider_id,
                    model=c.main.model,
                    temperature=0.3,
                    max_tokens=64,
                    reasoning=c.proactive.reasoning_enabled,
                    reasoning_budget=c.proactive.reasoning_budget,
                )
            else:
                proactive_cfg = self._make_agent_cfg(
                    provider_id=f"{c.proactive.preset}_proactive",
                    model=c.proactive.model,
                    temperature=0.3,
                    max_tokens=64,
                    reasoning=c.proactive.reasoning_enabled,
                    reasoning_budget=c.proactive.reasoning_budget,
                )

        summary_cfg = None
        if c.summary.enabled:
            if c.summary.use_main or c.path == WIZARD_PATH_RECOMMENDED:
                summary_cfg = self._make_agent_cfg(
                    provider_id=main_provider_id,
                    model=c.main.model,
                    temperature=0.2,
                    max_tokens=8192,
                    reasoning=c.summary.reasoning_enabled,
                    reasoning_budget=c.summary.reasoning_budget,
                )
            else:
                summary_cfg = self._make_agent_cfg(
                    provider_id=f"{c.summary.preset}_summary",
                    model=c.summary.model,
                    temperature=0.2,
                    max_tokens=8192,
                    reasoning=c.summary.reasoning_enabled,
                    reasoning_budget=c.summary.reasoning_budget,
                )

        agents_cfg = AgentsConfig(
            chat=chat_cfg,
            proactive=proactive_cfg,
            summary=summary_cfg,
        )

        # 4. features ——
        features = FeaturesConfig()
        if c.vision.enabled:
            vextra = c.vision.extra or {}
            features.vision = VisionFeatureConfig(
                enabled=True,
                type="api",
                provider=vision_provider_id,
                model=vextra.get("model", ""),
            )
        if c.weather.enabled:
            wextra = c.weather.extra or {}
            weather_host = wextra.get("host", "").strip() or "devapi.qweather.com"
            features.weather = WeatherFeatureConfig(
                enabled=True,
                api_key_id=weather_key_id,
                host=weather_host,
            )
        features.web_search = WebSearchFeatureConfig(enabled=c.web_search.enabled)
        features.long_term_memory = LongTermMemoryConfig(
            mode=c.long_term_memory_mode,
            keyword_trigger_save=c.long_term_memory_keyword_trigger_save,
        )
        if c.long_term_memory_mode == "rag":
            features.embedding = EmbeddingFeatureConfig(
                enabled=True,
                type=c.embedding_type,
                provider=c.embedding_provider if c.embedding_type == "api" else None,
                api_key_id=embedding_key_id,
                api_model=c.embedding_model,
                local_quality=c.embedding_local_quality,
                local_model_dir=c.embedding_local_model_dir,
            )

        # ASR
        if c.asr.enabled:
            aextra = c.asr.extra or {}
            features.asr = ASRFeatureConfig(
                enabled=True,
                type=aextra.get("type", "local"),
                local_model=aextra.get("local_model", "large-v3"),
                provider=aextra.get("provider") or None,
                api_key_id=asr_key_id,
                extra_credentials=aextra.get("extra_credentials", {}),
                device=aextra.get("device", "auto"),  # type: ignore[arg-type]
                language=aextra.get("language", "zh"),
                model_dir=aextra.get("model_dir", ""),
            )

        # TTS
        if c.tts.enabled:
            textra = c.tts.extra or {}
            features.tts = TTSFeatureConfig(
                enabled=True,
                type=textra.get("type", "local"),
                local_model=textra.get("local_model", "voxcpm2"),
                provider=textra.get("provider") or None,
                api_key_id=tts_key_id,
                extra_credentials=textra.get("extra_credentials", {}),
                reference_audio=textra.get("reference_audio", ""),
                default_prompt=textra.get("default_prompt", ""),
                model_dir=textra.get("model_dir", "") or "data/models/VoxCPM2",
                device=textra.get("device", "auto"),  # type: ignore[arg-type]
                load_denoiser=bool(textra.get("load_denoiser", False)),
                cfg_value=float(textra.get("cfg_value", 2.0)),
                inference_timesteps=int(textra.get("inference_timesteps", 10)),
            )

        # 5. adapter ——
        napcat_cfg = NapCatAdapterConfig(
            type="napcat",
            enabled=True,
            mode=c.adapter.mode,
            host=c.adapter.host,
            port=c.adapter.port,
            path=c.adapter.path,
            access_token_id=napcat_token_id,
            manage_process=c.adapter.manage_process,
            process_path=c.adapter.process_path,
            whitelist=self._make_whitelist(c.adapter.whitelist),
        )

        # 6. persona ——
        persona_cfg, persona_warning = self._save_persona_if_needed()

        # 7. root ——
        root = RootConfig(
            version=2,
            adapters={"default": napcat_cfg},
            providers=providers,
            agents=agents_cfg,
            features=features,
            persona=persona_cfg,
            behavior=BehaviorConfig(),
        )

        save_config(self._paths, root)
        logger.info(f"向导写入完成：config.yaml + secrets.enc（persona={persona_cfg.active}）")

    # ---- 辅助 ----

    def _make_provider(self, m, api_key_id: str) -> ProviderConfig:
        if m.preset == "custom":
            return ProviderConfig(
                display_name=m.display_name,
                protocol=m.protocol,
                base_url=m.base_url,
                api_key_id=api_key_id,
            )
        return ProviderConfig(
            preset=m.preset,
            display_name=m.display_name,
            api_key_id=api_key_id,
        )

    def _make_agent_cfg(
        self,
        provider_id: str,
        model: str,
        temperature: float = 0.6,
        top_p: float = 1.0,
        max_tokens: int = 16384,
        reasoning: bool = False,
        reasoning_budget: str | None = None,
        reasoning_max_tokens: int | None = None,
    ) -> AgentConfig:
        rcfg = None
        if reasoning:
            rcfg = ReasoningConfig(
                enabled=True,
                budget=reasoning_budget,
                max_tokens=reasoning_max_tokens,
            )
        return AgentConfig(
            provider=provider_id,
            model=model,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            reasoning=rcfg,
        )

    def _make_whitelist(self, state: WhitelistState) -> WhitelistConfig:
        return WhitelistConfig(
            mode=state.mode,
            qq_ids=[int(x) for x in state.qq_ids if x.isdigit()],
            group_ids=[int(x) for x in state.group_ids if x.isdigit()],
        )

    def _save_persona_if_needed(self) -> tuple[PersonaConfig, str]:
        """create 模式下把 generated_xml 写到 personas/{name}/persona_prompt.py。"""
        p = self._context.persona
        warning = ""
        if p.source == "create" and p.generated_xml:
            from agents.persona_gen_agent import PersonaGenResult, render_persona_file
            from agents.persona_import import PersonaImportError
            from agents.persona_loader import validate_persona_name

            validate_persona_name(p.active)
            target = self._paths.PERSONAS_DIR / p.active
            if target.exists():
                raise PersonaImportError(f"角色「{p.active}」已存在，请换一个名字")
            target.mkdir(parents=True, exist_ok=True)
            (target / "__init__.py").touch(exist_ok=True)
            result = PersonaGenResult(
                persona_prompt=p.generated_xml,
                display_name=p.active,
            )
            brief = p.brief
            admins = _admin_entries(self._context.admin_qq, self._context.admin_name)
            file_text = (
                render_persona_file(result, brief, admins=admins)
                if brief
                else _render_minimal_persona(p.active, p.generated_xml, admins=admins)
            )
            (target / "persona_prompt.py").write_text(file_text, encoding="utf-8")
        elif p.source == "import" and p.import_path:
            # 把导入目录的内容复制到 personas/ 下
            from agents.persona_import import copy_persona_dir

            src = Path(p.import_path)
            p.active = copy_persona_dir(src, self._paths.PERSONAS_DIR)
        # builtin 模式无操作

        return PersonaConfig(active=p.active or "debata"), warning

    # ============================================================
    # 圆角 mask（frameless 窗口 OS 层面真圆角）
    # ============================================================

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        from ..widgets import apply_rounded_mask, position_size_grip
        apply_rounded_mask(self, radius=12)
        position_size_grip(self, getattr(self, "_size_grip", None))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        from ..widgets import fade_in_window

        fade_in_window(self)

    def nativeEvent(self, eventType, message):  # type: ignore[override]
        from ..widgets import native_resize_hit_test

        hit = native_resize_hit_test(self, eventType, message)
        if hit is not None:
            return hit
        return super().nativeEvent(eventType, message)

    def _animate_view(self, view: QWidget) -> None:
        try:
            from PySide6.QtCore import QEasingCurve, QPropertyAnimation
            from PySide6.QtWidgets import QGraphicsOpacityEffect

            effect = QGraphicsOpacityEffect(view)
            view.setGraphicsEffect(effect)
            anim = QPropertyAnimation(effect, b"opacity", view)
            anim.setDuration(120)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            view._wizard_fade_animation = anim  # type: ignore[attr-defined]
            anim.finished.connect(lambda: view.setGraphicsEffect(None))
            anim.start()
        except Exception:
            pass

    # ============================================================
    # 关闭
    # ============================================================

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._completed_emitted:
            event.accept()
            return
        from ..widgets import show_message

        if show_message(
            self,
            COPY["quit.title"],
            COPY["quit.body"],
            confirm_text="不配了",
            cancel_text="先放着",
            is_danger=True,
        ):
            self.cancelled.emit()
            event.accept()
        else:
            event.ignore()


def _admin_entries(admin_qq: str, admin_name: str) -> list[dict[str, object]]:
    if not admin_qq:
        return []
    entry: dict[str, object] = {"qq": int(admin_qq), "role": "owner"}
    if admin_name:
        entry["name"] = admin_name
    return [entry]


def _render_minimal_persona(name: str, xml: str, admins: list[dict[str, object]] | None = None) -> str:
    import json

    safe = xml.replace("'''", "\\'\\'\\'")
    admins_text = json.dumps(admins or [], ensure_ascii=False, indent=4)
    admins_text = "\n".join("    " + line for line in admins_text.splitlines())
    return (
        '"""自动生成的人格档案。"""\n\n'
        "PERSONA_PROMPT = '''\n"
        f"{safe}\n"
        "'''\n\n"
        "PERSONA_VARS = {\n"
        f"    \"name\": \"{name}\",\n"
        f"    \"admins\": {admins_text},\n"
        "}\n"
    )


__all__ = ["WizardWindow"]
