"""适配器层统一数据模型 —— 屏蔽平台差异。

设计原则：
    - 所有适配器（NapCat / Discord / Telegram / ...）都向上提供同一套数据模型
    - 消息正文保留原始字符串（含 CQ 码或平台特定标记），由适配器负责解析/转换
    - 媒体段单独提取为 MediaSegment，便于业务层统一处理图片/语音/文件
    - Event 携带原始 JSON（raw），便于调试和特殊场景兜底

跨平台未来扩展点：
    - Discord 把 mention 转成 [CQ:at,qq=...] 风格，业务层无感
    - Telegram 把 reply_to 映射到 reply_message_id
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


# ============================================================
# 目标（发送方向）
# ============================================================


@dataclass(frozen=True, slots=True)
class Target:
    """消息发送目标。"""

    adapter: str
    """适配器名称（如 'napcat_default'）"""

    scope: Literal["private", "group"]
    """会话类型：私聊 or 群聊"""

    target_id: str
    """私聊时是 QQ 号，群聊时是群号。统一用字符串避免大数 ID 精度问题。"""

    def __str__(self) -> str:
        return f"{self.adapter}:{self.scope}:{self.target_id}"


# ============================================================
# 媒体段（图片/语音/文件等）
# ============================================================


class MediaType(str, Enum):
    IMAGE = "image"
    VOICE = "voice"
    VIDEO = "video"
    FILE = "file"
    FORWARD = "forward"
    FACE = "face"
    RECORD = "record"   # OneBot 语音


@dataclass(slots=True)
class MediaSegment:
    """一条消息中夹带的非文本资源。"""

    type: MediaType
    file_id: str | None = None
    """平台内部的资源 ID（如 NapCat 的 file_id）"""

    url: str | None = None
    """可直接下载的 URL（如果适配器能解析出来）"""

    name: str | None = None
    """文件名/显示名"""

    extra: dict[str, Any] = field(default_factory=dict)
    """适配器特有的附加字段"""


# ============================================================
# 事件
# ============================================================


class EventType(str, Enum):
    MESSAGE = "message"
    NOTICE = "notice"
    REQUEST = "request"
    META = "meta"


@dataclass(slots=True, kw_only=True)
class BaseEvent:
    """所有事件的基类。"""

    adapter: str
    """事件来源适配器名"""

    event_type: EventType

    timestamp: float
    """事件发生时间（Unix 秒）"""

    self_id: str
    """机器人自身 ID（如机器人 QQ 号）"""

    raw: dict[str, Any] = field(default_factory=dict)
    """适配器上报的原始 JSON，供调试和兜底使用"""


@dataclass(slots=True, kw_only=True)
class IncomingMessage(BaseEvent):
    """收到的消息事件。"""

    event_type: EventType = EventType.MESSAGE
    message_id: str
    """平台分配的消息 ID"""

    scope: Literal["private", "group"]
    user_id: str
    """发送者 ID"""

    nickname: str
    """发送者昵称（群聊优先群名片）"""

    group_id: str | None = None
    """仅群聊有效"""

    text: str = ""
    """纯文本内容（去除 CQ 码后的可读文本）"""

    raw_message: str = ""
    """含 CQ 码的原始消息字符串"""

    media: list[MediaSegment] = field(default_factory=list)
    """提取出的媒体段"""

    reply_to: str | None = None
    """如果消息回复了别的消息，这里是被回复的 message_id"""

    def is_private(self) -> bool:
        return self.scope == "private"

    def is_group(self) -> bool:
        return self.scope == "group"

    @property
    def source_target(self) -> Target:
        """消息来源对应的 Target（用于回复）。"""
        target_id = self.group_id if self.is_group() else self.user_id
        return Target(adapter=self.adapter, scope=self.scope, target_id=target_id or self.user_id)


# ----- Notice 子类型 -----


class NoticeType(str, Enum):
    GROUP_RECALL = "group_recall"
    FRIEND_RECALL = "friend_recall"
    GROUP_INCREASE = "group_increase"
    GROUP_DECREASE = "group_decrease"
    GROUP_ADMIN = "group_admin"
    FRIEND_ADD = "friend_add"
    POKE = "poke"
    OTHER = "other"


@dataclass(slots=True, kw_only=True)
class IncomingNotice(BaseEvent):
    """通知事件（撤回/入群/退群/戳一戳等）。"""

    event_type: EventType = EventType.NOTICE
    notice_type: NoticeType
    user_id: str | None = None
    group_id: str | None = None
    operator_id: str | None = None
    """触发者 ID（如撤回操作者）"""
    message_id: str | None = None


# ----- Request 子类型 -----


class RequestType(str, Enum):
    FRIEND = "friend"
    GROUP_ADD = "group_add"
    GROUP_INVITE = "group_invite"


@dataclass(slots=True, kw_only=True)
class IncomingRequest(BaseEvent):
    """请求事件（加好友/加群/邀请入群）。"""

    event_type: EventType = EventType.REQUEST
    request_type: RequestType
    flag: str
    """通过/拒绝时需要传回的标识"""

    user_id: str
    group_id: str | None = None
    comment: str = ""
    """附加消息（用户填写的验证消息）"""


@dataclass(slots=True, kw_only=True)
class MetaEvent(BaseEvent):
    """元事件：连接、心跳、生命周期等。"""

    event_type: EventType = EventType.META
    meta_type: str
    """如 'lifecycle' / 'heartbeat'"""

    sub_type: str = ""


# ----- 公共联合类型 -----


AnyEvent = IncomingMessage | IncomingNotice | IncomingRequest | MetaEvent


# ============================================================
# 联系人/群信息（统一返回结构）
# ============================================================


@dataclass(slots=True)
class FriendInfo:
    user_id: str
    nickname: str
    remark: str = ""


@dataclass(slots=True)
class GroupInfo:
    group_id: str
    group_name: str
    member_count: int = 0


@dataclass(slots=True)
class GroupMemberInfo:
    user_id: str
    nickname: str
    card: str = ""
    """群名片，没有时退回 nickname"""

    role: Literal["owner", "admin", "member"] = "member"

    @property
    def display_name(self) -> str:
        return self.card or self.nickname


@dataclass(slots=True)
class UserInfo:
    user_id: str
    nickname: str
    sex: str = "unknown"
    age: int = 0
    extra: dict[str, Any] = field(default_factory=dict)
