"""科大讯飞在线语音合成 REST API。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from pathlib import Path

import httpx

from features.tts import ITTSService, TTSError

logger = logging.getLogger(__name__)

_TTS_URL = "https://tts-api.xfyun.cn/v2/tts"
_EXT = {0: "wav", 1: "mp3", 2: "mp3"}  # aue → 扩展名


class iFlytekTTSService(ITTSService):
    """讯飞语音合成 —— REST POST 返回音频二进制。"""

    def __init__(
        self,
        app_id: str,
        api_key: str,
        api_secret: str,
        voice_name: str = "xiaoyan",
        speed: int = 50,
        volume: int = 50,
        pitch: int = 50,
        aue: int = 1,  # 0=wav 1=mp3(lame)
        timeout: float = 15.0,
    ) -> None:
        self._app_id = app_id
        self._api_key = api_key
        self._api_secret = api_secret
        self._voice_name = voice_name
        self._speed = speed
        self._volume = volume
        self._pitch = pitch
        self._aue = aue
        self._timeout = timeout

    async def warmup(self) -> None:
        pass

    def _build_headers(self) -> dict[str, str]:
        """构造 HMAC-SHA256 签名鉴权 header。"""
        host = "tts-api.xfyun.cn"
        date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())

        signature_origin = f"host: {host}\ndate: {date}\nPOST /v2/tts HTTP/1.1"
        signature = base64.b64encode(
            hmac.new(
                self._api_secret.encode(),
                signature_origin.encode(),
                hashlib.sha256,
            ).digest()
        ).decode()

        authorization_origin = (
            f'api_key="{self._api_key}",'
            f'algorithm="hmac-sha256",'
            f'headers="host date request-line",'
            f'signature="{signature}"'
        )
        authorization = base64.b64encode(authorization_origin.encode()).decode()

        return {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": authorization,
            "Date": date,
            "Host": host,
        }

    async def synthesize(
        self,
        text: str,
        *,
        reference_audio: str | Path | None = None,
        prompt: str = "",
    ) -> Path:
        """合成语音，返回音频文件路径。"""
        headers = self._build_headers()
        body = {
            "common": {"app_id": self._app_id},
            "business": {
                "aue": self._aue,
                "sfl": 1,
                "auf": "audio/L16;rate=16000",
                "vcn": self._voice_name,
                "speed": self._speed,
                "volume": self._volume,
                "pitch": self._pitch,
                "tte": "utf8",
            },
            "data": {"status": 2, "text": text},
        }

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(
                    _TTS_URL,
                    headers=headers,
                    data={"json": __import__("json").dumps(body)},
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise TTSError(f"讯飞 TTS 请求失败: {e.response.status_code}") from e
            except httpx.RequestError as e:
                raise TTSError(f"讯飞网络错误: {e}") from e

        ct = resp.headers.get("content-type", "")
        if "audio" in ct:
            ext = _EXT.get(self._aue, "mp3")
            workspace_run = Path("data/workspace/.run")
            workspace_run.mkdir(parents=True, exist_ok=True)
            ts = int(time.time() * 1000)
            out = workspace_run / f"voice_{ts}.{ext}"
            out.write_bytes(resp.content)
            logger.info(f"讯飞 TTS 完成: {out}")
            return out

        # 错误返回 JSON
        try:
            err = resp.json()
            raise TTSError(f"讯飞合成失败: {err}")
        except (ValueError, KeyError) as e:
            raise TTSError(f"讯飞合成失败: HTTP {resp.status_code}") from e

    async def aclose(self) -> None:
        pass
