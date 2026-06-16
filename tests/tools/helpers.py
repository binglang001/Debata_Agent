"""Shared helpers for tool tests."""

from __future__ import annotations

from typing import Any


def _assert_tool_result_envelope(result: dict[str, Any], tool_name: str) -> None:
    assert result["tool"] == tool_name
    assert result["result_format"] == "structured_json"
    assert "ok" in result
    assert "status" in result
    assert isinstance(result["brief"], str)
    assert result["brief"].strip()


def _make_config(
    *,
    memory_mode="file",
    vision_enabled=False,
    web_search_enabled=False,
    weather_enabled=False,
    persona_management_enabled=False,
    energy_mode="disabled",
    satiety_mode="disabled",
):
    """构造最小合法 RootConfig。"""
    from app_config.schema import (
        AgentConfig,
        AgentsConfig,
        FeaturesConfig,
        LongTermMemoryConfig,
        PersonaManagementConfig,
        ProviderConfig,
        RootConfig,
        VisionFeatureConfig,
        WeatherFeatureConfig,
        WebSearchFeatureConfig,
    )

    return RootConfig(
        providers={
            "deepseek": ProviderConfig(
                preset="deepseek", api_key_id="k1"
            )
        },
        agents=AgentsConfig(
            chat=AgentConfig(provider="deepseek", model="deepseek-chat"),
        ),
        features=FeaturesConfig(
            vision=VisionFeatureConfig(
                enabled=vision_enabled,
                provider="deepseek" if vision_enabled else None,
            ),
            web_search=WebSearchFeatureConfig(enabled=web_search_enabled),
            weather=WeatherFeatureConfig(
                enabled=weather_enabled,
                api_key_id="fake_qweather" if weather_enabled else None,
                host="devapi.qweather.com",
            ),
            long_term_memory=LongTermMemoryConfig(mode=memory_mode),
        ),
        persona_management=PersonaManagementConfig(
            enabled=persona_management_enabled,
            physiology={
                "energy": {"mode": energy_mode},
                "satiety": {"mode": satiety_mode},
            },
        ),
    )




def _assert_no_title(node):
    if isinstance(node, dict):
        assert "title" not in node, f"残留 title: {node}"
        for v in node.values():
            _assert_no_title(v)
    elif isinstance(node, list):
        for v in node:
            _assert_no_title(v)
