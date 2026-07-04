"""Dashboard 测试共享 helper。"""

# ruff: noqa: I001

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app_config.schema import (
    AgentConfig,
    AgentsConfig,
    NapCatAdapterConfig,
    ProviderConfig,
    RootConfig,
)

try:
    from PySide6.QtWidgets import QApplication, QWidget
    from ui.widgets.wheel_freeze import WheelFreezeFilter
except ImportError:  # pragma: no cover - 这些 helper 只在 Qt 测试中使用
    QApplication = None
    WheelFreezeFilter = None
    QWidget = None


def minimal_root_config() -> RootConfig:
    return RootConfig(
        providers={"ds": ProviderConfig(preset="deepseek", api_key_id="ds_key")},
        adapters={"default": NapCatAdapterConfig()},
        agents=AgentsConfig(chat=AgentConfig(provider="ds", model="deepseek-chat")),
    )


class EmptyHistory:
    async def records(self):
        return []


class EmptyImportant:
    def items(self):
        return []


def dashboard_runtime(tmp_paths, cfg: RootConfig | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        adapter=None,
        config=cfg or minimal_root_config(),
        embedding_service=None,
        history=EmptyHistory(),
        important=EmptyImportant(),
        model_activity={},
        paths=tmp_paths,
        persona=SimpleNamespace(name="Debata"),
        provider_health={},
        provider_registry=SimpleNamespace(presets={}),
        providers={},
        rag_store=None,
        secrets=SimpleNamespace(get=lambda _key: ""),
        usage_stats=None,
    )


async def pump_dashboard_events(qapp, rounds: int = 1) -> None:
    for _ in range(rounds):
        qapp.processEvents()
        await asyncio.sleep(0)


async def wait_for_dashboard_condition(qapp, condition, *, rounds: int = 50) -> None:
    for _ in range(rounds):
        if condition():
            return
        await pump_dashboard_events(qapp)
    assert condition()


def close_settings_page(page, qapp) -> None:
    remove_wheel_freeze_filters(page)
    page.close()
    for _ in range(3):
        qapp.processEvents()
    page.deleteLater()
    for _ in range(3):
        qapp.processEvents()


def remove_wheel_freeze_filters(root) -> None:
    if QApplication is None or QWidget is None or WheelFreezeFilter is None:
        return
    app = QApplication.instance()
    filters = []
    stored_filter = getattr(root, "_wheel_freeze_filter", None)
    if isinstance(stored_filter, WheelFreezeFilter):
        filters.append(stored_filter)
    filters.extend(root.findChildren(WheelFreezeFilter))
    seen = set()
    widgets = [root, *root.findChildren(QWidget)]
    for wheel_filter in filters:
        marker = id(wheel_filter)
        if marker in seen:
            continue
        seen.add(marker)
        if app is not None:
            app.removeEventFilter(wheel_filter)
        for widget in widgets:
            widget.removeEventFilter(wheel_filter)
