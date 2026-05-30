"""记忆方式配置：文件模式或 RAG 向量检索。"""

from __future__ import annotations

import asyncio

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ..components import ApiKeyInput, SectionCard, open_feature_guide
from ..context import BaseStepView, WizardContext
from ...theme import Spacing
from .features import (
    _directory_has_files,
    _open_directory,
    _path_picker_row,
    _prompt_download_model,
    _set_form_field_visible,
    _start_plugin_download,
)
from .main_model_custom import _PRESET_DEFAULTS


_EMBEDDING_PRESETS: dict[str, dict[str, str]] = {
    "volcengine": {
        "display": "火山方舟 · 独立 Embedding",
        "model": "doubao-embedding-text-240715",
    },
    "qwen": {
        "display": "通义千问 · 独立 Embedding",
        "model": "text-embedding-v4",
    },
    "openai": {
        "display": "OpenAI · 独立 Embedding",
        "model": "text-embedding-3-small",
    },
    "siliconflow": {
        "display": "SiliconFlow · 独立 Embedding",
        "model": "BAAI/bge-m3",
    },
    "glm": {
        "display": "智谱 · 独立 Embedding",
        "model": "embedding-3",
    },
    "custom": {
        "display": "自行填一个 Embedding 服务",
        "model": "",
    },
}


class EmbeddingStepView(BaseStepView):
    """长期记忆模式 + RAG embedding 配置。"""

    def __init__(self, context: WizardContext, parent=None) -> None:
        super().__init__(context, parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        card = SectionCard(
            title="记忆方式",
            subtitle=(
                "文件模式更轻，RAG 模式会用 embedding 模型做语义检索。"
                "本页完成后再进入渠道配置。"
            ),
            compact=True,
        )
        outer.addWidget(card)

        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self._rb_file = QRadioButton("文件模式（默认 · 零开销 · AI 主动调工具）")
        self._rb_rag = QRadioButton("RAG 向量检索（语义召回 · 需要 embedding）")
        self._mode_group.addButton(self._rb_file)
        self._mode_group.addButton(self._rb_rag)
        self._rb_file.toggled.connect(self._refresh_visibility)
        card.add_content(self._rb_file)
        card.add_content(self._mk_secondary("不加载额外模型，适合先跑通功能。"))

        rag_row = QWidget()
        rag_lay = QHBoxLayout(rag_row)
        rag_lay.setContentsMargins(0, 0, 0, 0)
        rag_lay.setSpacing(Spacing.SM)
        rag_lay.addWidget(self._rb_rag, 1)
        guide_btn = QPushButton("教程")
        guide_btn.setFlat(True)
        guide_btn.setProperty("role", "ghost")
        guide_btn.clicked.connect(lambda: open_feature_guide("embedding_rag", self))
        rag_lay.addWidget(guide_btn)
        card.add_content(rag_row)
        card.add_content(self._mk_secondary("适合长期运行后从大量记忆中取最相关内容。"))

        self._keyword_chk = QCheckBox("命中关键词强制保存（记住 / 约定 / 我叫等）")
        self._keyword_chk.setChecked(True)
        card.add_content(self._keyword_chk)

        sep = QFrame()
        sep.setProperty("role", "separator")
        card.add_content(sep)

        self._embedding_body = QFrame()
        self._embedding_body.setObjectName("Card")
        body = QVBoxLayout(self._embedding_body)
        body.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        body.setSpacing(Spacing.SM)

        title = QLabel("Embedding 配置")
        title.setProperty("role", "title-3")
        body.addWidget(title)

        desc = QLabel("API 模式可复用已配置 provider，也可单独配置 Embedding provider；本地模式模型放在项目 data/models 下。")
        desc.setProperty("role", "secondary")
        desc.setWordWrap(True)
        body.addWidget(desc)

        form = QFormLayout()
        self._form = form
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(Spacing.SM)

        self._type_combo = QComboBox()
        self._type_combo.addItem("API 服务", "api")
        self._type_combo.addItem("本地 sentence-transformers", "local")
        self._type_combo.currentIndexChanged.connect(self._refresh_visibility)
        form.addRow(QLabel("Embedding 来源"), self._type_combo)

        self._api_provider = QComboBox()
        self._api_provider.currentIndexChanged.connect(self._on_provider_changed)
        form.addRow(QLabel("Provider"), self._api_provider)

        self._api_base_url = QLineEdit()
        self._api_base_url.setPlaceholderText("https://api.example.com/v1")
        form.addRow(QLabel("Base URL"), self._api_base_url)

        self._api_model = QLineEdit()
        self._api_model.setPlaceholderText("如 text-embedding-v4 / embedding-3 / doubao-embedding-text-240715")
        form.addRow(QLabel("模型 ID"), self._api_model)

        self._api_key = ApiKeyInput(
            placeholder="复用已有 provider 时可留空；独立 provider 必填",
            allow_empty_test=True,
        )
        self._api_key.test_requested.connect(self._on_test_api)
        form.addRow(QLabel("API 密钥"), self._api_key)

        self._local_quality = QComboBox()
        self._local_quality.addItem("高性能（all-MiniLM-L6-v2 · 约 90MB）", "performance")
        self._local_quality.addItem("中文质量优先（bge-large-zh-v1.5 · 约 1.3GB）", "quality")
        self._local_quality.currentIndexChanged.connect(self._on_quality_changed)
        form.addRow(QLabel("模型选择"), self._local_quality)

        self._local_dir = QLineEdit()
        self._local_dir.setPlaceholderText("data/models/embedding/all-MiniLM-L6-v2")
        self._local_dir.textChanged.connect(lambda *_: self._check_local_model())
        self._local_dir_row = _path_picker_row(
            self._local_dir,
            parent=self,
            title="选择 Embedding 模型目录",
            directory=True,
        )
        form.addRow(QLabel("模型目录"), self._local_dir_row)
        body.addLayout(form)

        self._hint = QLabel("")
        self._hint.setProperty("role", "secondary")
        self._hint.setWordWrap(True)
        body.addWidget(self._hint)

        self._warning = QLabel("")
        self._warning.setProperty("role", "warning")
        self._warning.setWordWrap(True)
        self._warning.setVisible(False)
        body.addWidget(self._warning)

        actions = QHBoxLayout()
        self._download_btn = QPushButton("安装指引")
        self._download_btn.setProperty("role", "secondary")
        self._download_btn.clicked.connect(self._on_download)
        actions.addWidget(self._download_btn)
        self._open_dir_btn = QPushButton("打开目录")
        self._open_dir_btn.setProperty("role", "secondary")
        self._open_dir_btn.clicked.connect(lambda: _open_directory(self._embedding_dir()))
        actions.addWidget(self._open_dir_btn)
        actions.addStretch(1)
        body.addLayout(actions)

        card.add_content(self._embedding_body)

        self._rb_file.setChecked(True)
        self._refresh_provider_choices()
        self._refresh_visibility()

    def _mk_secondary(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setProperty("role", "secondary")
        lbl.setWordWrap(True)
        lbl.setContentsMargins(24, 0, 0, 0)
        return lbl

    def _refresh_provider_choices(self) -> None:
        current = self._api_provider.currentData()
        self._api_provider.clear()
        choices: list[tuple[str, str]] = []

        main_id = f"{self.context.main.preset}_main"
        choices.append((f"existing:{main_id}", f"复用主模型 · {self.context.main.display_name or self.context.main.preset}"))

        if self.context.proactive.enabled and not self.context.proactive.use_main:
            pid = f"{self.context.proactive.preset}_proactive"
            choices.append((f"existing:{pid}", f"复用主动思考 · {self.context.proactive.preset}"))
        if self.context.summary.enabled and not self.context.summary.use_main:
            pid = f"{self.context.summary.preset}_summary"
            choices.append((f"existing:{pid}", f"复用历史总结 · {self.context.summary.preset}"))

        for preset, info in _EMBEDDING_PRESETS.items():
            if preset != "custom" and preset not in _PRESET_DEFAULTS:
                continue
            choices.append((f"new:{preset}", info["display"]))

        seen = set()
        for value, label in choices:
            if value in seen:
                continue
            seen.add(value)
            self._api_provider.addItem(label, value)

        target = current
        if not target:
            if self.context.embedding_provider_preset:
                target = f"new:{self.context.embedding_provider_preset}"
            elif self.context.embedding_provider:
                target = f"existing:{self.context.embedding_provider}"
            else:
                target = f"existing:{main_id}"
        idx = self._api_provider.findData(target)
        if idx >= 0:
            self._api_provider.setCurrentIndex(idx)
        self._on_provider_changed()

    def _on_provider_changed(self, *_args) -> None:
        value = self._api_provider.currentData() or ""
        is_custom = value == "new:custom"
        _set_form_field_visible(self._form, self._api_base_url, self._rb_rag.isChecked() and self._type_combo.currentData() == "api" and is_custom)
        if value.startswith("new:"):
            preset = value.split(":", 1)[1]
            default_model = _EMBEDDING_PRESETS.get(preset, {}).get("model", "")
            if default_model and not self._api_model.text().strip():
                self._api_model.setText(default_model)

    def _refresh_visibility(self, *_args) -> None:
        is_rag = self._rb_rag.isChecked()
        is_local = self._type_combo.currentData() == "local"
        self._embedding_body.setVisible(is_rag)

        _set_form_field_visible(self._form, self._api_provider, is_rag and not is_local)
        value = self._api_provider.currentData() or ""
        _set_form_field_visible(self._form, self._api_base_url, is_rag and not is_local and value == "new:custom")
        _set_form_field_visible(self._form, self._api_model, is_rag and not is_local)
        _set_form_field_visible(self._form, self._api_key, is_rag and not is_local)
        _set_form_field_visible(self._form, self._local_quality, is_rag and is_local)
        _set_form_field_visible(self._form, self._local_dir_row, is_rag and is_local)

        self._download_btn.setVisible(is_rag and is_local)
        self._open_dir_btn.setVisible(is_rag and is_local)
        self._warning.setVisible(False)
        if not is_rag:
            return
        if is_local:
            self._hint.setText("切到下一页前会检查模型目录。未就绪时可查看安装指引。")
            self._check_local_model()
        else:
            self._hint.setText("可复用已有 provider；若主模型不提供 embedding，选择独立 Embedding provider 并填写其密钥。")

    def _on_quality_changed(self, *_args) -> None:
        current = self._local_dir.text().strip()
        known = {
            "data/models/embedding/all-MiniLM-L6-v2",
            "data/models/embedding/bge-large-zh-v1.5",
        }
        if not current or current in known:
            self._local_dir.setText(self._default_local_dir())
        self._check_local_model()

    def _default_local_dir(self) -> str:
        return (
            "data/models/embedding/bge-large-zh-v1.5"
            if self._local_quality.currentData() == "quality"
            else "data/models/embedding/all-MiniLM-L6-v2"
        )

    def _embedding_dir(self) -> str:
        return self._local_dir.text().strip() or self._default_local_dir()

    def _check_local_model(self) -> bool:
        if self._type_combo.currentData() != "local":
            self._warning.setVisible(False)
            return True
        d = self._embedding_dir()
        ok = _directory_has_files(d)
        if ok:
            self._warning.setVisible(False)
        else:
            self._warning.setText(f"⚠ 模型目录未就绪：{d}")
            self._warning.setVisible(True)
        return ok

    def _on_download(self) -> None:
        quality = self._local_quality.currentData() or "performance"
        if quality == "quality":
            plugin_name = "embedding_bge_zh"
            display_name = "bge-large-zh-v1.5 中文向量模型"
        else:
            plugin_name = "embedding_minilm"
            display_name = "all-MiniLM-L6-v2 向量模型"
        _start_plugin_download(
            self,
            plugin_name,
            plugin_name,
            display_name,
            on_finished=self._check_local_model,
        )

    async def _test_api_current(self) -> tuple[bool, str]:
        source = self._api_provider.currentData() or ""
        model = self._api_model.text().strip()
        key = self._api_key.text().strip()
        if not source:
            return False, "请先选择 Embedding provider"
        if not model:
            return False, "请先填写 embedding 模型 ID"
        base_url, api_key = self._api_endpoint_from_widgets(source, key)
        if not base_url:
            return False, "无法解析所选 provider 的 Base URL"
        if not api_key:
            return False, "请填写 API 密钥，或复用一个已配置密钥的 provider"
        try:
            from features.embedding import OpenAICompatEmbeddingService

            service = OpenAICompatEmbeddingService(
                base_url=base_url,
                api_key=api_key,
                model=model,
                timeout=8.0,
            )
            try:
                await service.embed_one("test")
            finally:
                await service.aclose()
            return True, "已就位"
        except Exception as e:  # noqa: BLE001
            return False, f"Embedding 模型检测失败：{e}"

    def _on_test_api(self, _key: str) -> None:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            self._api_key.set_test_state("error", "事件循环未就绪")
            return

        async def _do_test() -> None:
            ok, message = await self._test_api_current()
            self._api_key.set_test_state("success" if ok else "error", message)

        loop.create_task(_do_test())

    def _api_endpoint_from_widgets(
        self, source: str, explicit_key: str
    ) -> tuple[str, str]:
        key = explicit_key
        if source.startswith("new:"):
            preset = source.split(":", 1)[1]
            if preset == "custom":
                base_url = self._api_base_url.text().strip()
                try:
                    from providers.registry import normalize_base_url

                    return normalize_base_url(base_url, "openai_compat"), key
                except Exception:
                    return "", key
            info = _PRESET_DEFAULTS.get(preset, {})
            try:
                from providers.registry import normalize_base_url

                protocol = info.get("protocol", "openai_compat")
                return normalize_base_url(info.get("url", ""), protocol), key
            except Exception:
                return "", key

        provider_id = source.split(":", 1)[1] if ":" in source else source
        preset = self.context.main.preset
        base_url = self.context.main.base_url if preset == "custom" else ""
        if provider_id == f"{self.context.main.preset}_main":
            key = key or self.context.main.api_key
        elif provider_id == f"{self.context.proactive.preset}_proactive":
            preset = self.context.proactive.preset
            key = key or self.context.proactive.api_key
        elif provider_id == f"{self.context.summary.preset}_summary":
            preset = self.context.summary.preset
            key = key or self.context.summary.api_key

        if preset == "custom":
            return base_url, key
        info = _PRESET_DEFAULTS.get(preset, {})
        try:
            from providers.registry import normalize_base_url

            protocol = info.get("protocol", "openai_compat")
            return normalize_base_url(info.get("url", ""), protocol), key
        except Exception:
            return "", key

    def refresh(self) -> None:
        self._refresh_provider_choices()
        self._keyword_chk.setChecked(self.context.long_term_memory_keyword_trigger_save)
        if self.context.long_term_memory_mode == "rag":
            self._rb_rag.setChecked(True)
        else:
            self._rb_file.setChecked(True)

        idx = self._type_combo.findData(self.context.embedding_type or "api")
        if idx >= 0:
            self._type_combo.setCurrentIndex(idx)
        idx_p = self._api_provider.findData(self.context.embedding_provider)
        if idx_p < 0 and self.context.embedding_provider:
            if self.context.embedding_provider_preset:
                idx_p = self._api_provider.findData(f"new:{self.context.embedding_provider_preset}")
            else:
                idx_p = self._api_provider.findData(f"existing:{self.context.embedding_provider}")
        if idx_p >= 0:
            self._api_provider.setCurrentIndex(idx_p)
        self._api_base_url.setText(self.context.embedding_provider_base_url or "")
        self._api_model.setText(self.context.embedding_model or "")
        if self.context.embedding_api_key:
            self._api_key.set_text(self.context.embedding_api_key)

        idx_q = self._local_quality.findData(self.context.embedding_local_quality or "performance")
        if idx_q >= 0:
            self._local_quality.setCurrentIndex(idx_q)
        self._local_dir.setText(
            self.context.embedding_local_model_dir or self._default_local_dir()
        )
        self._refresh_visibility()

    def save(self) -> bool:
        self.context.long_term_memory_mode = "rag" if self._rb_rag.isChecked() else "file"
        self.context.long_term_memory_keyword_trigger_save = self._keyword_chk.isChecked()

        if self.context.long_term_memory_mode == "file":
            return True

        if self._type_combo.currentData() == "api":
            source = self._api_provider.currentData() or ""
            model = self._api_model.text().strip()
            if not source:
                self.invalid_input.emit("RAG API 模式需要选择一个 provider")
                return False
            if not model:
                self.invalid_input.emit("RAG API 模式需要填写 embedding 模型 ID")
                return False
            key = self._api_key.text()
            endpoint_url, _endpoint_key = self._api_endpoint_from_widgets(source, key)
            self.context.embedding_provider_preset = ""
            self.context.embedding_provider_display_name = ""
            self.context.embedding_provider_base_url = ""
            self.context.embedding_provider_protocol = "openai_compat"
            if source.startswith("new:"):
                preset = source.split(":", 1)[1]
                if not key:
                    self.invalid_input.emit("独立 Embedding provider 需要填写 API 密钥")
                    return False
                provider = f"embedding_{preset}"
                if preset == "custom":
                    base_url = self._api_base_url.text().strip()
                    if not base_url:
                        self.invalid_input.emit("自定义 Embedding provider 需要 Base URL")
                        return False
                    self.context.embedding_provider_base_url = base_url
                elif endpoint_url:
                    self.context.embedding_provider_base_url = endpoint_url
                self.context.embedding_provider_preset = preset
                self.context.embedding_provider_display_name = _EMBEDDING_PRESETS.get(preset, {}).get("display", provider)
            else:
                provider = source.split(":", 1)[1] if ":" in source else source
                if endpoint_url:
                    self.context.embedding_provider_base_url = endpoint_url
            self.context.embedding_type = "api"
            self.context.embedding_provider = provider
            self.context.embedding_model = model
            self.context.embedding_api_key = key
            return True

        local_dir = self._embedding_dir()
        if not _directory_has_files(local_dir):
            _prompt_download_model(
                self,
                "Embedding 模型未就绪",
                "你选择了 RAG 本地模型，但模型目录还没有可用文件。\n\n"
                f"当前目录：{local_dir}\n\n"
                "请按安装指引放置模型，或修复模型目录，然后再进入下一页。",
                self._on_download,
            )
            return False
        self.context.embedding_type = "local"
        self.context.embedding_local_quality = self._local_quality.currentData() or "performance"
        self.context.embedding_local_model_dir = local_dir
        return True

    async def validate_before_next(self) -> bool:
        if self.context.long_term_memory_mode != "rag" or self.context.embedding_type != "api":
            return True
        provider_id = self.context.embedding_provider
        base_url, api_key = self._embedding_endpoint(provider_id)
        if not base_url:
            self.invalid_input.emit("RAG API 模式无法解析所选 provider 的 Base URL")
            return False
        if not api_key:
            self.invalid_input.emit("RAG API 模式需要可用密钥；请填写 API 密钥或复用带密钥的 provider")
            return False
        try:
            from features.embedding import OpenAICompatEmbeddingService

            service = OpenAICompatEmbeddingService(
                base_url=base_url,
                api_key=api_key,
                model=self.context.embedding_model,
                timeout=8.0,
            )
            try:
                await service.embed_one("test")
            finally:
                await service.aclose()
        except Exception as e:  # noqa: BLE001
            self.invalid_input.emit(f"Embedding 模型检测失败：{e}")
            return False
        return True

    def _embedding_endpoint(self, provider_id: str) -> tuple[str, str]:
        if self.context.embedding_api_key:
            api_key = self.context.embedding_api_key
        else:
            api_key = ""
            if provider_id == f"{self.context.main.preset}_main":
                api_key = self.context.main.api_key
            elif provider_id == f"{self.context.proactive.preset}_proactive":
                api_key = self.context.proactive.api_key
            elif provider_id == f"{self.context.summary.preset}_summary":
                api_key = self.context.summary.api_key

        def _normalized(url: str, protocol: str = "openai_compat") -> str:
            from providers.registry import normalize_base_url

            return normalize_base_url((url or "").strip(), protocol)

        if self.context.embedding_provider_base_url:
            try:
                return _normalized(
                    self.context.embedding_provider_base_url,
                    self.context.embedding_provider_protocol or "openai_compat",
                ), api_key
            except Exception:
                return "", api_key

        if self.context.embedding_provider_preset:
            preset = self.context.embedding_provider_preset
            try:
                if preset == "custom":
                    return "", api_key
                info = _PRESET_DEFAULTS.get(preset, {})
                protocol = info.get("protocol", "openai_compat")
                return _normalized(info.get("url", ""), protocol), api_key
            except Exception:
                return "", api_key

        preset = self.context.main.preset
        if provider_id == f"{self.context.proactive.preset}_proactive":
            preset = self.context.proactive.preset
        elif provider_id == f"{self.context.summary.preset}_summary":
            preset = self.context.summary.preset
        elif provider_id.startswith("embedding_"):
            candidate = provider_id.removeprefix("embedding_")
            if candidate in _PRESET_DEFAULTS:
                preset = candidate
        elif provider_id in _PRESET_DEFAULTS:
            preset = provider_id

        try:
            if preset == "custom":
                return self.context.main.base_url, api_key
            info = _PRESET_DEFAULTS.get(preset, {})
            protocol = info.get("protocol", "openai_compat")
            return _normalized(info.get("url", ""), protocol), api_key
        except Exception:
            return "", api_key
