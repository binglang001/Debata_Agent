"""和风天气 API service。

流程：
    1. 地理编码：城市名 → location_id（GET /geo/v2/city/lookup）
    2. 实时天气：GET /v7/weather/now
    3. 多日预报：GET /v7/weather/{N}d（按 days 选择 3d/7d）

注：和风天气的 geoapi 走固定域名 geoapi.qweather.com，
    实时/预报走用户配的 host（免费版 devapi.qweather.com 或商业版专属域名）。
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class WeatherService:
    """和风天气 API 客户端。"""

    GEO_HOST = "geoapi.qweather.com"

    def __init__(
        self,
        *,
        api_key: str,
        host: str = "devapi.qweather.com",
        timeout_seconds: float = 10.0,
    ) -> None:
        self.api_key = api_key
        self.host = host
        self.timeout_seconds = timeout_seconds

    async def query(self, city: str, days: int = 1) -> str:
        """查询城市天气。days=1 返回实时，>=2 返回实时 + 多日预报。"""
        if not city.strip():
            return "（城市名为空）"
        days = max(1, min(int(days), 7))

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            # 1. 地理编码
            try:
                geo_resp = await client.get(
                    f"https://{self.GEO_HOST}/v2/city/lookup",
                    params={"location": city, "key": self.api_key},
                )
                geo = geo_resp.json()
            except Exception as e:
                logger.warning(f"geo lookup 失败 city={city!r}: {e}")
                return f"地理编码失败：{type(e).__name__}: {e}"

            if geo.get("code") != "200" or not geo.get("location"):
                return f"未找到城市「{city}」（API code={geo.get('code')}）"

            loc_info = geo["location"][0]
            location_id = loc_info["id"]
            location_name = (
                f"{loc_info.get('adm1', '')} {loc_info.get('name', city)}"
            ).strip()

            # 2. 实时天气
            try:
                now_resp = await client.get(
                    f"https://{self.host}/v7/weather/now",
                    params={"location": location_id, "key": self.api_key},
                )
                now = now_resp.json()
            except Exception as e:
                logger.warning(f"实时天气查询失败 loc={location_name}: {e}")
                return f"实时天气查询失败：{type(e).__name__}: {e}"

            if now.get("code") != "200":
                return f"实时天气 API 错误：code={now.get('code')}"

            now_data = now.get("now", {})
            text_parts: list[str] = [
                f"{location_name} 实时：{now_data.get('text', '?')}，"
                f"{now_data.get('temp', '?')}°C，"
                f"{now_data.get('windDir', '')}{now_data.get('windScale', '?')}级，"
                f"湿度 {now_data.get('humidity', '?')}%"
            ]

            # 3. 多日预报（如需要）
            if days > 1:
                # 和风天气端点是 /v7/weather/{N}d，N 必须是 3、7、10、15、30
                # 免费版仅支持 3 和 7
                forecast_n = 3 if days <= 3 else 7
                try:
                    forecast_resp = await client.get(
                        f"https://{self.host}/v7/weather/{forecast_n}d",
                        params={"location": location_id, "key": self.api_key},
                    )
                    forecast = forecast_resp.json()
                except Exception as e:
                    text_parts.append(f"（预报查询失败：{type(e).__name__}: {e}）")
                else:
                    if forecast.get("code") == "200":
                        text_parts.append(f"\n未来 {forecast_n} 天：")
                        for day in forecast.get("daily", []):
                            text_parts.append(
                                f"  {day.get('fxDate', '?')}：白天{day.get('textDay', '?')}，"
                                f"夜间{day.get('textNight', '?')}，"
                                f"{day.get('tempMin', '?')}~{day.get('tempMax', '?')}°C"
                            )
                    else:
                        text_parts.append(f"（预报 API 错误：code={forecast.get('code')}）")

            return "\n".join(text_parts)
