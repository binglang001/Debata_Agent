"""好友/群请求处理 —— 暂存请求 + 通知管理员 + 等待审核。

迁移自旧 handler._handle_request / handle_friend_request / handle_group_request。

行为：
    1. 收到 request 事件 → 把请求信息塞进 PendingRequestStore（带 30 分钟过期）
    2. 调 pipeline.run_one_turn，让 Agent 看到 task_context 中的请求描述
    3. Agent 通常会调 send_private_messages 通知管理员
    4. 管理员回复后再次走 message pipeline，Agent 会从 pending_requests 文本
       里看到对应 flag，决定调 set_*_add_request 工具批准/拒绝
"""

from __future__ import annotations

import logging
import time

from adapters.types import IncomingRequest, RequestType
from utils import get_time

from .message_pipeline import MessagePipeline
from .state import PendingRequestInfo, PendingRequestStore

logger = logging.getLogger(__name__)


class RequestHandler:
    """好友/群请求处理器。"""

    def __init__(
        self,
        *,
        pipeline: MessagePipeline,
        pending_requests: PendingRequestStore,
        request_timeout_seconds: float = 1800.0,
    ) -> None:
        self.pipeline = pipeline
        self.pending_requests = pending_requests
        self.request_timeout = request_timeout_seconds

    async def on_request(self, event: IncomingRequest) -> None:
        """接收 request 事件，按类型分发。"""
        if event.request_type == RequestType.FRIEND:
            await self._handle_friend(event)
        elif event.request_type in (RequestType.GROUP_ADD, RequestType.GROUP_INVITE):
            await self._handle_group(event)
        else:
            logger.debug(f"未知 request 类型：{event.request_type}")

    async def _handle_friend(self, event: IncomingRequest) -> None:
        """处理好友请求。"""
        logger.info(f"好友请求: QQ {event.user_id}, flag={event.flag}")

        # 查询陌生人信息（失败容忍）
        user_info = await self._safe_user_info(event.user_id)
        nickname = user_info.get("nickname", "未知")

        info = PendingRequestInfo(
            type="friend",
            user_id=event.user_id,
            nickname=nickname,
            comment=event.comment or "无附加消息",
            flag=event.flag,
            expires_at=time.time() + self.request_timeout,
        )
        self.pending_requests.put(info)

        # 触发 Agent 通知管理员
        task_context = (
            f"现在是{get_time()}。收到好友添加请求。\n"
            f"请求者信息：QQ {event.user_id}，昵称 {nickname}，"
            f"附加消息：{info.comment}。【操作标识 flag={event.flag}】\n"
            f"请按照验证审核规则处理：先通知管理员（发送私聊），等待管理员回复。"
            f"管理员同意则调用对应工具通过，否则不操作。不要自行决定。"
        )
        await self.pipeline.run_one_turn(task_context)

    async def _handle_group(self, event: IncomingRequest) -> None:
        """处理加群/邀请请求。"""
        sub_type = "add" if event.request_type == RequestType.GROUP_ADD else "invite"
        sub_label = "加群申请" if sub_type == "add" else "邀请入群"
        logger.info(
            f"群请求({sub_label}): QQ {event.user_id}, 群 {event.group_id}, "
            f"flag={event.flag}"
        )

        user_info = await self._safe_user_info(event.user_id)
        nickname = user_info.get("nickname", "未知")

        info = PendingRequestInfo(
            type="group",
            user_id=event.user_id,
            nickname=nickname,
            comment=event.comment or "无附加消息",
            flag=event.flag,
            expires_at=time.time() + self.request_timeout,
            sub_type=sub_type,
            group_id=event.group_id,
        )
        self.pending_requests.put(info)

        task_context = (
            f"现在是{get_time()}。收到{sub_label}。\n"
            f"{sub_label}：请求者 QQ {event.user_id}，昵称 {nickname}，"
            f"群号 {event.group_id}，附加消息：{info.comment}。"
            f"sub_type={sub_type}，【操作标识 flag={event.flag}】\n"
            f"请按照验证审核规则处理：先通知管理员（发送私聊），等待管理员回复。"
            f"管理员同意则调用对应工具通过，否则不操作。不要自行决定。"
        )
        await self.pipeline.run_one_turn(task_context)

    async def _safe_user_info(self, user_id: str) -> dict:
        """获取用户信息，失败返回空 dict（不抛）。"""
        try:
            info = await self.pipeline.adapter.get_user_info(user_id)
            return {
                "nickname": info.nickname,
                "sex": info.sex,
                "age": info.age,
            }
        except Exception as e:
            logger.warning(f"获取陌生人信息失败 user_id={user_id}: {e}")
            return {}
