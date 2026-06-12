"""和风天气 service 回归测试。"""

from __future__ import annotations

import pytest

from features.weather.weather_service import WeatherService


class _FakeResponse:
    def __init__(self, status_code: int, data: dict | None = None) -> None:
        self.status_code = status_code
        self._data = data

    def json(self) -> dict:
        if self._data is None:
            raise ValueError("not json")
        return self._data


class _FakeClient:
    calls: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def get(self, url: str, params: dict):
        self.calls.append(url)
        if url.endswith("/geo/v2/city/lookup"):
            return _FakeResponse(
                200,
                {
                    "code": "200",
                    "location": [{"id": "101010100", "name": "北京", "adm1": "北京"}],
                },
            )
        if url.endswith("/v7/weather/now"):
            return _FakeResponse(
                200,
                {"code": "200", "now": {"text": "晴", "temp": "20", "windScale": "1", "humidity": "50"}},
            )
        raise AssertionError(url)


@pytest.mark.asyncio
async def test_weather_uses_configured_host_for_geo_lookup(monkeypatch):
    import features.weather.weather_service as mod

    _FakeClient.calls = []
    monkeypatch.setattr(mod.httpx, "AsyncClient", _FakeClient)

    service = WeatherService(api_key="k", host="example.qweatherapi.com")
    result = await service.query("北京")

    assert "北京 实时：晴" in result
    assert _FakeClient.calls[0] == "https://example.qweatherapi.com/geo/v2/city/lookup"


@pytest.mark.asyncio
async def test_weather_reports_http_error_without_json_parse_warning(monkeypatch):
    import features.weather.weather_service as mod

    class Client(_FakeClient):
        async def get(self, url: str, params: dict):
            return _FakeResponse(404, None)

    monkeypatch.setattr(mod.httpx, "AsyncClient", Client)
    service = WeatherService(api_key="k", host="example.qweatherapi.com")

    result = await service.query("北京")

    assert result == "地理编码 API 错误：HTTP 404"
