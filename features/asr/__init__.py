"""ASR（自动语音识别）feature 模块。

接口在此定义，具体实现由插件提供：
    - plugins/whisper/whisper_impl.py（基于 faster-whisper，DeepSeek 实装）

未启用时 Pipeline 不调用；启用时由 message_pipeline 在处理语音段时调 transcribe()。

预热（warmup）：
    Runtime 启动后会 fire-and-forget 调一次 warmup()。模型在后台加载，
    第一次 transcribe() 时若仍未就绪则 await ready 事件，避免用户等 30s。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class ASRError(Exception):
    """ASR 调用相关异常。"""


class IASRService(ABC):
    """语音转文字的抽象接口。

    实装约定：
        - 实例化时**不**加载模型（实际加载放在 warmup() 内）
        - warmup 同步加载模型，可被 Runtime 启动后 fire-and-forget
        - transcribe 接受文件路径，返回纯文本（无时间戳无说话人）
        - 如果 transcribe 时 warmup 还没跑完，内部应 await ready 事件
        - aclose 释放底层资源（模型句柄 / 临时文件等），可幂等
    """

    @abstractmethod
    async def warmup(self) -> None:
        """加载模型到内存。重复调用应幂等。Runtime 启动后 fire-and-forget 调一次。"""

    @abstractmethod
    async def transcribe(self, audio_path: str | Path) -> str:
        """把音频文件转成文本。失败 raise ASRError。
        若 warmup 未完成，内部应 await。"""

    @abstractmethod
    async def aclose(self) -> None:
        """释放底层资源。可幂等。"""


__all__ = [
    "ASRError",
    "IASRService",
    "BaiduASRService",
    "iFlytekASRService",
    "VolcengineASRService",
]

# 懒导入，避免没装 websockets 时顶层崩溃
def _get_baidu_service():
    from .baidu_service import BaiduASRService
    return BaiduASRService


def _get_iflytek_service():
    from .iflytek_service import iFlytekASRService
    return iFlytekASRService


def _get_volcengine_service():
    from .volcengine_service import VolcengineASRService
    return VolcengineASRService
