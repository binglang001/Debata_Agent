"""科大讯飞语音听写 WebSocket API（流式版）。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from urllib.parse import urlencode

from features.asr import ASRError, IASRService

logger = logging.getLogger(__name__)

# 讯飞 WebSocket 地址
_WSS_URL = "wss://iat-api.xfyun.cn/v2/iat"

# 音频帧大小（1280 字节 / 200ms @ 16kHz 16bit mono）
_FRAME_SIZE = 1280

# 状态码
_STATUS_FIRST = 0
_STATUS_CONTINUE = 1
_STATUS_LAST = 2


class iFlytekASRService(IASRService):
    """讯飞语音识别 —— WebSocket 流式 API。"""

    def __init__(
        self,
        app_id: str,
        api_key: str,
        api_secret: str,
        language: str = "zh_cn",
        accent: str = "mandarin",
        timeout: float = 30.0,
    ) -> None:
        self._app_id = app_id
        self._api_key = api_key
        self._api_secret = api_secret
        self._language = language
        self._accent = accent
        self._timeout = timeout

    async def warmup(self) -> None:
        """讯飞无预加载，no-op。"""
        pass

    def _build_auth_url(self) -> str:
        """构造带 HMAC-SHA256 签名的 WebSocket URL。"""
        host = "iat-api.xfyun.cn"
        date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())

        # 签名原文
        signature_origin = f"host: {host}\ndate: {date}\nGET /v2/iat HTTP/1.1"
        signature = base64.b64encode(
            hmac.new(
                self._api_secret.encode(),
                signature_origin.encode(),
                hashlib.sha256,
            ).digest()
        ).decode()

        # authorization
        authorization_origin = (
            f'api_key="{self._api_key}",'
            f'algorithm="hmac-sha256",'
            f'headers="host date request-line",'
            f'signature="{signature}"'
        )
        authorization = base64.b64encode(authorization_origin.encode()).decode()

        params = urlencode(
            {"authorization": authorization, "date": date, "host": host}
        )
        return f"{_WSS_URL}?{params}"

    async def transcribe(self, audio_path: str | Path) -> str:
        """WebSocket 流式发送音频，拼接收到的识别文本。"""
        audio_path = Path(audio_path)
        if not audio_path.exists():
            raise ASRError(f"音频文件不存在: {audio_path}")

        # 读原始 PCM（或转码后）
        raw = audio_path.read_bytes()
        if len(raw) == 0:
            raise ASRError("音频文件为空")

        # 转 16kHz 16bit mono PCM（如需要）；这里假定 NapCat 已转码
        # 实际项目中有 ffmpeg 转码步骤，此处接收已转码的结果

        ws_url = self._build_auth_url()

        try:
            import websockets
        except ImportError as e:
            raise ASRError("需安装 websockets: pip install websockets") from e

        result_parts: list[str] = []
        result_event = asyncio.Event()

        async def _run():
            try:
                async with websockets.connect(ws_url, open_timeout=self._timeout) as ws:
                    # 发送启动参数
                    await ws.send(json.dumps({
                        "common": {"app_id": self._app_id},
                        "business": {
                            "language": self._language,
                            "domain": "iat",
                            "accent": self._accent,
                            "ptt": 1,  # 自动标点
                        },
                        "data": {
                            "status": _STATUS_FIRST,
                            "format": "audio/L16;rate=16000",
                            "encoding": "raw",
                            "audio": base64.b64encode(raw).decode("ascii"),
                        },
                    }))

                    # 收结果
                    async for msg in ws:
                        data = json.loads(msg)
                        code = data.get("code", 0)
                        if code != 0:
                            raise ASRError(f"讯飞识别失败: code={code} {data.get('message', '')}")
                        info = data.get("data", {})
                        if info.get("status") == 2:  # 最终结果
                            # 拼接最终结果
                            ws_result = info.get("result", {})
                            text = _decode_iflytek_result(ws_result)
                            if text:
                                result_parts.append(text)
                            result_event.set()
                            break
                        else:
                            # 中间结果暂不收，只等最终结果（status=2）
                            pass
            except websockets.WebSocketException as e:
                raise ASRError(f"讯飞 WebSocket 错误: {e}") from e

        task = asyncio.ensure_future(_run())
        try:
            await asyncio.wait_for(result_event.wait(), timeout=self._timeout)
        except asyncio.TimeoutError as e:
            task.cancel()
            raise ASRError("讯飞识别超时") from e
        await task
        return "".join(result_parts)

    async def aclose(self) -> None:
        pass


def _decode_iflytek_result(result: dict) -> str:
    """从讯飞 JSON 结果中提取文本。ws 数组按 sn 排序拼接。"""
    ws = result.get("ws", [])
    if not ws:
        return ""
    # 按 sn 排序（虽然通常已按序）
    ws_sorted = sorted(ws, key=lambda x: x.get("sn", 0))
    parts = []
    for item in ws_sorted:
        cw = item.get("cw", [])
        for w in cw:
            parts.append(w.get("w", ""))
    return "".join(parts)
