"""NapCat OneBot V11 API 调用层。

NapCat 的 WS 连接是双向的：
    - 客户端 → NapCat：发送 {action, params, echo} 调用 API
    - NapCat → 客户端：可能是事件上报（含 post_type），也可能是 API 响应（含 echo 和 retcode）

本模块用 echo 字段配对请求与响应：
    1. 调用 API 前生成唯一 echo
    2. 注册 asyncio.Future 等待该 echo 的响应
    3. NapCat 返回带 echo 的响应时唤醒对应 Future
    4. 调用方拿到响应或超时

并发安全：多个 API 调用可同时进行，互不阻塞。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from adapters.base import AdapterAPIError, AdapterNotConnectedError

from .connection import NapCatConnection

logger = logging.getLogger(__name__)


class NapCatApiCaller:
    """处理 OneBot V11 API 调用与响应分发。"""

    def __init__(
        self,
        connection: NapCatConnection,
        *,
        default_timeout: float = 30.0,
        wait_connected_timeout: float = 3.0,
    ) -> None:
        self._conn = connection
        self.default_timeout = default_timeout
        self.wait_connected_timeout = wait_connected_timeout
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    def handle_response(self, data: dict[str, Any]) -> bool:
        """处理一条 NapCat 消息：如果是 API 响应（含 echo），唤醒对应的 Future。

        返回 True 表示这条消息是 API 响应，业务层不必再处理为事件。
        """
        echo = data.get("echo")
        if not echo:
            return False  # 不是响应

        future = self._pending.pop(echo, None)
        if future is None:
            logger.warning(f"收到无主响应（echo={echo}）")
            return True

        if not future.done():
            future.set_result(data)
        return True

    async def call(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """调用一次 OneBot API。

        Args:
            action: API 名称（如 'send_private_msg'）
            params: 参数字典
            timeout: 超时秒数，默认 default_timeout

        Returns:
            响应的 data 字段（去掉 status/retcode/echo 外壳）

        Raises:
            AdapterNotConnectedError: 当前未连接 NapCat
            AdapterAPIError: NapCat 返回了错误状态
            asyncio.TimeoutError: 超时
        """
        if not self._conn.is_connected:
            if self.wait_connected_timeout > 0:
                ok = await self._conn.wait_connected(self.wait_connected_timeout)
                if not ok:
                    raise AdapterNotConnectedError(
                        f"NapCat 未连接，等待 {self.wait_connected_timeout:.1f}s 后仍无法调用 {action}"
                    )
            else:
                raise AdapterNotConnectedError(f"NapCat 未连接，无法调用 {action}")

        echo = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[echo] = future

        payload = {
            "action": action,
            "params": params or {},
            "echo": echo,
        }

        try:
            await self._conn.send(payload)
        except Exception as e:
            self._pending.pop(echo, None)
            raise AdapterAPIError(f"发送 API 请求失败: {e}") from e

        try:
            response = await asyncio.wait_for(
                future, timeout=timeout if timeout is not None else self.default_timeout
            )
        except asyncio.TimeoutError:
            self._pending.pop(echo, None)
            logger.warning(f"API 调用超时: {action} (echo={echo})")
            raise

        status = response.get("status", "")
        retcode = response.get("retcode", -1)
        if status != "ok" or retcode != 0:
            msg = response.get("message") or response.get("wording") or str(response)
            raise AdapterAPIError(f"API {action} 失败 (retcode={retcode}): {msg}")

        return response.get("data") or {}

    def discard_pending(self) -> None:
        """清空所有等待中的 Future（断线时调用，让调用方早点失败）。"""
        for _echo, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(
                    AdapterNotConnectedError("NapCat 连接已断开，请求被取消")
                )
        self._pending.clear()
