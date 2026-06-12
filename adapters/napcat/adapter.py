"""NapCat 适配器主类 —— IAdapter 的 NapCat 实现。

组装：
    NapCatConnection（连接层）+ NapCatApiCaller（API 调用）
    + parse_napcat_event（事件解析）+ NapCatProcessManager（可选进程托管）

数据流：
    入：NapCat → connection → on_message(raw_dict)
         → api_caller.handle_response 拦截 API 响应
         → 剩下的当事件解析 → IncomingMessage/Notice/Request
         → self._emit(event) 投递给业务层

    出：业务层调 send_text/send_image/... → 内部 api_caller.call("send_msg", ...)
         → connection.send(JSON) → NapCat
"""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import Any, Literal

from adapters.base import (
    AdapterAPIError,
    IAdapter,
)
from adapters.types import (
    FriendInfo,
    GroupInfo,
    GroupMemberInfo,
    Target,
    UserInfo,
)
from app_config.schema import NapCatAdapterConfig
from app_config.secrets import SecretsManager

from .api_call import NapCatApiCaller
from .connection import (
    ForwardWSConnection,
    NapCatConnection,
    ReverseWSConnection,
)
from .events import parse_napcat_event
from .process import NapCatProcessManager

logger = logging.getLogger(__name__)


class NapCatAdapter(IAdapter):
    """OneBot V11 协议的 NapCat 实现。"""

    def __init__(
        self,
        name: str,
        connection: NapCatConnection,
        *,
        process_manager: NapCatProcessManager | None = None,
        api_timeout_seconds: float = 30.0,
        api_wait_connected_timeout_seconds: float = 3.0,
        process_warmup_seconds: float = 2.0,
        voice_fetch_delay_seconds: float = 1.0,
    ) -> None:
        super().__init__(name)
        self._connection = connection
        self._api = NapCatApiCaller(
            connection,
            default_timeout=api_timeout_seconds,
            wait_connected_timeout=api_wait_connected_timeout_seconds,
        )
        self._process = process_manager
        self._process_warmup_seconds = process_warmup_seconds
        self._voice_fetch_delay_seconds = voice_fetch_delay_seconds

        # 安装消息分发
        connection.on_message(self._on_napcat_message)
        connection.on_connection_lost(self._api.discard_pending)

    # ============================================================
    # 工厂方法
    # ============================================================

    @classmethod
    def from_config(
        cls,
        name: str,
        cfg: NapCatAdapterConfig,
        secrets: SecretsManager,
    ) -> NapCatAdapter:
        """从配置构造。所有 timeout / heartbeat / 重连参数均来自 cfg。"""
        access_token = None
        if cfg.access_token_id:
            access_token = secrets.get(cfg.access_token_id)
            if access_token is None:
                # 友好提示：常见误用是把 token 值本身当 ID 填到 access_token_id 字段
                hint = ""
                if len(cfg.access_token_id) >= 12 and not cfg.access_token_id.replace(
                    "_", ""
                ).isalpha():
                    hint = (
                        "\n  提示：access_token_id 应该是 secrets 里的引用 ID "
                        "（如 'napcat_default_token'），不是 token 的实际值。"
                        "你现在填的看起来像 token 本身。"
                    )
                logger.warning(
                    f"NapCat 配置引用的 access_token_id={cfg.access_token_id!r} "
                    f"在 secrets 中找不到，将不使用 token{hint}"
                )

        # 两种模式用同一组 host/port/path 字段（语义对称）
        if cfg.mode == "client":
            # 程序作为 WS 客户端，连 ws://{host}:{port}{path}（NapCat = 正向 WS 服务端）
            ws_url = f"ws://{cfg.host}:{cfg.port}{cfg.path}"
            connection: NapCatConnection = ReverseWSConnection(
                ws_url=ws_url,
                access_token=access_token,
                reconnect_interval=cfg.reconnect_interval_seconds,
                max_reconnect_attempts=cfg.max_reconnect_attempts,
                reconnect_backoff_max=cfg.reconnect_backoff_max_seconds,
                ping_interval=cfg.ping_interval_seconds,
                ping_timeout=cfg.ping_timeout_seconds,
                initial_connect_timeout=cfg.startup_connect_timeout_seconds,
                fast_reconnect_attempts=cfg.fast_reconnect_attempts,
                fast_reconnect_interval=cfg.fast_reconnect_interval_seconds,
                reconnect_jitter=cfg.reconnect_jitter_seconds,
            )
        else:  # mode == "server"
            # 程序作为 WS 服务端，监听 {host}:{port}{path} 等 NapCat 反向连入
            connection = ForwardWSConnection(
                host=cfg.host,
                port=cfg.port,
                path=cfg.path,
                access_token=access_token,
                ping_interval=cfg.ping_interval_seconds,
                ping_timeout=cfg.ping_timeout_seconds,
            )

        process_manager: NapCatProcessManager | None = None
        if cfg.manage_process and cfg.process_path:
            process_manager = NapCatProcessManager(
                executable=cfg.process_path,
                args=list(cfg.process_args),
                auto_restart=cfg.auto_restart,
            )

        return cls(
            name=name,
            connection=connection,
            process_manager=process_manager,
            api_timeout_seconds=cfg.api_timeout_seconds,
            api_wait_connected_timeout_seconds=cfg.api_wait_connected_timeout_seconds,
            process_warmup_seconds=cfg.process_warmup_seconds,
            voice_fetch_delay_seconds=cfg.voice_fetch_delay_seconds,
        )

    # ============================================================
    # 生命周期
    # ============================================================

    async def start(self) -> None:
        if self._process is not None:
            await self._process.start()
            # 给 NapCat 一点启动时间再尝试连接
            await asyncio.sleep(self._process_warmup_seconds)
        await self._connection.start()

    async def stop(self) -> None:
        await self._connection.stop()
        self._api.discard_pending()
        if self._process is not None:
            await self._process.stop()

    @property
    def is_connected(self) -> bool:
        return self._connection.is_connected

    # ============================================================
    # 入站消息分发
    # ============================================================

    async def _on_napcat_message(self, data: dict[str, Any]) -> None:
        """NapCat 每条消息都流经这里。"""
        # API 响应优先
        if self._api.handle_response(data):
            return

        # 事件
        event = parse_napcat_event(self.name, data)
        if event is None:
            return
        await self._emit(event)

    # ============================================================
    # 消息发送
    # ============================================================

    async def send_text(self, target: Target, content: str) -> str | None:
        params = self._build_send_params(target, content)
        result = await self._api.call("send_msg", params)
        mid = result.get("message_id")
        return str(mid) if mid is not None else None

    async def send_image(
        self,
        target: Target,
        *,
        image_path: Path | None = None,
        image_url: str | None = None,
        image_b64: str | None = None,
    ) -> str | None:
        if image_path is not None:
            def _read():
                return image_path.read_bytes()
            raw = await asyncio.to_thread(_read)
            image_b64 = base64.b64encode(raw).decode("ascii")

        if image_b64:
            cq = f"[CQ:image,file=base64://{image_b64}]"
        elif image_url:
            cq = f"[CQ:image,file={image_url}]"
        else:
            raise ValueError("必须提供 image_path / image_url / image_b64 之一")

        return await self.send_text(target, cq)

    async def recall(self, message_id: str) -> bool:
        try:
            await self._api.call("delete_msg", {"message_id": int(message_id)})
            return True
        except (AdapterAPIError, ValueError) as e:
            logger.warning(f"撤回失败: {e}")
            return False

    async def send_voice(
        self, target: Target, audio_path: Path
    ) -> str | None:
        """通过 OneBot V11 record 段发送语音。"""
        file_uri = "file:///" + str(audio_path.absolute()).replace("\\", "/")
        message = [{"type": "record", "data": {"file": file_uri}}]
        params: dict[str, Any] = {
            "message_type": target.scope,
            "message": message,
        }
        if target.scope == "private":
            params["user_id"] = int(target.target_id)
        elif target.scope == "group":
            params["group_id"] = int(target.target_id)
        else:
            raise ValueError(f"未知 target_type: {target.scope}")

        try:
            result = await self._api.call("send_msg", params)
        except AdapterAPIError as e:
            msg = str(e)
            if "retcode=1200" in msg and "Timeout" in msg:
                logger.warning(
                    "NapCat send_voice 回包超时，消息可能已实际发出；按疑似成功处理: %s",
                    e,
                )
                return None
            raise
        mid = result.get("message_id")
        return str(mid) if mid is not None else None

    # ============================================================
    # 联系人查询
    # ============================================================

    async def list_friends(self) -> list[FriendInfo]:
        data = await self._api.call("get_friend_list", {})
        items = data if isinstance(data, list) else data.get("friends", [])
        return [
            FriendInfo(
                user_id=str(f.get("user_id", "")),
                nickname=f.get("nickname", "") or "",
                remark=f.get("remark", "") or "",
            )
            for f in items
        ]

    async def list_groups(self) -> list[GroupInfo]:
        data = await self._api.call("get_group_list", {})
        items = data if isinstance(data, list) else data.get("groups", [])
        return [
            GroupInfo(
                group_id=str(g.get("group_id", "")),
                group_name=g.get("group_name", "") or "",
                member_count=int(g.get("member_count", 0) or 0),
            )
            for g in items
        ]

    async def list_group_members(self, group_id: str) -> list[GroupMemberInfo]:
        data = await self._api.call("get_group_member_list", {"group_id": int(group_id)})
        items = data if isinstance(data, list) else data.get("members", [])
        out: list[GroupMemberInfo] = []
        for m in items:
            role = m.get("role", "member")
            if role not in ("owner", "admin", "member"):
                role = "member"
            out.append(
                GroupMemberInfo(
                    user_id=str(m.get("user_id", "")),
                    nickname=m.get("nickname", "") or "",
                    card=m.get("card", "") or "",
                    role=role,
                )
            )
        return out

    async def get_user_info(self, user_id: str) -> UserInfo:
        data = await self._api.call("get_stranger_info", {"user_id": int(user_id)})
        return UserInfo(
            user_id=str(data.get("user_id", user_id)),
            nickname=data.get("nickname", "") or "",
            sex=data.get("sex", "unknown") or "unknown",
            age=int(data.get("age", 0) or 0),
            extra={k: v for k, v in data.items() if k not in ("user_id", "nickname", "sex", "age")},
        )

    # ============================================================
    # 请求处理
    # ============================================================

    async def handle_friend_request(
        self, flag: str, approve: bool, remark: str = ""
    ) -> None:
        await self._api.call(
            "set_friend_add_request",
            {"flag": flag, "approve": approve, "remark": remark},
        )

    async def handle_group_request(
        self,
        flag: str,
        sub_type: Literal["add", "invite"],
        approve: bool,
        reason: str = "",
    ) -> None:
        await self._api.call(
            "set_group_add_request",
            {
                "flag": flag,
                "sub_type": sub_type,
                "approve": approve,
                "reason": reason,
            },
        )

    # ============================================================
    # 媒体辅助（NapCat 专属 API）
    # ============================================================

    async def fetch_voice_text(self, message_id: str) -> str:
        """NapCat 的 fetch_ptt_text Action。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 8.0
        await asyncio.sleep(min(max(self._voice_fetch_delay_seconds, 0.0), 8.0))
        last_error: Exception | None = None
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                data = await self._api.call(
                    "fetch_ptt_text",
                    {"message_id": int(message_id)},
                    timeout=max(0.1, min(3.0, remaining)),
                )
                return data.get("text", "") or ""
            except (AdapterAPIError, ValueError) as e:
                last_error = e
                msg = str(e)
                retryable = (
                    "获取语音转文字结果失败" in msg
                    or ("retcode=1200" in msg and "fetch_ptt_text" in msg)
                )
                remaining = deadline - loop.time()
                if not retryable or remaining <= 0:
                    break
                await asyncio.sleep(min(1.5, max(0.1, remaining)))
            except asyncio.TimeoutError as e:
                last_error = e
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(1.0, max(0.1, remaining)))
        if last_error is not None:
            logger.warning(f"语音转文字失败 msg_id={message_id}: {last_error}")
        return ""

    async def get_file_url(self, file_id: str) -> str | None:
        if file_id and Path(file_id).exists():
            return file_id
        last_error: Exception | None = None
        for params in (
            {"file_id": file_id, "type": "url"},
            {"file_id": file_id},
            {"file": file_id},
        ):
            try:
                data = await self._api.call("get_file", params)
                value = (
                    data.get("url")
                    or data.get("file")
                    or data.get("file_path")
                    or data.get("path")
                    or None
                )
                if value:
                    return value
            except AdapterAPIError as e:
                last_error = e
                msg = str(e).lower()
                if "no such file" in msg or "not found" in msg or "不存在" in msg:
                    break
                continue
            except asyncio.TimeoutError as e:
                last_error = e
                break
        if last_error is not None:
            logger.warning(f"获取文件 URL 失败 file_id={file_id}: {last_error}")
        return None

    async def upload_file(
        self,
        target: Target,
        file_path: Path,
        *,
        display_name: str | None = None,
    ) -> None:
        if target.scope == "private":
            params: dict[str, Any] = {
                "user_id": int(target.target_id),
                "file": str(file_path),
                "name": display_name or file_path.name,
            }
            await self._api.call("upload_private_file", params)
        elif target.scope == "group":
            params = {
                "group_id": int(target.target_id),
                "file": str(file_path),
                "name": display_name or file_path.name,
            }
            await self._api.call("upload_group_file", params)
        else:
            raise ValueError(f"未知 scope: {target.scope}")

    async def get_forward_msg(self, forward_id: str) -> list[dict[str, Any]]:
        data = await self._api.call("get_forward_msg", {"id": forward_id})
        messages = data.get("messages", [])
        return messages if isinstance(messages, list) else []

    async def get_group_history(
        self, group_id: str, count: int = 100
    ) -> list[dict[str, Any]]:
        data = await self._api.call(
            "get_group_msg_history",
            {"group_id": int(group_id), "count": count},
        )
        messages = data.get("messages", [])
        return messages if isinstance(messages, list) else []

    # ============================================================
    # 通用 API 通道
    # ============================================================

    async def call_api(self, action: str, **params: Any) -> dict[str, Any]:
        return await self._api.call(action, params)

    # ============================================================
    # 内部辅助
    # ============================================================

    def _build_send_params(self, target: Target, content: str) -> dict[str, Any]:
        """根据 Target 构造 send_msg 的参数。"""
        if target.scope == "private":
            return {
                "message_type": "private",
                "user_id": int(target.target_id),
                "message": content,
            }
        elif target.scope == "group":
            return {
                "message_type": "group",
                "group_id": int(target.target_id),
                "message": content,
            }
        else:
            raise ValueError(f"未知 scope: {target.scope}")
