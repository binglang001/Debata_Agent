"""UI 配置测试共享 helper。"""

from __future__ import annotations

from app_config.schema import AgentConfig, AgentsConfig, NapCatAdapterConfig, ProviderConfig, RootConfig


def _minimal_root_config() -> RootConfig:
    return RootConfig(
        providers={
            "ds": ProviderConfig(
                preset="deepseek",
                display_name="DeepSeek",
                api_key_id="ds_key",
            ),
        },
        adapters={"default": NapCatAdapterConfig()},
        agents=AgentsConfig(chat=AgentConfig(provider="ds", model="deepseek-chat")),
    )
