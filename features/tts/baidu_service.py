"""百度短文本在线语音合成 REST API。"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from features.tts import ITTSService, TTSError

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
_TTS_URL = "https://tsn.baidu.com/text2audio"


class BaiduTTSService(ITTSService):
    """百度语音合成 —— GET text2audio，返回 mp3。"""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        per: int = 0,  # 0=女声 1=男声 3=度逍遥 4=度丫丫
        spd: int = 5,
        pit: int = 5,
        vol: int = 5,
        aue: int = 3,  # 3=mp3
        timeout: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._secret_key = secret_key
        self._per = per
        self._spd = spd
        self._pit = pit
        self._vol = vol
        self._aue = aue
        self._timeout = timeout
        self._token: str | None = None
        self._client: httpx.AsyncClient | None = None

    async def warmup(self) -> None:
        """预取 access_token。"""
        await self._ensure_token()

    async def _ensure_token(self) -> str:
        if self._token is not None:
            return self._token
        self._client = httpx.AsyncClient(timeout=self._timeout)
        try:
            resp = await self._client.post(
                _TOKEN_URL,
                params={
                    "grant_type": "client_credentials",
                    "client_id": self._api_key,
                    "client_secret": self._secret_key,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data.get("access_token")
            if not self._token:
                raise TTSError(f"百度 access_token 获取失败: {data}")
            return self._token
        except httpx.HTTPStatusError as e:
            raise TTSError(f"百度鉴权失败: {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise TTSError(f"百度网络错误: {e}") from e

    async def synthesize(
        self,
        text: str,
        *,
        reference_audio: str | Path | None = None,
        prompt: str = "",
    ) -> Path:
        """合成语音，返回 mp3 文件路径。workspace/.run/voice_{ts}.mp3"""
        token = await self._ensure_token()
        assert self._client is not None

        # 截断过长的文本（百度限制约 200 字符/次）
        if len(text) > 200:
            text = text[:200]

        params = {
            "tex": quote(text),
            "tok": token,
            "cuid": "debata",
            "ctp": "1",
            "lan": "zh",
            "spd": str(self._spd),
            "pit": str(self._pit),
            "vol": str(self._vol),
            "per": str(self._per),
            "aue": str(self._aue),
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())

        try:
            resp = await self._client.get(f"{_TTS_URL}?{query}")
            ct = resp.headers.get("content-type", "")
            if "audio" in ct:
                workspace_run = Path("data/workspace/.run")
                workspace_run.mkdir(parents=True, exist_ok=True)
                ts = int(time.time() * 1000)
                out = workspace_run / f"voice_{ts}.mp3"
                out.write_bytes(resp.content)
                logger.info(f"百度 TTS 完成: {out}")
                return out
            # 错误时返回 JSON
            try:
                err = resp.json()
                raise TTSError(f"百度合成失败: {err}")
            except (ValueError, KeyError):
                raise TTSError(f"百度合成失败: HTTP {resp.status_code}")
        except httpx.RequestError as e:
            raise TTSError(f"百度网络错误: {e}") from e

    async def aclose(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        self._token = None
