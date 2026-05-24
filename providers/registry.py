"""Provider 实例的注册中心与构造工厂。

启动流程：
    1. load_all_presets() —— 扫描 providers/presets/*/preset.yaml
    2. 对 config.providers 中每条记录：
        - 如指定 preset，从预设取 protocol/base_url
        - 否则用用户填的 protocol/base_url
        - 通过 PROTOCOL_REGISTRY[protocol] 构造对应类
    3. 注册到 ProviderRegistry，按 ID 查找

Provider 实例是异步资源（含 HTTP 连接池），需在停机时调用 close_all() 优雅关闭。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable

from app_config.schema import ProviderConfig
from app_config.secrets import SecretsManager

from .base import IProvider, ProviderError
from .presets_loader import ProviderPreset, load_all_presets
from .protocols import AnthropicProvider, OpenAICompatProvider

logger = logging.getLogger(__name__)


# 协议名 → 构造函数。新增协议时在此注册。
ProtocolFactory = Callable[..., IProvider]

PROTOCOL_REGISTRY: dict[str, ProtocolFactory] = {
    "openai_compat": OpenAICompatProvider,
    "anthropic": AnthropicProvider,
}


def register_protocol(protocol_name: str, factory: ProtocolFactory) -> None:
    """新增一个协议实现。"""
    if protocol_name in PROTOCOL_REGISTRY:
        raise ValueError(f"协议 {protocol_name} 已注册")
    PROTOCOL_REGISTRY[protocol_name] = factory


def known_protocols() -> list[str]:
    return list(PROTOCOL_REGISTRY.keys())


# ============================================================
# 构造逻辑
# ============================================================


def build_provider(
    provider_id: str,
    cfg: ProviderConfig,
    secrets: SecretsManager,
    presets: dict[str, ProviderPreset],
) -> IProvider:
    """根据 ProviderConfig 构造一个 IProvider 实例。"""

    # 解析协议、base_url、reasoning_style
    preset: ProviderPreset | None = None
    if cfg.preset:
        preset = presets.get(cfg.preset.lower())
        if preset is None:
            raise ProviderError(
                f"未知预设: {cfg.preset}。已加载的预设: {sorted(presets.keys())}"
            )

    protocol = cfg.protocol or (preset.protocol if preset else None)
    if protocol is None:
        raise ProviderError(f"provider {provider_id} 既无 preset 也无 protocol")
    base_url = cfg.base_url or (preset.base_url if preset else None)
    if not base_url:
        raise ProviderError(f"provider {provider_id} 缺 base_url")

    factory = PROTOCOL_REGISTRY.get(protocol)
    if factory is None:
        raise ProviderError(
            f"未知协议: {protocol}。已注册: {list(PROTOCOL_REGISTRY.keys())}"
        )

    api_key: str | None = None
    if cfg.api_key_id:
        api_key = secrets.get(cfg.api_key_id)
        if api_key is None:
            logger.warning(
                f"Provider {provider_id} 引用的 api_key_id={cfg.api_key_id} "
                f"在 secrets 中找不到，将以空密钥构造"
            )

    kwargs: dict[str, Any] = {
        "name": provider_id,
        "base_url": base_url,
        "api_key": api_key,
        "timeout": cfg.timeout,
        "extra_headers": dict(cfg.extra_headers),
    }

    # openai_compat 专属参数
    if protocol == "openai_compat" and preset is not None:
        kwargs["reasoning_style"] = preset.reasoning_style

    return factory(**kwargs)


# ============================================================
# 注册中心
# ============================================================


class ProviderRegistry:
    """运行时管理所有 IProvider 实例。"""

    def __init__(self) -> None:
        self._providers: dict[str, IProvider] = {}
        self._presets: dict[str, ProviderPreset] = {}

    def load_presets(self, presets_dir: Path) -> None:
        self._presets = load_all_presets(presets_dir)

    @property
    def presets(self) -> dict[str, ProviderPreset]:
        return self._presets

    def register(self, provider: IProvider) -> None:
        if provider.name in self._providers:
            raise ValueError(f"Provider 名重复: {provider.name}")
        self._providers[provider.name] = provider
        logger.info(f"Provider 已注册: {provider.name} ({type(provider).__name__})")

    def get(self, name: str) -> IProvider:
        if name not in self._providers:
            raise KeyError(
                f"未知 provider: {name}。已注册: {list(self._providers.keys())}"
            )
        return self._providers[name]

    def has(self, name: str) -> bool:
        return name in self._providers

    def list_names(self) -> list[str]:
        return list(self._providers.keys())

    def build_from_config(
        self,
        providers_cfg: dict[str, ProviderConfig],
        secrets: SecretsManager,
    ) -> None:
        """根据配置批量构造所有 provider 实例并注册。"""
        for pid, cfg in providers_cfg.items():
            try:
                provider = build_provider(pid, cfg, secrets, self._presets)
                self.register(provider)
            except Exception as e:
                logger.error(f"构造 provider {pid} 失败: {e}")
                raise

    async def close_all(self) -> None:
        """关闭所有 provider 的底层连接。"""
        if not self._providers:
            return
        await asyncio.gather(
            *(p.aclose() for p in self._providers.values()),
            return_exceptions=True,
        )
        logger.info(f"已关闭 {len(self._providers)} 个 provider")
