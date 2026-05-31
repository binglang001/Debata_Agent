"""远程获取 provider 可用模型列表。

openai_compat 协议调 GET {base_url}/models（去 /v{N} 后缀），
anthropic 协议返回已知模型列表（官方无 /models 端点）。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .base import ProviderError

logger = logging.getLogger(__name__)

# Anthropic 已知模型（无 /models 端点，手动维护）
_ANTHROPIC_KNOWN = [
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "claude-opus-4-7",
    "claude-opus-4-5",
    "claude-sonnet-4-5",
    "claude-haiku-4-5-20251001",
    "claude-opus-4-1",
    "claude-sonnet-4",
    "claude-3.5-haiku",
]

# 需过滤的 irrelevant 模型 ID 模式
_SKIP_PREFIXES = ("dall-e", "whisper", "tts", "o1", "babbage", "davinci", "omni")


def _should_skip(model_id: str) -> bool:
    lower = model_id.lower()
    return any(lower.startswith(p) for p in _SKIP_PREFIXES)


async def fetch_model_list(
    base_url: str,
    api_key: str,
    protocol: str,
    timeout: float = 15.0,
) -> list[str]:
    """从 provider 获取模型 ID 列表。

    Args:
        base_url: 已规范化的 base_url（含 /v{N} 后缀）
        api_key: API 密钥
        protocol: "openai_compat" 或 "anthropic"
        timeout: 请求超时秒数

    Returns:
        模型 ID 列表，按字母排序

    Raises:
        ProviderError: 网络错误 / 鉴权失败 / 响应格式异常
    """
    if protocol == "anthropic":
        return list(_ANTHROPIC_KNOWN)

    if protocol != "openai_compat":
        raise ProviderError(f"协议 {protocol!r} 不支持获取模型列表")

    # 去掉 /v{N} 后缀得到 API 根路径
    import re
    api_root = re.sub(r"/v\d+$", "", base_url)
    url = f"{api_root}/models"

    headers: dict[str, str] = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    proxy = None
    try:
        import os
        proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or None
    except Exception:
        pass

    async with httpx.AsyncClient(proxy=proxy, timeout=timeout) as client:
        try:
            resp = await client.get(url, headers=headers)
        except httpx.TransportError as e:
            raise ProviderError(f"网络不通：{e}") from e
        except httpx.TimeoutException as e:
            raise ProviderError("请求超时，请检查网络或换一个更短的超时") from e

        if resp.status_code == 401 or resp.status_code == 403:
            raise ProviderError("鉴权失败，请检查 API 密钥是否正确")
        if resp.status_code == 404:
            raise ProviderError("该 provider 不支持 /models 端点，请手动输入模型 ID")
        if resp.status_code != 200:
            raise ProviderError(f"服务器返回 HTTP {resp.status_code}")

        try:
            data: dict[str, Any] = resp.json()
        except Exception as e:
            raise ProviderError("响应不是合法 JSON") from e

        # 尝试多种响应格式
        models_raw: list[dict[str, Any]] = []
        if "data" in data and isinstance(data["data"], list):
            models_raw = data["data"]
        elif "models" in data and isinstance(data["models"], list):
            models_raw = data["models"]
        elif isinstance(data, list):
            models_raw = data
        else:
            raise ProviderError("无法解析模型列表（未知的响应格式）")

        ids: list[str] = []
        for item in models_raw:
            if not isinstance(item, dict):
                continue
            mid = item.get("id") or item.get("model") or ""
            if not mid or not isinstance(mid, str):
                continue
            if _should_skip(mid):
                continue
            ids.append(mid)

        if not ids:
            raise ProviderError("模型列表为空，请手动输入模型 ID")

        ids.sort()
        logger.info(f"从 {url} 获取到 {len(ids)} 个模型")
        return ids
