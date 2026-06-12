"""Provider 轻量健康检测回归测试。"""

from __future__ import annotations

import httpx
import pytest

from providers.health import probe_embedding_endpoint, probe_provider_endpoint


class _DummyResponse:
    def __init__(self, status_code: int, data=None, text: str = "") -> None:
        self.status_code = status_code
        self._data = data
        self.text = text

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


@pytest.mark.asyncio
async def test_probe_openai_compat_success(monkeypatch):
    async def fake_post(self, url, headers=None, json=None):
        assert url == "https://api.test/v1/chat/completions"
        assert json["max_tokens"] == 1
        return _DummyResponse(200, {"choices": [{"message": {"content": "x"}}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = await probe_provider_endpoint(
        protocol="openai_compat",
        base_url="https://api.test/v1",
        api_key="sk",
        model="m",
    )

    assert result.status == "ok"


@pytest.mark.asyncio
async def test_probe_openai_compat_auth_error(monkeypatch):
    async def fake_post(self, url, headers=None, json=None):
        return _DummyResponse(401, {"error": "bad"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = await probe_provider_endpoint(
        protocol="openai_compat",
        base_url="https://api.test/v1",
        api_key="bad",
        model="m",
    )

    assert result.status == "error"
    assert "鉴权" in result.message


@pytest.mark.asyncio
async def test_probe_embedding_endpoint_uses_embeddings_api(monkeypatch):
    async def fake_post(self, url, headers=None, json=None):
        assert url == "https://open.bigmodel.cn/api/paas/v4/embeddings"
        assert json == {"model": "embedding-3", "input": ["test"]}
        return _DummyResponse(200, {"data": [{"embedding": [0.1, 0.2]}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = await probe_embedding_endpoint(
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key="sk",
        model="embedding-3",
    )

    assert result.status == "ok"
