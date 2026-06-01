"""共享状态对象：待处理请求、速率限制、等。

把旧 handler.py 里散落的全局字典/列表收口到类里，便于依赖注入和测试。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)


# ============================================================
# 待处理验证请求（好友/群申请暂存，等管理员决断）
# ============================================================


@dataclass(slots=True)
class PendingRequestInfo:
    """单条待处理验证请求。"""

    type: Literal["friend", "group"]
    user_id: str
    nickname: str
    comment: str
    flag: str
    expires_at: float
    sub_type: Literal["add", "invite"] | None = None
    """仅 type=group 时有效"""
    group_id: str | None = None
    """仅 type=group 时有效"""

    def to_text(self) -> str:
        """生成给 LLM 看的描述行。"""
        if self.type == "friend":
            return (
                f"- [好友请求] QQ={self.user_id} 昵称={self.nickname} "
                f"附加消息={self.comment} 【操作标识 flag={self.flag}】"
            )
        return (
            f"- [群请求/{self.sub_type}] QQ={self.user_id} 昵称={self.nickname} "
            f"群号={self.group_id} 附加消息={self.comment} "
            f"【操作标识 flag={self.flag}】"
        )


class PendingRequestStore:
    """待处理验证请求的暂存仓库。

    主要责任：
        - 入库
        - 过期清理（lazy：每次查询时清）
        - 文本化（拼成 system prompt 注入用）
    """

    def __init__(self, timeout_seconds: float = 1800.0) -> None:
        self._items: dict[str, PendingRequestInfo] = {}
        self._timeout = timeout_seconds

    def put(self, info: PendingRequestInfo) -> None:
        """存入一条待处理请求。"""
        self._items[info.flag] = info

    def get(self, flag: str) -> PendingRequestInfo | None:
        """按 flag 获取请求，查询前先清过期。"""
        self._purge_expired()
        return self._items.get(flag)

    def remove(self, flag: str) -> None:
        """移除指定请求（批准/拒绝后调用）。"""
        self._items.pop(flag, None)

    def to_prompt_text(self) -> str:
        """生成可注入 system prompt 的待处理请求文本。空时返回 ""。"""
        self._purge_expired()
        if not self._items:
            return ""
        lines = ["当前待处理的验证请求："]
        lines.extend(info.to_text() for info in self._items.values())
        lines.append(
            "管理员可通过回复消息指示同意或拒绝（使用 set_friend_add_request / "
            "set_group_add_request 工具）。"
        )
        return "\n".join(lines)

    def _purge_expired(self) -> None:
        now = time.time()
        expired = [flag for flag, info in self._items.items() if info.expires_at < now]
        for flag in expired:
            logger.info(f"验证请求已过期自动清理: flag={flag}")
            self._items.pop(flag, None)


# ============================================================
# 速率限制（非好友消息频次）
# ============================================================


class RateLimiter:
    """滑动窗口速率限制。

    用法：
        if await limiter.check_and_log(user_id):
            # 已超限
            await send_rate_limit_message(...)

    构造参数：
        window_seconds: 窗口长度
        max_messages: 窗口内最多允许的消息数
        whitelist_provider: 异步函数，返回不受限的 user_id 集合（如好友列表）
    """

    def __init__(
        self,
        window_seconds: int = 60,
        max_messages: int = 5,
        whitelist_provider=None,
        whitelist_cache_ttl_seconds: float = 300.0,
    ) -> None:
        self.window = window_seconds
        self.max_messages = max_messages
        self.whitelist_provider = whitelist_provider
        self.whitelist_cache_ttl_seconds = whitelist_cache_ttl_seconds
        self._records: dict[str, list[float]] = {}
        self._lock = asyncio.Lock()
        self._whitelist_cache: set[str] | None = None
        self._whitelist_cache_expires_at: float = 0.0
        self._whitelist_refresh_task: asyncio.Task | None = None

    async def check_and_log(self, user_id: str) -> bool:
        """记录本次消息，返回 True 表示已超限。"""
        if self.whitelist_provider is not None:
            whitelist = self._get_cached_whitelist()
            if whitelist is not None and user_id in whitelist:
                return False

        now = time.time()
        async with self._lock:
            stamps = self._records.get(user_id, [])
            stamps = [t for t in stamps if now - t < self.window]
            if len(stamps) >= self.max_messages:
                self._records[user_id] = stamps
                return True
            stamps.append(now)
            # 空窗口下不留 key（避免长期运行后空 list 内存泄漏）
            if stamps:
                self._records[user_id] = stamps
            else:
                self._records.pop(user_id, None)
            return False

    def _get_cached_whitelist(self) -> set[str] | None:
        """返回好友白名单缓存；过期时后台刷新，不阻塞消息热路径。"""
        now = time.time()
        if self._whitelist_cache is not None and now < self._whitelist_cache_expires_at:
            return self._whitelist_cache

        self._schedule_whitelist_refresh()
        # 有旧缓存时先用旧缓存，避免 NapCat 一次慢响应让好友被短时误判。
        return self._whitelist_cache

    def _schedule_whitelist_refresh(self) -> None:
        if self.whitelist_provider is None:
            return
        task = self._whitelist_refresh_task
        if task is not None and not task.done():
            return
        self._whitelist_refresh_task = asyncio.create_task(
            self._refresh_whitelist_cache(),
            name="rate-limit-whitelist-refresh",
        )

    async def _refresh_whitelist_cache(self) -> None:
        if self.whitelist_provider is None:
            return
        try:
            whitelist = await self.whitelist_provider()
            self._whitelist_cache = set(whitelist or set())
            self._whitelist_cache_expires_at = (
                time.time() + self.whitelist_cache_ttl_seconds
            )
            logger.debug("速率限制好友白名单已刷新：%s 人", len(self._whitelist_cache))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 不让好友列表查询失败阻塞正常聊天；已有旧缓存会继续使用。
            logger.debug("速率限制白名单后台刷新失败：%s", e)


# ============================================================
# 待处理批次（用户消息合并窗口内的暂存）
# ============================================================


@dataclass(slots=True)
class PendingMessageItem:
    """单条待批处理的入站消息（已脱离 NoneBot Event）。"""

    message_id: str
    user_id: str
    nickname: str
    location: str
    """显示用，如 '群聊 12345' / '私聊'"""

    text: str
    """重建后的可读消息文本（CQ 码解析 + 媒体 URL 标记后）"""

    conversation_id: str = "legacy:unknown"
    """当前消息所属会话标签，仅用于呈现/筛选，不拆存储。"""

    inbound_seq: int = 0
    """入站消息单调序号，用于判断回复发送前是否有群友插话。"""

    received_at: float = 0.0
    """pipeline 接收到该消息的 monotonic 时间。"""

    raw_event: object = field(default=None)
    """原始 IncomingMessage 引用，便于回查"""

    keyword_saved: bool = False
    """入队前是否已由关键词强制保存为长期记忆。"""


class MessageBatch:
    """消息批处理队列。

    接入点：MessagePipeline 启动 batch_task 后从这里取消息。
    """

    def __init__(self) -> None:
        self._pending: list[PendingMessageItem] = []
        self._lock = asyncio.Lock()

    @property
    def lock(self) -> asyncio.Lock:
        """暴露给外部，便于"加锁后既检查又操作"的场景。"""
        return self._lock

    async def append(self, item: PendingMessageItem) -> int:
        """添加一条消息，返回当前队列长度。"""
        async with self._lock:
            self._pending.append(item)
            return len(self._pending)

    async def drain(self) -> list[PendingMessageItem]:
        """取走所有积压消息（清空队列）。"""
        async with self._lock:
            items = self._pending[:]
            self._pending.clear()
            return items

    async def drain_conversation(self, conversation_id: str) -> list[PendingMessageItem]:
        """取走指定会话的积压消息，其他会话保留。"""
        async with self._lock:
            matched: list[PendingMessageItem] = []
            remaining: list[PendingMessageItem] = []
            for item in self._pending:
                if item.conversation_id == conversation_id:
                    matched.append(item)
                else:
                    remaining.append(item)
            self._pending = remaining
            return matched

    async def peek_locked(self) -> list[PendingMessageItem]:
        """需要在外部 `async with batch.lock:` 块内调用。
        用于"在发送前最后检查是否有新消息打断"场景。"""
        return list(self._pending)

    def is_empty_unsafe(self) -> bool:
        """不加锁的快速判断（仅作为优化提示，不能依赖）。"""
        return not self._pending
