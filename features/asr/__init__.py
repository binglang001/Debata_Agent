"""ASR（自动语音识别）feature 模块。

接口在此定义，具体实现由插件提供：
    - features/asr/local_whisper.py（Phase 3 由 DeepSeek 完成）
        基于 faster-whisper，模型存 F:/.models/faster-whisper/{size}/

未启用时 Pipeline 不调用；启用时由 message_pipeline 在处理语音段时调 transcribe()。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class ASRError(Exception):
    """ASR 调用相关异常。"""


class IASRService(ABC):
    """语音转文字的抽象接口。

    实装约定：
        - 实例化时**不**加载模型（lazy load 到首次 transcribe）
        - transcribe 接受文件路径，返回纯文本（无时间戳无说话人）
        - aclose 释放底层资源（模型句柄 / 临时文件等）
    """

    @abstractmethod
    async def transcribe(self, audio_path: str | Path) -> str:
        """把音频文件转成文本。失败 raise ASRError。"""

    @abstractmethod
    async def aclose(self) -> None:
        """释放底层资源。可幂等。"""


__all__ = ["ASRError", "IASRService"]
