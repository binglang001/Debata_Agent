"""模型能力查询。

读取 providers/model_capabilities.yaml 和 providers/presets，用确定性规则回答：
- 某 provider/model 是否支持 vision / embedding / tool_call / reasoning
- 某 provider 推荐的 chat / vision / embedding 模型是什么

这里不联网，不猜测未知能力。远程 /models 只提供 ID 时，调用方可用本模块补齐已知能力。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class ModelCapability:
    id: str
    display_name: str = ""
    capabilities: set[str] = field(default_factory=set)
    context_length: int = 0
    status: str = ""
    notes: str = ""

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities


@dataclass(slots=True)
class ProviderCapability:
    id: str
    display_name: str = ""
    protocol: str = "openai_compat"
    base_url: str = ""
    has_embedding: bool = False
    vision_reuse: bool = False
    models: dict[str, ModelCapability] = field(default_factory=dict)
    embedding_models: dict[str, ModelCapability] = field(default_factory=dict)

    def all_models(self) -> dict[str, ModelCapability]:
        out = dict(self.models)
        out.update(self.embedding_models)
        return out


_CACHE: dict[Path, dict[str, ProviderCapability]] = {}


def default_capabilities_path() -> Path:
    return Path(__file__).resolve().parent / "model_capabilities.yaml"


def load_model_capabilities(
    path: Path | None = None,
    *,
    force_reload: bool = False,
) -> dict[str, ProviderCapability]:
    target = (path or default_capabilities_path()).resolve()
    if not force_reload and target in _CACHE:
        return dict(_CACHE[target])
    if not target.exists():
        _CACHE[target] = {}
        return {}
    data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    providers_raw = data.get("providers", {}) if isinstance(data, dict) else {}
    out: dict[str, ProviderCapability] = {}
    for pid, raw in providers_raw.items():
        if not isinstance(raw, dict):
            continue
        provider = ProviderCapability(
            id=str(pid),
            display_name=str(raw.get("display_name", pid)),
            protocol=str(raw.get("protocol", "openai_compat")),
            base_url=str(raw.get("base_url", "")),
            has_embedding=bool(raw.get("has_embedding", False)),
            vision_reuse=bool(raw.get("vision_reuse", False)),
        )
        provider.models.update(_parse_models(raw.get("models", [])))
        provider.embedding_models.update(
            _parse_models(raw.get("embedding_models", []), default_capability="embedding")
        )
        provider.models.update(_parse_models(raw.get("deprecated_models", [])))
        if provider.embedding_models:
            provider.has_embedding = True
        out[provider.id] = provider
    _CACHE[target] = dict(out)
    return out


def model_capability(provider_id: str, model_id: str) -> ModelCapability | None:
    provider = load_model_capabilities().get(provider_id)
    if provider is None:
        return None
    models = provider.all_models()
    if model_id in models:
        return models[model_id]
    # OpenRouter / SiliconFlow / Together 这类 provider 常带上游命名空间。
    # 若完整 ID 未命中，只在尾部模型名完全匹配时返回，避免误判。
    tail = model_id.rsplit("/", 1)[-1]
    for known_id, info in models.items():
        if known_id.rsplit("/", 1)[-1].lower() == tail.lower():
            return info
    return None


def model_supports(provider_id: str, model_id: str, capability: str) -> bool:
    info = model_capability(provider_id, model_id)
    return bool(info and info.supports(capability))


def recommended_model(provider_id: str, capability: str = "chat") -> ModelCapability | None:
    provider = load_model_capabilities().get(provider_id)
    if provider is None:
        return None
    pool = provider.embedding_models if capability == "embedding" else provider.models
    recommended = [
        model for model in pool.values()
        if model.supports(capability) and model.status == "recommended"
    ]
    if recommended:
        return recommended[0]
    active = [model for model in pool.values() if model.supports(capability)]
    return active[0] if active else None


def recommended_provider(capability: str = "chat") -> ProviderCapability | None:
    """按项目推荐顺序返回支持指定能力的 provider。"""
    providers = load_model_capabilities()
    for pid in _provider_preference(capability):
        provider = providers.get(pid)
        if provider and recommended_model(pid, capability):
            return provider
    for provider in providers.values():
        if recommended_model(provider.id, capability):
            return provider
    return None


def known_model_ids(provider_id: str, *, capability: str | None = None) -> list[str]:
    provider = load_model_capabilities().get(provider_id)
    if provider is None:
        return []
    models = provider.all_models()
    if capability:
        return [mid for mid, info in models.items() if info.supports(capability)]
    return list(models.keys())


def capability_badges(provider_id: str, model_id: str) -> str:
    info = model_capability(provider_id, model_id)
    if info is None:
        return ""
    labels = []
    for cap, text in (
        ("tool_call", "工具"),
        ("reasoning", "推理"),
        ("vision", "视觉"),
        ("embedding", "向量"),
    ):
        if info.supports(cap):
            labels.append(text)
    if info.context_length:
        labels.append(f"{_compact_context(info.context_length)}")
    return " / ".join(labels)


def _parse_models(
    raw_models: Any,
    *,
    default_capability: str | None = None,
) -> dict[str, ModelCapability]:
    out: dict[str, ModelCapability] = {}
    if not isinstance(raw_models, list):
        return out
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id", "")).strip()
        if not mid:
            continue
        capabilities = set(item.get("capabilities", []) or [])
        if default_capability:
            capabilities.add(default_capability)
        out[mid] = ModelCapability(
            id=mid,
            display_name=str(item.get("display_name", mid)),
            capabilities=capabilities,
            context_length=int(item.get("context_length", 0) or 0),
            status=str(item.get("status", "")),
            notes=str(item.get("notes", "")),
        )
    return out


def _compact_context(value: int) -> str:
    if value >= 1_000_000:
        return f"{value // 1_000_000}M上下文"
    if value >= 1000:
        return f"{value // 1000}K上下文"
    return f"{value}上下文"


def _provider_preference(capability: str) -> tuple[str, ...]:
    if capability == "embedding":
        return (
            "volcengine",
            "openai",
            "qwen",
            "glm",
            "gemini",
            "siliconflow",
            "together",
        )
    if capability == "vision":
        return (
            "volcengine",
            "glm",
            "qwen",
            "gemini",
            "openai",
            "anthropic",
            "xai",
            "openrouter",
        )
    if capability == "chat":
        return (
            "deepseek",
            "volcengine",
            "anthropic",
            "openai",
            "qwen",
            "glm",
            "gemini",
        )
    return tuple(load_model_capabilities().keys())


__all__ = [
    "ModelCapability",
    "ProviderCapability",
    "capability_badges",
    "known_model_ids",
    "load_model_capabilities",
    "model_capability",
    "model_supports",
    "recommended_provider",
    "recommended_model",
]
