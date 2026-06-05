"""可选功能开关 + 详细配置。"""

from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout

from ...theme import Spacing
from ..components import SectionCard
from ..context import BaseStepView, WizardContext
from ..copy import COPY
from ._shared import (
    _add_guide_button,
    _directory_has_files,
    _open_directory,
    _path_picker_row,
    _prompt_download_model,
    _resolve_project_path,
    _set_form_field_visible,
    _start_plugin_download,
)
from .feature_cards.simple_toggle import _SimpleFeatureToggle
from .feature_cards.tts import _TTSFeatureCard
from .feature_cards.vision import _VISION_PRESETS, _VisionFeatureCard
from .feature_cards.weather import _WeatherFeatureCard


class FeaturesStepView(BaseStepView):
    """5 个 feature toggle。长期记忆/RAG 在独立「记忆方式」页配置。"""

    def __init__(self, context: WizardContext, parent=None) -> None:
        super().__init__(context, parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        card = SectionCard(
            title="选些可选的本领",
            subtitle=(
                "这些功能默认关闭。打开哪个，Debata 就拥有哪个能力。\n"
                "现在不开也没关系，之后随时能在设置里打开。"
            ),
            compact=True,
        )
        outer.addWidget(card)

        self._vision = _VisionFeatureCard(self.context)
        card.add_content(self._vision)

        self._weather = _WeatherFeatureCard()
        card.add_content(self._weather)

        # ASR 已改用 NapCat 内置语音转文字，不再暴露 Whisper 配置。
        self._tts = _TTSFeatureCard()
        card.add_content(self._tts)

        self._web_search = _SimpleFeatureToggle(
            COPY["features.web_search_title"],
            COPY["features.web_search_desc"],
            guide_name="web_search",
        )
        card.add_content(self._web_search)

    def refresh(self) -> None:
        self._vision.set_state(self.context.vision)
        self._weather.set_state(self.context.weather)
        self.context.asr.enabled = False
        self._tts.set_state(self.context.tts)
        self._web_search.set_enabled(self.context.web_search.enabled)

    def save(self) -> bool:
        v = self._vision.state()
        if v["enabled"]:
            if not v["model"]:
                self.invalid_input.emit("开了「看懂图片」就要填一下视觉模型 ID")
                return False
            if v["preset"] != "main" and not v["api_key"]:
                self.invalid_input.emit("开了「看懂图片」就要填一下视觉 provider 的 API 密钥")
                return False
            if v["preset"] == "custom" and not v["base_url"]:
                self.invalid_input.emit("自定义视觉 provider 需要填 Base URL")
                return False

        w = self._weather.state()
        if w["enabled"]:
            if not w["host"]:
                self.invalid_input.emit("开了「查天气」就要填一下 API 主机（控制台拷过来）")
                return False
            if not w["api_key"]:
                self.invalid_input.emit("开了「查天气」就要填一下和风天气 API 密钥")
                return False

        t = self._tts.state()
        if t["enabled"] and not self._tts.ensure_ready(self):
            return False

        self.context.vision.enabled = v["enabled"]
        self.context.vision.api_key = v["api_key"]
        self.context.vision.extra = {
            "preset": v["preset"],
            "model": v["model"],
            "base_url": v["base_url"],
        }

        self.context.weather.enabled = w["enabled"]
        self.context.weather.api_key = w["api_key"]
        self.context.weather.extra = {"host": w["host"]}

        self.context.asr.enabled = False
        self.context.asr.extra = {}

        self.context.tts.enabled = t["enabled"]
        self.context.tts.extra = t

        self.context.web_search.enabled = self._web_search.is_enabled()
        return True


__all__ = [
    "FeaturesStepView",
    "_SimpleFeatureToggle",
    "_TTSFeatureCard",
    "_VISION_PRESETS",
    "_VisionFeatureCard",
    "_WeatherFeatureCard",
    "_add_guide_button",
    "_directory_has_files",
    "_open_directory",
    "_path_picker_row",
    "_prompt_download_model",
    "_resolve_project_path",
    "_set_form_field_visible",
    "_start_plugin_download",
]
