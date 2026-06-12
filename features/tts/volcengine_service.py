"""火山引擎豆包语音合成 HTTP REST API（一次性合成）。"""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path

import httpx

from features.tts import ITTSService, TTSError

logger = logging.getLogger(__name__)

_TTS_URL = "https://openspeech.bytedance.com/api/v1/tts"


class VolcengineTTSService(ITTSService):
    """火山引擎语音合成 —— HTTP POST 返回 base64 音频。"""

    def __init__(
        self,
        app_id: str,
        access_token: str,
        voice_type: str = "zh_female_qingxin",
        format_: str = "mp3",
        sample_rate: int = 24000,
        speed: float = 1.0,
        volume: float = 1.0,
        pitch: float = 1.0,
        timeout: float = 15.0,
    ) -> None:
        self._app_id = app_id
        self._access_token = access_token
        self._voice_type = voice_type
        self._format = format_
        self._sample_rate = sample_rate
        self._speed = speed
        self._volume = volume
        self._pitch = pitch
        self._timeout = timeout

    async def warmup(self) -> None:
        pass

    async def synthesize(
        self,
        text: str,
        *,
        reference_audio: str | Path | None = None,
        prompt: str = "",
    ) -> Path:
        """合成语音，返回音频文件路径。"""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(
                    _TTS_URL,
                    json={
                        "app": {"appid": self._app_id},
                        "user": {"uid": "debata"},
                        "audio": {
                            "voice_type": self._voice_type,
                            "encoding": self._format,
                            "sample_rate": self._sample_rate,
                            "speed_ratio": self._speed,
                            "volume_ratio": self._volume,
                            "pitch_ratio": self._pitch,
                        },
                        "request": {"text": text, "text_type": "plain"},
                    },
                    headers={
                        "Authorization": f"Bearer;{self._access_token}",
                        "Content-Type": "application/json",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as e:
                raise TTSError(f"火山 TTS 请求失败: {e.response.status_code}") from e
            except httpx.RequestError as e:
                raise TTSError(f"火山网络错误: {e}") from e

        audio_b64 = data.get("data")
        if not audio_b64:
            raise TTSError(f"火山 TTS 返回空: {data}")

        workspace_run = Path("data/workspace/.run")
        workspace_run.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        out = workspace_run / f"voice_{ts}.{self._format}"
        out.write_bytes(base64.b64decode(audio_b64))
        logger.info(f"火山 TTS 完成: {out}")
        return out

    async def aclose(self) -> None:
        pass
