"""火山引擎语音识别 WebSocket API（BigModel ASR）。"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import struct
import uuid
from pathlib import Path

from features.asr import ASRError, IASRService

logger = logging.getLogger(__name__)

_WSS_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"

# 协议常量
_HEADER_SIZE = 4
_SEQ_SIZE = 4
_PAYLOAD_LEN_SIZE = 4
_FRAME_OVERHEAD = _HEADER_SIZE + _SEQ_SIZE + _PAYLOAD_LEN_SIZE

# 消息类型
_TYPE_FULL_REQUEST = 0x01
_TYPE_AUDIO_ONLY = 0x02
_TYPE_AUDIO_LAST = 0x03

# 序列化类型
_SERIAL_JSON = 0x01
_SERIAL_NO_DATA = 0x00


def _build_frame(msg_type: int, payload: bytes, seq: int) -> bytes:
    """构造 BigModel 二进制帧：Header(4) + Seq(4) + PayloadLen(4) + Payload。"""
    payload = gzip.compress(payload)
    header = struct.pack(">I", (msg_type << 4) | (_SERIAL_JSON if payload else _SERIAL_NO_DATA))
    seq_bytes = struct.pack(">I", seq)
    length = struct.pack(">I", len(payload))
    return header + seq_bytes + length + payload


class VolcengineASRService(IASRService):
    """火山引擎 BigModel 语音识别 —— WebSocket 二进制协议。"""

    def __init__(
        self,
        app_id: str,
        access_token: str,
        resource_id: str = "volc.seedasr.sauc.duration",
        timeout: float = 30.0,
    ) -> None:
        self._app_id = app_id
        self._access_token = access_token
        self._resource_id = resource_id
        self._timeout = timeout

    async def warmup(self) -> None:
        """火山引擎无预加载。"""
        pass

    async def transcribe(self, audio_path: str | Path) -> str:
        """WebSocket 流式发送 PCM 音频，返回识别文本。"""
        audio_path = Path(audio_path)
        if not audio_path.exists():
            raise ASRError(f"音频文件不存在: {audio_path}")

        raw = audio_path.read_bytes()
        if len(raw) == 0:
            raise ASRError("音频文件为空")

        try:
            import websockets
        except ImportError as e:
            raise ASRError("需安装 websockets: pip install websockets") from e

        connect_id = uuid.uuid4().hex
        result_parts: list[str] = []
        result_event = asyncio.Event()

        async def _run():
            try:
                async with websockets.connect(
                    _WSS_URL,
                    extra_headers={
                        "X-Api-App-Id": self._app_id,
                        "X-Api-Access-Key": self._access_token,
                        "X-Api-Connect-Id": connect_id,
                    },
                    open_timeout=self._timeout,
                ) as ws:
                    seq = 0

                    # 发送 FULL_CLIENT_REQUEST 初始化
                    init = json.dumps({
                        "user": {"uid": "debata"},
                        "audio": {
                            "format": "pcm",
                            "rate": 16000,
                            "bits": 16,
                            "channel": 1,
                            "language": "zh-CN",
                        },
                        "request": {
                            "model_name": "bigmodel",
                            "enable_itn": True,
                            "enable_punc": True,
                        },
                    }).encode()
                    await ws.send(_build_frame(_TYPE_FULL_REQUEST, init, seq))
                    seq += 1

                    # 分帧发送音频（200ms / 1280 bytes per frame）
                    chunk_size = 1280
                    offset = 0
                    while offset < len(raw):
                        chunk = raw[offset: offset + chunk_size]
                        offset += chunk_size
                        await ws.send(
                            _build_frame(
                                _TYPE_AUDIO_ONLY if offset < len(raw) else _TYPE_AUDIO_LAST,
                                chunk,
                                seq,
                            )
                        )
                        seq += 1
                        await asyncio.sleep(0.1)  # 模拟实时流

                    # 如果最后一块刚好是 chunk_size 的倍数，补一个空 LAST 帧
                    if len(raw) % chunk_size == 0:
                        await ws.send(_build_frame(_TYPE_AUDIO_LAST, b"", seq))

                    # 收结果
                    async for msg in ws:
                        # 解析响应帧
                        if isinstance(msg, bytes) and len(msg) >= _FRAME_OVERHEAD:
                            payload = msg[_FRAME_OVERHEAD:]
                            try:
                                payload = gzip.decompress(payload)
                                data = json.loads(payload.decode())
                            except (gzip.BadGzipFile, json.JSONDecodeError):
                                continue

                            # 累积识别文本
                            payload_msg = data.get("payload_msg", {})
                            if payload_msg.get("result"):
                                for utterance in payload_msg["result"]:
                                    text = utterance.get("text", "")
                                    if text:
                                        result_parts.append(text)
                            # 检查消息类型
                            msg_type_int = data.get("message", 0)
                            if isinstance(msg_type_int, int) and msg_type_int == 1:
                                # ASR_ENDED: 服务端结束
                                result_event.set()
                                break

            except websockets.WebSocketException as e:
                raise ASRError(f"火山引擎 WebSocket 错误: {e}") from e

        task = asyncio.ensure_future(_run())
        try:
            await asyncio.wait_for(result_event.wait(), timeout=self._timeout)
        except asyncio.TimeoutError as e:
            task.cancel()
            raise ASRError("火山引擎识别超时") from e
        await task
        return "".join(result_parts)

    async def aclose(self) -> None:
        pass
