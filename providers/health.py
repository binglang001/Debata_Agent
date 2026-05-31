"""Provider 连通性探针。

这里不用完整 SDK，只发一次低成本 HTTP 请求，避免设置页/向导测试时
因为 SDK 初始化、流式逻辑或大超时让 UI 体感卡顿。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

import httpx

HealthStatus = Literal["checking", "ok", "error"]


@dataclass(slots=True)
class ProviderHealth:
    status: HealthStatus
    message: str
    latency_ms: int = 0


def _timeout(seconds: float) -> httpx.Timeout:
    return httpx.Timeout(seconds, connect=min(3.0, seconds), read=seconds)


def _short_error(resp: httpx.Response) -> str:
    text = resp.text.strip().replace("\n", " ")
    if len(text) > 180:
        text = text[:180] + "..."
    return text


async def probe_provider_endpoint(
    *,
    protocol: str,
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: float = 8.0,
    extra_headers: dict[str, str] | None = None,
) -> ProviderHealth:
    """探测 provider + model 是否可用。"""
    if not api_key:
        return ProviderHealth("error", "缺 API 密钥")
    if not base_url:
        return ProviderHealth("error", "缺 Base URL")
    if not model:
        return ProviderHealth("error", "缺模型 ID")

    protocol = protocol or "openai_compat"
    start = time.perf_counter()
    try:
        if protocol == "anthropic":
            result = await _probe_anthropic(
                base_url=base_url,
                api_key=api_key,
                model=model,
                timeout_seconds=timeout_seconds,
                extra_headers=extra_headers or {},
            )
        else:
            result = await _probe_openai_compat(
                base_url=base_url,
                api_key=api_key,
                model=model,
                timeout_seconds=timeout_seconds,
                extra_headers=extra_headers or {},
            )
    except httpx.TimeoutException:
        return ProviderHealth("error", "请求超时")
    except httpx.TransportError as e:
        return ProviderHealth("error", f"网络不通：{e}")
    except Exception as e:  # noqa: BLE001
        return ProviderHealth("error", f"检测失败：{e}")

    result.latency_ms = int((time.perf_counter() - start) * 1000)
    return result


async def probe_embedding_endpoint(
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: float = 8.0,
    extra_headers: dict[str, str] | None = None,
) -> ProviderHealth:
    """探测 OpenAI 兼容 /embeddings 端点是否可用。"""
    if not api_key:
        return ProviderHealth("error", "缺 API 密钥")
    if not base_url:
        return ProviderHealth("error", "缺 Base URL")
    if not model:
        return ProviderHealth("error", "缺 Embedding 模型 ID")

    start = time.perf_counter()
    try:
        result = await _probe_openai_compat_embedding(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
            extra_headers=extra_headers or {},
        )
    except httpx.TimeoutException:
        return ProviderHealth("error", "请求超时")
    except httpx.TransportError as e:
        return ProviderHealth("error", f"网络不通：{e}")
    except Exception as e:  # noqa: BLE001
        return ProviderHealth("error", f"检测失败：{e}")

    result.latency_ms = int((time.perf_counter() - start) * 1000)
    return result


async def probe_provider_instance(
    provider,
    *,
    model: str,
    protocol: str | None = None,
    timeout_seconds: float = 8.0,
) -> ProviderHealth:
    """从已构造 provider 实例做探测。"""
    proto = protocol
    if proto is None:
        if type(provider).__name__ == "AnthropicProvider":
            proto = "anthropic"
        else:
            proto = "openai_compat"
    return await probe_provider_endpoint(
        protocol=proto,
        base_url=getattr(provider, "base_url", ""),
        api_key=getattr(provider, "api_key", ""),
        model=model,
        timeout_seconds=timeout_seconds,
        extra_headers=getattr(provider, "extra_headers", {}) or {},
    )


async def probe_embedding_provider_instance(
    provider,
    *,
    model: str,
    api_key: str | None = None,
    timeout_seconds: float = 8.0,
) -> ProviderHealth:
    """从已构造 provider 实例探测 /embeddings。"""
    return await probe_embedding_endpoint(
        base_url=getattr(provider, "base_url", ""),
        api_key=api_key or getattr(provider, "api_key", "") or "",
        model=model,
        timeout_seconds=timeout_seconds,
        extra_headers=getattr(provider, "extra_headers", {}) or {},
    )


async def _probe_openai_compat(
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: float,
    extra_headers: dict[str, str],
) -> ProviderHealth:
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        **extra_headers,
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0,
        "max_tokens": 1,
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=_timeout(timeout_seconds)) as client:
        resp = await client.post(url, headers=headers, json=payload)
    if resp.status_code in (401, 403):
        return ProviderHealth("error", "鉴权失败，请检查 API 密钥")
    if resp.status_code == 402:
        return ProviderHealth("error", "账户余额不足或未开通计费")
    if resp.status_code == 404:
        return ProviderHealth("error", "模型或接口不存在")
    if resp.status_code == 429:
        return ProviderHealth("error", "请求被限流")
    if resp.status_code >= 400:
        return ProviderHealth("error", f"HTTP {resp.status_code}: {_short_error(resp)}")
    try:
        data = resp.json()
    except ValueError:
        return ProviderHealth("error", "响应不是合法 JSON")
    if not isinstance(data, dict) or "choices" not in data:
        return ProviderHealth("error", "响应格式异常")
    return ProviderHealth("ok", "可用")


async def _probe_openai_compat_embedding(
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: float,
    extra_headers: dict[str, str],
) -> ProviderHealth:
    url = base_url.rstrip("/") + "/embeddings"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        **extra_headers,
    }
    payload = {
        "model": model,
        "input": ["test"],
    }
    async with httpx.AsyncClient(timeout=_timeout(timeout_seconds)) as client:
        resp = await client.post(url, headers=headers, json=payload)
    if resp.status_code in (401, 403):
        return ProviderHealth("error", "鉴权失败，请检查 API 密钥")
    if resp.status_code == 402:
        return ProviderHealth("error", "账户余额不足或未开通计费")
    if resp.status_code == 404:
        return ProviderHealth("error", "Embedding 模型或接口不存在")
    if resp.status_code == 429:
        return ProviderHealth("error", "请求被限流")
    if resp.status_code >= 400:
        return ProviderHealth("error", f"HTTP {resp.status_code}: {_short_error(resp)}")
    try:
        data = resp.json()
    except ValueError:
        return ProviderHealth("error", "响应不是合法 JSON")
    items = data.get("data") if isinstance(data, dict) else None
    if not items or not isinstance(items, list):
        return ProviderHealth("error", "Embedding 响应格式异常")
    first = items[0]
    if not isinstance(first, dict) or not isinstance(first.get("embedding"), list):
        return ProviderHealth("error", "Embedding 响应缺少向量")
    return ProviderHealth("ok", "可用")


async def _probe_anthropic(
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: float,
    extra_headers: dict[str, str],
) -> ProviderHealth:
    url = base_url.rstrip("/") + "/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
        **extra_headers,
    }
    payload = {
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }
    async with httpx.AsyncClient(timeout=_timeout(timeout_seconds)) as client:
        resp = await client.post(url, headers=headers, json=payload)
    if resp.status_code in (401, 403):
        return ProviderHealth("error", "鉴权失败，请检查 API 密钥")
    if resp.status_code == 402:
        return ProviderHealth("error", "账户余额不足或未开通计费")
    if resp.status_code == 404:
        return ProviderHealth("error", "模型或接口不存在")
    if resp.status_code == 429:
        return ProviderHealth("error", "请求被限流")
    if resp.status_code >= 400:
        return ProviderHealth("error", f"HTTP {resp.status_code}: {_short_error(resp)}")
    try:
        data = resp.json()
    except ValueError:
        return ProviderHealth("error", "响应不是合法 JSON")
    if not isinstance(data, dict) or "content" not in data:
        return ProviderHealth("error", "响应格式异常")
    return ProviderHealth("ok", "可用")
