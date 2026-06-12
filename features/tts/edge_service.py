"""EdgeTTS online speech synthesis service."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from features.tts import ITTSService, TTSError

logger = logging.getLogger(__name__)


class EdgeTTSService(ITTSService):
    """Microsoft Edge online TTS via the edge-tts package.

    The service needs no API key, but synthesis depends on Microsoft's online
    endpoint and may fail because of network conditions or service policy.
    """

    def __init__(
        self,
        voice: str = "zh-CN-XiaoxiaoNeural",
        *,
        rate: str = "+0%",
        volume: str = "+0%",
        pitch: str = "+0Hz",
        output_dir: str | Path = "data/workspace/.run",
    ) -> None:
        self._voice = voice or "zh-CN-XiaoxiaoNeural"
        self._rate = rate or "+0%"
        self._volume = volume or "+0%"
        self._pitch = pitch or "+0Hz"
        self._output_dir = Path(output_dir)

    async def warmup(self) -> None:
        """No-op. Avoid network calls during Runtime startup."""

    async def synthesize(
        self,
        text: str,
        *,
        reference_audio: str | Path | None = None,
        prompt: str = "",
    ) -> Path:
        content = (text or "").strip()
        if not content:
            raise TTSError("EdgeTTS 合成文本为空")
        try:
            import edge_tts
        except ImportError as e:
            raise TTSError("未安装 edge-tts，请安装项目依赖后再启用 EdgeTTS") from e

        self._output_dir.mkdir(parents=True, exist_ok=True)
        out = self._output_dir / f"voice_{int(time.time() * 1000)}.mp3"
        try:
            try:
                communicate = edge_tts.Communicate(
                    content,
                    self._voice,
                    rate=self._rate,
                    volume=self._volume,
                    pitch=self._pitch,
                )
            except TypeError:
                communicate = edge_tts.Communicate(
                    content,
                    self._voice,
                    rate=self._rate,
                    volume=self._volume,
                )
            await communicate.save(str(out))
        except Exception as e:  # noqa: BLE001
            raise TTSError(
                "EdgeTTS 合成失败；它是免费在线服务，但可能因网络、代理或微软服务策略不可用。"
                f"原始错误：{e}"
            ) from e

        logger.info("EdgeTTS 完成: %s voice=%s", out, self._voice)
        return out

    async def aclose(self) -> None:
        pass
