"""Feature setting sections for SettingsPage.

This module is a mechanical split from `ui.dashboard.settings_page`. Keep
behavior equivalent; do not change feature cards, summaries, dialogs, or save
logic while moving methods.
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ...theme import Spacing
from ...widgets import show_message
from ...wizard.components import SectionCard
from ..copy import DASHBOARD_COPY
from .dialogs import (
    _ASREditDialog,
    _EmbeddingEditDialog,
    _TTSEditDialog,
    _VisionEditDialog,
    _WeatherEditDialog,
)


def _settings_page_global(name: str, fallback):
    module = sys.modules.get("ui.dashboard.settings_page")
    if module is None:
        return fallback
    return getattr(module, name, fallback)


class SettingsFeaturesMixin:
    # ============================================================
    # 功能节：features 全部可改
    # ============================================================

    def _build_features_section(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Spacing.MD)

        card = SectionCard(
            title=DASHBOARD_COPY["settings.section_features"],
            subtitle="每项功能独立配置。开关即时保存，密钥/配置修改后需重启。",
        )
        card.add_content(self._build_vision_card())
        card.add_content(self._build_weather_card())
        card.add_content(self._build_websearch_card())
        card.add_content(self._build_tts_card())
        layout.addWidget(card)
        layout.addWidget(self._build_emoji_section())
        return page

    def _build_vision_card(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("Card")
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.SM)

        v = self._cfg().features.vision
        head = QHBoxLayout()
        chk = QCheckBox("看懂图片（vision）")
        chk.setChecked(v.enabled)
        self._vision_chk = chk
        head.addWidget(chk)
        head.addStretch(1)
        edit_btn = QPushButton("编辑配置")
        edit_btn.setProperty("role", "secondary")
        head.addWidget(edit_btn)
        outer.addLayout(head)

        summary = QLabel(self._vision_summary())
        summary.setProperty("role", "secondary")
        summary.setWordWrap(True)
        summary.setContentsMargins(24, 0, 0, 0)
        self._vision_summary_lbl = summary
        outer.addWidget(summary)

        edit_btn.clicked.connect(lambda: self._open_vision_dialog(chk, summary))

        def _on_toggle(on: bool) -> None:
            if self._suppress_signals:
                return
            v_now = self._cfg().features.vision
            if on:
                if not v_now.provider or not v_now.model:
                    if not self._open_vision_dialog(chk, summary):
                        self._suppress_signals = True
                        chk.setChecked(False)
                        self._suppress_signals = False
                        return
                v_now.enabled = True
            else:
                v_now.enabled = False
            summary.setText(self._vision_summary())
            try:
                self._validate_features_then_save("vision toggle")
            except Exception as e:  # noqa: BLE001
                self._suppress_signals = True
                chk.setChecked(not on)
                v_now.enabled = not on
                self._suppress_signals = False
                show_message(self, "未能保存", str(e))

        chk.toggled.connect(_on_toggle)
        return wrap

    def _vision_summary(self) -> str:
        v = self._cfg().features.vision
        if not v.provider and not v.model:
            return "未配置 · 启用时会弹窗引导填写"
        return f"provider: {v.provider or '?'}  ·  model: {v.model or '?'}"

    def _open_vision_dialog(self, chk: QCheckBox, summary: QLabel) -> bool:
        v = self._cfg().features.vision
        provider_ids = list(self._cfg().providers.keys())
        provider_presets = {pid: (p.preset or "") for pid, p in self._cfg().providers.items()}
        dlg = _settings_page_global("_VisionEditDialog", _VisionEditDialog)(
            provider_ids, provider_presets,
            v.provider, v.model, v.api_key_id, self,
        )
        if not dlg.exec() or not dlg.result_data:
            return False
        data = dlg.result_data
        v.provider = data["provider"]
        v.model = data["model"]
        if data["api_key"]:
            sid = v.api_key_id or "vision_key"
            v.api_key_id = sid
            provider_cfg = self._cfg().providers.get(v.provider)
            if provider_cfg is not None:
                provider_cfg.api_key_id = sid
            self._set_secret(sid, data["api_key"])
        v.enabled = True
        self._suppress_signals = True
        chk.setChecked(True)
        self._suppress_signals = False
        summary.setText(self._vision_summary())
        self._save_now(needs_restart=True, change_desc="features.vision (dialog)")
        return True

    def _build_weather_card(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("Card")
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.SM)

        w = self._cfg().features.weather
        head = QHBoxLayout()
        chk = QCheckBox("查天气（和风天气）")
        chk.setChecked(w.enabled)
        self._weather_chk = chk
        head.addWidget(chk)
        head.addStretch(1)
        edit_btn = QPushButton("编辑配置")
        edit_btn.setProperty("role", "secondary")
        head.addWidget(edit_btn)
        outer.addLayout(head)

        summary = QLabel(self._weather_summary())
        summary.setProperty("role", "secondary")
        summary.setWordWrap(True)
        summary.setContentsMargins(24, 0, 0, 0)
        self._weather_summary_lbl = summary
        outer.addWidget(summary)

        edit_btn.clicked.connect(lambda: self._open_weather_dialog(chk, summary))

        def _on_toggle(on: bool) -> None:
            if self._suppress_signals:
                return
            w_now = self._cfg().features.weather
            if on:
                if not w_now.api_key_id:
                    if not self._open_weather_dialog(chk, summary):
                        self._suppress_signals = True
                        chk.setChecked(False)
                        self._suppress_signals = False
                        return
                w_now.enabled = True
            else:
                w_now.enabled = False
            summary.setText(self._weather_summary())
            try:
                self._validate_features_then_save("weather toggle")
            except Exception as e:  # noqa: BLE001
                self._suppress_signals = True
                chk.setChecked(not on)
                w_now.enabled = not on
                self._suppress_signals = False
                show_message(self, "未能保存", str(e))

        chk.toggled.connect(_on_toggle)
        return wrap

    def _weather_summary(self) -> str:
        w = self._cfg().features.weather
        if not w.api_key_id:
            return "未配置 · 启用时会弹窗引导填写"
        return f"host: {w.host}  ·  密钥 id: {w.api_key_id}"

    def _open_weather_dialog(self, chk: QCheckBox, summary: QLabel) -> bool:
        w = self._cfg().features.weather
        dlg = _settings_page_global("_WeatherEditDialog", _WeatherEditDialog)(w.host or "", w.api_key_id, self)
        if not dlg.exec() or not dlg.result_data:
            return False
        data = dlg.result_data
        w.host = data["host"]
        if data["api_key"]:
            sid = w.api_key_id or "qweather"
            w.api_key_id = sid
            self._set_secret(sid, data["api_key"])
        w.enabled = True
        self._suppress_signals = True
        chk.setChecked(True)
        self._suppress_signals = False
        summary.setText(self._weather_summary())
        self._save_now(needs_restart=True, change_desc="features.weather (dialog)")
        return True

    def _build_websearch_card(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("Card")
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.SM)

        ws = self._cfg().features.web_search
        head = QHBoxLayout()
        chk = QCheckBox("联网搜索（DuckDuckGo · 无需密钥）")
        chk.setChecked(ws.enabled)
        self._ws_chk = chk
        head.addWidget(chk)
        head.addStretch(1)
        outer.addLayout(head)

        def _on_toggle(on: bool) -> None:
            if self._suppress_signals:
                return
            ws.enabled = on
            self._save_now(needs_restart=True, change_desc="features.web_search.enabled")

        chk.toggled.connect(_on_toggle)
        return wrap

    def _open_asr_dialog(self, chk: QCheckBox, summary: QLabel) -> bool:
        a = self._cfg().features.asr
        dlg = _settings_page_global("_ASREditDialog", _ASREditDialog)(a, self)
        if not dlg.exec() or not dlg.result_data:
            return False
        data = dlg.result_data
        a.type = data["type"]
        if data["type"] == "api":
            a.provider = data["provider"]
            a.extra_credentials = data.get("extra_credentials", {})
            if data.get("api_key"):
                sid = a.api_key_id or "asr_key"
                a.api_key_id = sid
                self._set_secret(sid, data["api_key"])
        else:
            a.device = data["device"]
            a.language = data["language"]
            a.model_dir = data["model_dir"]
        a.enabled = True
        self._suppress_signals = True
        chk.setChecked(True)
        self._suppress_signals = False
        summary.setText(self._asr_summary())
        self._save_now(needs_restart=True, change_desc="features.asr (dialog)")
        return True

    def _build_tts_card(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("Card")
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.SM)

        feat = self._cfg().features.tts
        head = QHBoxLayout()
        chk = QCheckBox("用声音说话（TTS）")
        chk.setChecked(feat.enabled)
        self._tts_chk = chk
        head.addWidget(chk)
        head.addStretch(1)
        edit_btn = QPushButton("编辑配置")
        edit_btn.setProperty("role", "secondary")
        head.addWidget(edit_btn)
        outer.addLayout(head)

        summary = QLabel(self._tts_summary())
        summary.setProperty("role", "secondary")
        summary.setWordWrap(True)
        summary.setContentsMargins(24, 0, 0, 0)
        self._tts_summary_lbl = summary
        outer.addWidget(summary)

        edit_btn.clicked.connect(lambda: self._open_tts_dialog(chk, summary))

        def _on_toggle(on: bool) -> None:
            if self._suppress_signals:
                return
            if on:
                t = self._cfg().features.tts
                if t.type == "api" and not t.provider:
                    if not self._open_tts_dialog(chk, summary):
                        self._suppress_signals = True
                        chk.setChecked(False)
                        self._suppress_signals = False
                        return
                t.enabled = True
            else:
                feat.enabled = False
            summary.setText(self._tts_summary())
            self._save_now(needs_restart=True, change_desc="features.tts.enabled")

        chk.toggled.connect(_on_toggle)
        return wrap

    def _open_tts_dialog(self, chk: QCheckBox, summary: QLabel) -> bool:
        t = self._cfg().features.tts
        dlg = _settings_page_global("_TTSEditDialog", _TTSEditDialog)(t, self)
        if not dlg.exec() or not dlg.result_data:
            return False
        data = dlg.result_data
        t.type = data["type"]
        if data["type"] == "api":
            t.provider = data["provider"]
            t.extra_credentials = data.get("extra_credentials", {})
            if data["provider"] == "edge":
                t.api_key_id = None
            elif data.get("api_key"):
                sid = t.api_key_id or f"tts_{data['provider']}"
                t.api_key_id = sid
                self._set_secret(sid, data["api_key"])
        else:
            t.device = data.get("device", "auto")
            t.reference_audio = data["reference_audio"]
            t.default_prompt = data["default_prompt"]
            t.model_dir = data.get("model_dir", "data/models/VoxCPM2")
            t.load_denoiser = bool(data.get("load_denoiser", False))
            t.cfg_value = float(data.get("cfg_value", 2.0))
            t.inference_timesteps = int(data.get("inference_timesteps", 10))
        t.enabled = True
        self._suppress_signals = True
        chk.setChecked(True)
        self._suppress_signals = False
        summary.setText(self._tts_summary())
        self._save_now(needs_restart=True, change_desc="features.tts (dialog)")
        return True

    def _asr_summary(self) -> str:
        a = self._cfg().features.asr
        if not a.enabled:
            return "未启用"
        if a.type == "local":
            return f"本地 · {a.local_model} · device={a.device} · lang={a.language}"
        return f"API · provider={a.provider or '?'}"

    def _tts_summary(self) -> str:
        t = self._cfg().features.tts
        if not t.enabled:
            return "未启用"
        if t.type == "local":
            detail = f" · ref={t.reference_audio}" if t.reference_audio else ""
            if t.default_prompt and not detail:
                detail = f" · prompt={t.default_prompt}"
            return f"本地 · {t.local_model} · device={t.device}{detail}"
        if t.provider == "edge":
            return "API · EdgeTTS（无需密钥）"
        voice = t.extra_credentials.get("voice", "") if t.extra_credentials else ""
        detail = f" · voice={voice}" if voice else ""
        return f"API · provider={t.provider or '?'}{detail}"

    def _build_embedding_card(self) -> QWidget:
        """RAG 历史召回 embedding 配置卡片。"""
        wrap = QFrame()
        wrap.setObjectName("Card")
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.SM)

        title = QLabel("Embedding（RAG 历史召回）")
        title.setProperty("role", "title-3")
        outer.addWidget(title)

        desc_text = "将历史对话转为向量，用于 RAG 历史召回增强；重要记忆仍按原有机制保存和注入。"
        lt = self._cfg().features.long_term_memory
        if lt.mode != "rag":
            desc_text += "\n当前未启用 RAG 历史召回，此处配置暂不生效。"
        desc = QLabel(desc_text)
        desc.setProperty("role", "secondary")
        desc.setWordWrap(True)
        outer.addWidget(desc)

        summary = QLabel(self._embedding_summary())
        summary.setProperty("role", "secondary")
        summary.setWordWrap(True)
        summary.setContentsMargins(0, Spacing.SM, 0, 0)
        self._emb_summary_lbl = summary
        outer.addWidget(summary)

        action_row = QHBoxLayout()
        edit_btn = QPushButton("编辑 Embedding 配置")
        edit_btn.setProperty("role", "secondary")
        action_row.addWidget(edit_btn)
        guide_btn = QPushButton("教程")
        guide_btn.setProperty("role", "secondary")
        action_row.addWidget(guide_btn)
        action_row.addStretch(1)
        outer.addLayout(action_row)

        edit_btn.clicked.connect(lambda: self._open_embedding_dialog(summary))
        guide_btn.clicked.connect(lambda: self._open_feature_guide("embedding_rag"))
        return wrap

    def _open_feature_guide(self, guide_name: str) -> None:
        from ui.wizard.components import open_feature_guide

        open_feature_guide(guide_name, self)

    def _embedding_summary(self) -> str:
        emb = self._cfg().features.embedding
        if emb.type == "api":
            return f"API 模式 · provider={emb.provider or '?'} · model={emb.api_model or '?'}"
        return f"本地模式 · {emb.local_quality} · dir={emb.local_model_dir or '?'}"

    def _open_embedding_dialog(self, summary: QLabel) -> None:
        emb = self._cfg().features.embedding
        provider_ids = list(self._cfg().providers.keys()) if self._cfg().providers else []
        dlg = _settings_page_global("_EmbeddingEditDialog", _EmbeddingEditDialog)(provider_ids, emb, self)
        if not dlg.exec() or not dlg.result_data:
            return
        data = dlg.result_data
        emb.enabled = True
        emb.type = data["type"]
        if data["type"] == "api":
            emb.provider = data["provider"]
            emb.api_model = data["model"]
            if data.get("api_key"):
                sid = emb.api_key_id or "embedding_key"
                emb.api_key_id = sid
                self._set_secret(sid, data["api_key"])
        else:
            emb.local_quality = data["local_quality"]
            emb.local_model_dir = data["local_model_dir"]
        summary.setText(self._embedding_summary())
        self._save_now(needs_restart=True, change_desc="features.embedding (dialog)")

    def _build_longterm_memory_card(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("Card")
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.SM)

        lt = self._cfg().features.long_term_memory
        title = QLabel("长期记忆")
        title.setProperty("role", "title-3")
        outer.addWidget(title)

        desc = QLabel("重要记忆始终启用。这里仅控制是否额外从历史对话中做 RAG 向量召回。")
        desc.setProperty("role", "secondary")
        desc.setWordWrap(True)
        outer.addWidget(desc)

        group = QButtonGroup(wrap)
        group.setExclusive(True)
        rb_file = QRadioButton("不启用 RAG 历史召回（默认 · 重要记忆仍启用）")
        rb_rag = QRadioButton("启用 RAG 历史召回增强（需启用下方 Embedding 配置）")
        rb_file.setChecked(lt.mode == "file")
        rb_rag.setChecked(lt.mode == "rag")
        group.addButton(rb_file)
        group.addButton(rb_rag)
        outer.addWidget(rb_file)
        outer.addWidget(rb_rag)

        def _on_mode(*_) -> None:
            if self._suppress_signals:
                return
            lt.mode = "rag" if rb_rag.isChecked() else "file"
            self._save_now(needs_restart=True, change_desc=f"long_term_memory.mode={lt.mode}")

        rb_file.toggled.connect(_on_mode)
        rb_rag.toggled.connect(_on_mode)
        return wrap

    def _validate_features_then_save(self, change_desc: str) -> None:
        # 让 pydantic 重新走一遍 model_validator
        f = self._cfg().features
        # vision 启用时 provider 或 api_key_id 不能空
        if f.vision.enabled and f.vision.type == "api":
            if not f.vision.provider and not f.vision.api_key_id:
                raise ValueError("vision 启用必须填 provider 或 api_key_id")
        # weather 启用时密钥必填
        if f.weather.enabled and not f.weather.api_key_id:
            raise ValueError("weather 启用必须填 api_key_id（先粘贴 API 密钥再开启开关）")
        self._save_now(needs_restart=True, change_desc=change_desc)
