"""概览页回归测试。"""

# ruff: noqa: E402, I001

from __future__ import annotations

from types import SimpleNamespace

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from ui.dashboard.overview_page import OverviewPage

from tests.dashboard.helpers import minimal_root_config


def test_overview_page_shows_usage_activity_and_provider_counts(qapp):
    class FakeUsageStore:
        def summarize(self, range_name):
            assert range_name == "today"
            return SimpleNamespace(
                request_count=3,
                prompt_tokens=1000,
                completion_tokens=200,
                reasoning_tokens=50,
                cached_tokens=800,
                cache_creation_tokens=120,
                total_tokens=1250,
                cache_hit_rate=0.8,
            )

    cfg = minimal_root_config()
    runtime = type(
        "RuntimeStub",
        (),
        {
            "adapter": None,
            "config": cfg,
            "providers": {"ok": object(), "bad": object()},
            "provider_health": {
                "ok": SimpleNamespace(status="ok", latency_ms=123, message="可用"),
                "bad": SimpleNamespace(status="error", message="请求超时"),
            },
            "_hist_len": 0,
            "important": None,
            "usage_stats": FakeUsageStore(),
            "model_activity": {
                "state": "tool",
                "text": "调用工具：get_weather",
                "model": "deepseek-chat",
                "agent": "主模型",
                "tool_names": ["get_weather"],
            },
            "persona": type("Persona", (), {"name": "Mika"})(),
        },
    )()
    page = OverviewPage(runtime)
    try:
        labels = "\n".join(label.text() for label in page.findChildren(QtWidgets.QLabel))

        assert page._providers_card._right_label.text() == "1/2 可用"
        assert page._providers_card._right_label.maximumWidth() == 112
        assert page._providers_card._right_label.maximumHeight() == 22
        assert page._providers_card._right_label.toolTip() == "部分可用 1/2 · 请求超时"
        assert "部分可用 1/2 · 请求超时" not in labels
        assert "ok" in labels
        assert "可用 · 123ms" in labels
        assert "bad" in labels
        assert "异常 · 请求超时" in labels
        assert "渠道状态" in labels
        assert "用量统计" in labels
        assert "请求数" in labels
        assert "3" in labels
        assert "输入 token" in labels
        assert "输出 token" in labels
        assert "总 token" in labels
        assert "1,250" in labels
        assert "KV 命中 token" in labels
        assert "800" in labels
        assert "KV 写入 token" in labels
        assert "120" in labels
        assert "80.0%" in labels
        assert "主模型状态" in labels
        assert "调用工具" in labels
        assert "get_weather" in labels
        assert "累计概况" not in labels
        assert "当前角色" not in labels
        assert page._usage_card._right_label.isHidden()
    finally:
        page.deleteLater()


def test_overview_page_idle_activity_hides_stale_completion_text(qapp):
    runtime = type(
        "RuntimeStub",
        (),
        {
            "adapter": None,
            "config": minimal_root_config(),
            "providers": {},
            "provider_health": {},
            "_hist_len": 0,
            "important": None,
            "usage_stats": None,
            "model_activity": {
                "state": "idle",
                "text": "社交决策完成",
                "agent": "社交决策",
                "model": "deepseek-chat",
            },
        },
    )()
    page = OverviewPage(runtime)
    try:
        page._timer.stop()
        page.refresh()
        labels = "\n".join(label.text() for label in page.findChildren(QtWidgets.QLabel))

        assert page._timer.interval() == 1000
        assert "空闲" in labels
        assert "社交决策完成" not in labels
    finally:
        page.deleteLater()


def test_overview_cards_keep_content_driven_height(qapp):
    page = OverviewPage(
        type(
            "RuntimeStub",
            (),
            {
                "adapter": None,
                "config": minimal_root_config(),
                "providers": {},
                "provider_health": {},
                "_hist_len": 0,
                "important": None,
                "usage_stats": None,
                "model_activity": {},
            },
        )()
    )
    try:
        cards = [
            page._activity_card,
            page._providers_card,
            page._adapter_card,
            page._usage_card,
        ]
        for card in cards:
            assert card.sizePolicy().verticalPolicy() == QtWidgets.QSizePolicy.Policy.Preferred
    finally:
        page.deleteLater()
