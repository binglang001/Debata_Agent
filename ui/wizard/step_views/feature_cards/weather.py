"""Weather feature card."""

from __future__ import annotations

import asyncio

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ....theme import Spacing
from ...components import ApiKeyInput
from ...copy import COPY
from .._shared import _add_guide_button


class _WeatherFeatureCard(QFrame):
    """查天气：开关 + 和风 API 主机 + 密钥。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.SM)

        head = QHBoxLayout()
        self._check = QCheckBox(COPY["features.weather_title"])
        self._check.toggled.connect(self._toggle_body)
        head.addWidget(self._check)
        head.addStretch(1)
        _add_guide_button(head, "weather", self)
        outer.addLayout(head)

        d = QLabel(COPY["features.weather_desc"])
        d.setProperty("role", "secondary")
        d.setWordWrap(True)
        d.setContentsMargins(24, 0, 0, 0)
        outer.addWidget(d)

        self._body = QWidget()
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(24, Spacing.SM, 0, 0)
        body_layout.setSpacing(Spacing.SM)

        form = QFormLayout()
        self._form = form
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(Spacing.SM)

        self._host_edit = QLineEdit()
        self._host_edit.setPlaceholderText("yourdomain.qweatherapi.com")
        form.addRow(QLabel("API 主机"), self._host_edit)

        self._key_input = ApiKeyInput(placeholder="和风天气 API 密钥")
        self._key_input.test_requested.connect(self._on_test)
        form.addRow(QLabel("API 密钥"), self._key_input)

        body_layout.addLayout(form)

        hint = QLabel(
            "和风天气从 2024 起给每个账号分配独立 API Host，"
            "登录 https://console.qweather.com → 项目管理 → 复制「API Host」。"
            "免费开发版老主机 devapi.qweather.com 仅供历史项目使用，新账号已不再分配。"
        )
        hint.setProperty("role", "secondary")
        hint.setWordWrap(True)
        body_layout.addWidget(hint)

        outer.addWidget(self._body)
        self._body.setVisible(False)

    def _toggle_body(self, on: bool) -> None:
        self._body.setVisible(on)

    def state(self) -> dict:
        return {
            "enabled": self._check.isChecked(),
            "host": self._host_edit.text().strip(),
            "api_key": self._key_input.text(),
        }

    async def _test_current(self) -> tuple[bool, str]:
        host = self._host_edit.text().strip()
        key = self._key_input.text().strip()
        if not host:
            return False, "请先填写 API 主机"
        if not key:
            return False, "请先填写 API 密钥"
        try:
            from features.weather import WeatherService

            service = WeatherService(
                api_key=key,
                host=host,
                timeout_seconds=8.0,
            )
            result = await service.query("北京", days=1)
            if "失败" in result or "错误" in result or "未找到城市" in result:
                return False, result
            return True, "已就位"
        except Exception as e:  # noqa: BLE001
            return False, f"未能完成：{e}"

    def _on_test(self, _key: str) -> None:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            self._key_input.set_test_state("error", "事件循环未就绪")
            return

        async def _do_test() -> None:
            ok, message = await self._test_current()
            self._key_input.set_test_state("success" if ok else "error", message)

        loop.create_task(_do_test())

    def set_state(self, choice) -> None:
        self._check.setChecked(choice.enabled)
        extra = choice.extra or {}
        host = extra.get("host", "")
        if host:
            self._host_edit.setText(host)
        if choice.api_key:
            self._key_input.set_text(choice.api_key)


__all__ = ["_WeatherFeatureCard"]
