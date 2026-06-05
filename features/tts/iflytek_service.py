"""iFlytek streaming TTS WebSocket API."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from email.utils import formatdate
from pathlib import Path
from urllib.parse import urlencode, urlparse

import websockets

from features.tts import ITTSService, TTSError

logger = logging.getLogger(__name__)

_TTS_URL = "wss://tts-api.xfyun.cn/v2/tts"
_AUE_EXT = {
    "raw": "pcm",
    "lame": "mp3",
    "mp3": "mp3",
    "opus": "opus",
    "opus-wb": "opus",
    "speex": "speex",
    "speex-wb": "speex",
}


class iFlytekTTSService(ITTSService):
    """科大讯飞在线语音合成流式 API。"""

    def __init__(
        self,
        app_id: str,
        api_key: str,
        api_secret: str,
        voice_name: str = "x4_xiaoyan",
        *,
        speed: int = 50,
        volume: int = 50,
        pitch: int = 50,
        aue: str = "lame",
        auf: str = "audio/L16;rate=16000",
        tte: str = "UTF8",
        reg: str = "2",
        rdn: str = "0",
        timeout: float = 30.0,
        output_dir: str | Path = "data/workspace/.run",
    ) -> None:
        self._app_id = app_id
        self._api_key = api_key
        self._api_secret = api_secret
        self._voice_name = voice_name or "x4_xiaoyan"
        self._speed = _clamp_int(speed, 0, 100, 50)
        self._volume = _clamp_int(volume, 0, 100, 50)
        self._pitch = _clamp_int(pitch, 0, 100, 50)
        self._aue = aue or "lame"
        self._auf = auf or "audio/L16;rate=16000"
        self._tte = tte or "UTF8"
        self._reg = reg or "2"
        self._rdn = rdn or "0"
        self._timeout = timeout
        self._output_dir = Path(output_dir)

    async def warmup(self) -> None:
        """No-op. Do not spend an API call at startup."""

    def _auth_url(self) -> str:
        parsed = urlparse(_TTS_URL)
        host = parsed.netloc
        date = formatdate(timeval=None, localtime=False, usegmt=True)
        signature_origin = f"host: {host}\ndate: {date}\nGET {parsed.path} HTTP/1.1"
        signature = base64.b64encode(
            hmac.new(
                self._api_secret.encode("utf-8"),
                signature_origin.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")
        authorization_origin = (
            f'api_key="{self._api_key}", algorithm="hmac-sha256", '
            f'headers="host date request-line", signature="{signature}"'
        )
        authorization = base64.b64encode(
            authorization_origin.encode("utf-8")
        ).decode("utf-8")
        query = urlencode(
            {
                "authorization": authorization,
                "date": date,
                "host": host,
            }
        )
        return f"{_TTS_URL}?{query}"

    async def synthesize(
        self,
        text: str,
        *,
        reference_audio: str | Path | None = None,
        prompt: str = "",
    ) -> Path:
        content = (text or "").strip()
        if not content:
            raise TTSError("讯飞 TTS 合成文本为空")
        if not self._app_id or not self._api_key or not self._api_secret:
            raise TTSError("讯飞 TTS 缺少 AppID、APIKey 或 APISecret")
        encoded = content.encode("utf-8")
        if len(encoded) >= 8000:
            raise TTSError("讯飞 TTS 单次文本需小于 8000 字节，请缩短文本")

        payload = {
            "common": {"app_id": self._app_id},
            "business": {
                "aue": self._aue,
                "auf": self._auf,
                "vcn": self._voice_name,
                "speed": self._speed,
                "volume": self._volume,
                "pitch": self._pitch,
                "tte": self._tte,
                "reg": self._reg,
                "rdn": self._rdn,
            },
            "data": {
                "status": 2,
                "text": base64.b64encode(encoded).decode("ascii"),
            },
        }
        if self._aue == "lame":
            payload["business"]["sfl"] = 1

        chunks: list[bytes] = []
        try:
            async with websockets.connect(
                self._auth_url(),
                open_timeout=self._timeout,
                close_timeout=5,
                max_size=None,
            ) as websocket:
                await asyncio_wait_for_send(websocket, json.dumps(payload, ensure_ascii=False), self._timeout)
                while True:
                    raw = await asyncio_wait_for_recv(websocket, self._timeout)
                    frame = json.loads(raw)
                    code = int(frame.get("code", 0))
                    if code != 0:
                        raise TTSError(
                            f"讯飞 TTS 返回错误 code={code}: {frame.get('message') or frame}"
                        )
                    data = frame.get("data")
                    if not data:
                        continue
                    audio = data.get("audio")
                    if audio:
                        try:
                            chunks.append(base64.b64decode(audio))
                        except Exception as e:  # noqa: BLE001
                            raise TTSError("讯飞 TTS 返回音频 base64 解码失败") from e
                    if int(data.get("status", 0)) == 2:
                        break
        except TTSError:
            raise
        except Exception as e:  # noqa: BLE001
            raise TTSError(f"讯飞 TTS WebSocket 调用失败：{e}") from e

        if not chunks:
            raise TTSError("讯飞 TTS 未返回音频数据")
        self._output_dir.mkdir(parents=True, exist_ok=True)
        ext = _AUE_EXT.get(self._aue.split(";")[0], "mp3")
        out = self._output_dir / f"voice_{int(time.time() * 1000)}.{ext}"
        out.write_bytes(b"".join(chunks))
        logger.info("讯飞 TTS 完成: %s voice=%s aue=%s", out, self._voice_name, self._aue)
        return out

    async def aclose(self) -> None:
        pass


async def asyncio_wait_for_send(websocket, message: str, timeout: float) -> None:
    import asyncio

    await asyncio.wait_for(websocket.send(message), timeout=timeout)


async def asyncio_wait_for_recv(websocket, timeout: float) -> str:
    import asyncio

    raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _clamp_int(value: int, minimum: int, maximum: int, default: int) -> int:
    try:
        raw = int(value)
    except (TypeError, ValueError):
        raw = default
    return min(maximum, max(minimum, raw))
