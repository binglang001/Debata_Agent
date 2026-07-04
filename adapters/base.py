"""适配器抽象接口 —— 所有渠道适配器需要实现的契约。

`IAdapter` 是一个 ABC，不是 Protocol——理由：
    1. 强制实现，未实现时启动期就报错而不是运行期 AttributeError
    2. 可以提供默认行为（如未实现的可选 API 抛 NotImplementedError）
    3. 配合 isinstance 检查更友好

未实现的可选 API（如 Discord 没有"语音转文字"）默认抛 NotImplementedError，
业务层应捕获并降级处理。
"""

from __future__ import annotations

import inspect
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from .types import (
    AnyEvent,
    FriendInfo,
    GroupInfo,
    GroupMemberInfo,
    Target,
    UserInfo,
)

logger = logging.getLogger(__name__)

# 事件回调签名：async (event) -> None
EventCallback = Callable[[AnyEvent], Awaitable[None]]
FriendConfirmedCallback = Callable[[str], Awaitable[None] | None]


class AdapterError(Exception):
    """适配器层异常基类。"""


class AdapterNotConnectedError(AdapterError):
    """适配器未连接时调用 API。"""


class AdapterAPIError(AdapterError):
    """适配器 API 调用失败（远端返回错误）。"""


class IAdapter(ABC):
    """所有渠道适配器的抽象基类。"""

    #: 实例名（如 "napcat_default"），由 registry 统一管理
    name: str

    def __init__(self, name: str) -> None:
        self.name = name
        self._callback: EventCallback | None = None
        self._friend_confirmed_callback: FriendConfirmedCallback | None = None

    # ============================================================
    # 生命周期
    # ============================================================

    @abstractmethod
    async def start(self) -> None:
        """启动适配器：建立连接、注册心跳等。可能阻塞直到连接成功。"""

    @abstractmethod
    async def stop(self) -> None:
        """优雅停止：关闭连接、取消任务。"""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        ...

    # ============================================================
    # 事件订阅
    # ============================================================

    def subscribe(self, callback: EventCallback) -> None:
        """注册事件回调。一个适配器只支持一个回调（由 registry 统一收口）。"""
        self._callback = callback

    async def _emit(self, event: AnyEvent) -> None:
        """适配器实现内部调用此方法投递事件。"""
        if self._callback is not None:
            await self._callback(event)

    def set_friend_confirmed_callback(
        self,
        callback: FriendConfirmedCallback | None,
    ) -> None:
        """注册“确认已是好友”的回调，供运行时刷新好友白名单缓存。"""
        self._friend_confirmed_callback = callback

    async def _emit_friend_confirmed(self, user_id: str) -> None:
        """适配器确认 user_id 已是好友时调用。"""
        callback = self._friend_confirmed_callback
        if callback is None:
            return
        try:
            result = callback(str(user_id))
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            logger.warning("好友确认回调失败 user_id=%s: %s", user_id, e)

    # ============================================================
    # 消息发送
    # ============================================================

    @abstractmethod
    async def send_text(self, target: Target, content: str) -> str | None:
        """发送文本消息（可含 CQ 码或平台标记）。返回 message_id。"""

    @abstractmethod
    async def send_image(
        self,
        target: Target,
        *,
        image_path: Path | None = None,
        image_url: str | None = None,
        image_b64: str | None = None,
    ) -> str | None:
        """发送图片。三种来源任选一种。"""

    @abstractmethod
    async def recall(self, message_id: str) -> bool:
        """撤回消息。返回是否成功。"""

    # ============================================================
    # 联系人查询
    # ============================================================

    @abstractmethod
    async def list_friends(self) -> list[FriendInfo]:
        ...

    @abstractmethod
    async def list_groups(self) -> list[GroupInfo]:
        ...

    @abstractmethod
    async def list_group_members(self, group_id: str) -> list[GroupMemberInfo]:
        ...

    @abstractmethod
    async def get_user_info(self, user_id: str) -> UserInfo:
        ...

    # ============================================================
    # 请求处理
    # ============================================================

    @abstractmethod
    async def handle_friend_request(
        self, flag: str, approve: bool, remark: str = ""
    ) -> dict[str, Any] | None:
        ...

    @abstractmethod
    async def handle_group_request(
        self,
        flag: str,
        sub_type: Literal["add", "invite"],
        approve: bool,
        reason: str = "",
    ) -> None:
        ...

    # ============================================================
    # 媒体辅助（可选实现）
    # ============================================================

    async def fetch_voice_text(self, message_id: str) -> str:
        """获取语音消息的文字转录。

        默认未实现，子类按需覆盖。
        """
        raise NotImplementedError(f"{self.name} 不支持语音转文字")

    async def get_file_url(self, file_id: str) -> str | None:
        """根据 file_id 获取可下载 URL。"""
        raise NotImplementedError(f"{self.name} 不支持 file_id → URL 解析")

    async def get_image_url(self, file_id: str) -> str | None:
        """根据图片 file_id 获取本地路径或可下载 URL。"""
        raise NotImplementedError(f"{self.name} 不支持 image file_id → URL 解析")

    async def upload_file(
        self,
        target: Target,
        file_path: Path,
        *,
        display_name: str | None = None,
    ) -> None:
        """上传文件到目标会话。"""
        raise NotImplementedError(f"{self.name} 不支持文件上传")

    async def get_forward_msg(self, forward_id: str) -> list[dict[str, Any]]:
        """提取合并转发消息的内容。"""
        raise NotImplementedError(f"{self.name} 不支持合并转发提取")

    async def get_group_history(
        self, group_id: str, count: int = 100
    ) -> list[dict[str, Any]]:
        """获取群历史消息。"""
        raise NotImplementedError(f"{self.name} 不支持群历史拉取")

    # ============================================================
    # 通用 API 通道（适配器专属功能）
    # ============================================================

    @abstractmethod
    async def call_api(self, action: str, **params: Any) -> dict[str, Any]:
        """通用 API 调用。用于适配器特有的扩展能力（如 NapCat 的 fetch_ptt_text）。

        业务层应优先使用上面的语义化方法。call_api 是最后的逃生通道。
        """

    # ============================================================
    # 调试辅助
    # ============================================================

    def __repr__(self) -> str:
        status = "connected" if self.is_connected else "disconnected"
        return f"<{type(self).__name__} name={self.name!r} [{status}]>"
