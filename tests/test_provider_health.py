"""Provider 轻量健康检测回归测试。"""

from __future__ import annotations

import httpx
import pytest

from providers.base import CompletionResult, ProviderAuthError
from providers.health import (
    probe_embedding_endpoint,
    probe_provider_endpoint,
    probe_provider_instance,
)


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
    clients = []

    async def fake_post(self, url, headers=None, json=None):
        assert url == "https://api.test/v1/chat/completions"
        assert json["max_tokens"] == 1
        return _DummyResponse(200, {"choices": [{"message": {"content": "x"}}]})

    original_init = httpx.AsyncClient.__init__

    def fake_init(self, *args, **kwargs):
        clients.append(kwargs)
        original_init(self, *args, **kwargs)

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setattr(httpx.AsyncClient, "__init__", fake_init)
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = await probe_provider_endpoint(
        protocol="openai_compat",
        base_url="https://api.test/v1",
        api_key="sk",
        model="m",
    )

    assert result.status == "ok"
    assert clients[-1]["proxy"] == "http://127.0.0.1:7897"


@pytest.mark.asyncio
async def test_probe_endpoint_omits_proxy_when_env_is_empty(monkeypatch):
    clients = []

    async def fake_post(self, url, headers=None, json=None):
        return _DummyResponse(200, {"choices": [{"message": {"content": "x"}}]})

    original_init = httpx.AsyncClient.__init__

    def fake_init(self, *args, **kwargs):
        clients.append(kwargs)
        original_init(self, *args, **kwargs)

    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", fake_init)
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = await probe_provider_endpoint(
        protocol="openai_compat",
        base_url="https://api.test/v1",
        api_key="sk",
        model="m",
    )

    assert result.status == "ok"
    assert "proxy" not in clients[-1]


class _FakeProvider:
    base_url = "https://api.test/v1"
    api_key = "sk"

    def __init__(self) -> None:
        self.calls = []

    async def chat_completion(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return CompletionResult(content="x")


@pytest.mark.asyncio
async def test_probe_provider_instance_uses_real_provider_call():
    provider = _FakeProvider()

    result = await probe_provider_instance(provider, model="chat-model", timeout_seconds=3.0)

    assert result.status == "ok"
    messages, kwargs = provider.calls[0]
    assert messages == [{"role": "user", "content": "hi"}]
    assert kwargs["model"] == "chat-model"
    assert kwargs["stream"] is False
    assert kwargs["max_tokens"] == 1
    assert kwargs["timeout"] == 3.0


@pytest.mark.asyncio
async def test_probe_provider_instance_maps_provider_errors():
    class BadProvider(_FakeProvider):
        async def chat_completion(self, messages, **kwargs):
            raise ProviderAuthError("bad key")

    result = await probe_provider_instance(BadProvider(), model="chat-model")

    assert result.status == "error"
    assert "鉴权" in result.message


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


@pytest.mark.asyncio
async def test_probe_volcengine_vision_embedding_uses_multimodal_api(monkeypatch):
    async def fake_post(self, url, headers=None, json=None):
        assert url == "https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal"
        assert json == {
            "model": "doubao-embedding-vision-251215",
            "input": [{"type": "text", "text": "test"}],
        }
        return _DummyResponse(200, {"data": [{"embedding": [0.1, 0.2]}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = await probe_embedding_endpoint(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key="sk",
        model="doubao-embedding-vision-251215",
    )

    assert result.status == "ok"


@pytest.mark.asyncio
async def test_probe_volcengine_vision_embedding_accepts_object_data(monkeypatch):
    async def fake_post(self, url, headers=None, json=None):
        assert url == "https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal"
        return _DummyResponse(200, {"data": {"embedding": [0.1, 0.2]}})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = await probe_embedding_endpoint(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key="sk",
        model="doubao-embedding-vision-251215",
    )

    assert result.status == "ok"
