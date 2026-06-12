"""百度短语音识别 REST API。"""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path

import httpx

from features.asr import ASRError, IASRService

logger = logging.getLogger(__name__)

# 百度语音识别 dev_pid 映射
PID_MAP = {
    "zh": 1537,  # 普通话（标准版）
    "zh-fast": 80001,  # 普通话（极速版）
    "en": 1737,  # 英语
    "yue": 1637,  # 粤语
    "sichuan": 1837,  # 四川话
}

_TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
_API_URL = "http://vop.baidu.com/server_api"


class BaiduASRService(IASRService):
    """百度语音识别 —— REST API，支持 60s 内短音频。"""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        dev_pid: int = 1537,
        timeout: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._secret_key = secret_key
        self._dev_pid = dev_pid
        self._timeout = timeout
        self._token: str | None = None
        self._client: httpx.AsyncClient | None = None

    async def warmup(self) -> None:
        """预取 access_token。"""
        await self._ensure_token()

    async def _ensure_token(self) -> str:
        """获取或刷新百度 OAuth2 access_token。"""
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
                raise ASRError(f"百度 access_token 获取失败: {data}")
            return self._token
        except httpx.HTTPStatusError as e:
            raise ASRError(f"百度鉴权失败: {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise ASRError(f"百度网络错误: {e}") from e

    async def transcribe(self, audio_path: str | Path) -> str:
        """识别短音频（≤60s），返回文本。"""
        audio_path = Path(audio_path)
        if not audio_path.exists():
            raise ASRError(f"音频文件不存在: {audio_path}")

        token = await self._ensure_token()
        assert self._client is not None

        # 读音频并 base64
        def _read():
            raw = audio_path.read_bytes()
            return base64.b64encode(raw).decode("ascii"), len(raw)

        speech_b64, raw_len = await asyncio.to_thread(_read)

        body = {
            "format": audio_path.suffix.lstrip(".") or "wav",
            "rate": 16000,
            "channel": 1,
            "cuid": "debata",
            "token": token,
            "dev_pid": self._dev_pid,
            "len": raw_len,
            "speech": speech_b64,
        }

        try:
            resp = await self._client.post(_API_URL, json=body)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            raise ASRError(f"百度识别请求失败: {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise ASRError(f"百度网络错误: {e}") from e

        err_no = data.get("err_no", -1)
        if err_no != 0:
            raise ASRError(f"百度识别失败: err_no={err_no} {data.get('err_msg', '')}")
        results = data.get("result", [])
        return results[0] if results else ""

    async def aclose(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        self._token = None
