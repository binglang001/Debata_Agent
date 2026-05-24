"""NapCat WebSocket 连接管理。

两种连接模式（在配置中通过 mode 字段选择，命名从 Diana_Agent 视角）：

    client（推荐）：
        程序作为 WS 客户端，主动连 NapCat 的 WS 服务（如 ws://127.0.0.1:3001）。
        NapCat 端配「正向 WS」服务监听。
        优势：便于程序托管 NapCat 进程；无需开放本地端口。

    server：
        程序作为 WS 服务端监听端口，NapCat 作为客户端反向连入。
        NapCat 端配「反向 WS」客户端，目标地址填本程序的监听地址。
        旧 NoneBot 用户的默认行为。

通用特性：
    - 心跳检测（websockets 自带 ping/pong）
    - 断线自动重连（指数退避，封顶 60 秒）
    - 收发消息回调式投递，非阻塞
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from typing import Awaitable, Callable

import websockets
from websockets.asyncio.client import ClientConnection as _WSClient
from websockets.asyncio.server import ServerConnection as _WSServer

logger = logging.getLogger(__name__)


# NapCat 投递过来的原始 JSON 字典
MessageCallback = Callable[[dict], Awaitable[None]]


class NapCatConnection(ABC):
    """WebSocket 连接抽象基类。"""

    def __init__(self) -> None:
        self._callback: MessageCallback | None = None

    def on_message(self, callback: MessageCallback) -> None:
        """注册接收到 NapCat 消息时的回调。"""
        self._callback = callback

    async def _dispatch(self, data: dict) -> None:
        if self._callback is None:
            return
        try:
            await self._callback(data)
        except Exception as e:
            logger.exception(f"NapCat 消息回调失败: {type(e).__name__}: {e}")

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send(self, data: dict) -> None: ...

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...


class ReverseWSConnection(NapCatConnection):
    """程序作为客户端连接 NapCat（推荐模式）。"""

    def __init__(
        self,
        ws_url: str,
        access_token: str | None = None,
        *,
        reconnect_interval: float = 3.0,
        max_reconnect_attempts: int = -1,
        reconnect_backoff_max: float = 60.0,
        ping_interval: float = 20.0,
        ping_timeout: float = 20.0,
        initial_connect_timeout: float = 10.0,
    ) -> None:
        super().__init__()
        self.ws_url = ws_url
        self.access_token = access_token
        self.reconnect_interval = reconnect_interval
        self.max_reconnect_attempts = max_reconnect_attempts
        self.reconnect_backoff_max = reconnect_backoff_max
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.initial_connect_timeout = initial_connect_timeout

        self._ws: _WSClient | None = None
        self._loop_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._connected_event = asyncio.Event()

    @property
    def is_connected(self) -> bool:
        return self._ws is not None

    async def start(self) -> None:
        self._stop_event.clear()
        self._connected_event.clear()
        self._loop_task = asyncio.create_task(self._run_forever(), name="napcat-reverse-ws")

        # 等待首次连接成功（不强求，超时也继续——会持续重连）
        try:
            await asyncio.wait_for(
                self._connected_event.wait(), timeout=self.initial_connect_timeout
            )
            logger.info(f"NapCat 初次连接成功: {self.ws_url}")
        except asyncio.TimeoutError:
            logger.warning(
                f"NapCat 初次连接 {self.initial_connect_timeout}s 内未成功，"
                f"重连循环已在后台运行"
            )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

    async def send(self, data: dict) -> None:
        if self._ws is None:
            raise RuntimeError("NapCat 连接未建立")
        await self._ws.send(json.dumps(data, ensure_ascii=False))

    async def _run_forever(self) -> None:
        attempts = 0
        backoff = self.reconnect_interval

        while not self._stop_event.is_set():
            attempts += 1
            try:
                logger.info(f"连接 NapCat（第 {attempts} 次尝试）: {self.ws_url}")
                headers: dict[str, str] = {}
                if self.access_token:
                    headers["Authorization"] = f"Bearer {self.access_token}"

                async with websockets.connect(
                    self.ws_url,
                    additional_headers=headers if headers else None,
                    ping_interval=self.ping_interval,
                    ping_timeout=self.ping_timeout,
                    max_size=2**24,  # 16 MB，支持大消息（如长群历史）
                ) as ws:
                    self._ws = ws
                    self._connected_event.set()
                    attempts = 0
                    backoff = self.reconnect_interval
                    logger.info(f"NapCat WS 已连接: {self.ws_url}")

                    async for raw in ws:
                        if self._stop_event.is_set():
                            break
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError as e:
                            logger.warning(f"NapCat 消息 JSON 解析失败: {e}")
                            continue
                        # 异步分发，不阻塞接收循环
                        asyncio.create_task(self._dispatch(data))

                    logger.info("NapCat WS 连接已关闭")

            except asyncio.CancelledError:
                raise
            except (
                websockets.ConnectionClosed,
                ConnectionRefusedError,
                OSError,
            ) as e:
                logger.warning(f"NapCat 连接失败: {type(e).__name__}: {e}")
            except Exception as e:
                logger.exception(f"NapCat 连接异常: {e}")
            finally:
                self._ws = None
                self._connected_event.clear()

            if self._stop_event.is_set():
                break

            if 0 < self.max_reconnect_attempts <= attempts:
                logger.error(f"已达最大重连次数 {self.max_reconnect_attempts}，放弃")
                break

            logger.info(f"{backoff:.1f}s 后重试 NapCat 连接")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                break  # stop 信号到达
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, self.reconnect_backoff_max)


class ForwardWSConnection(NapCatConnection):
    """程序作为服务端监听 NapCat 反向连入（兼容旧 NoneBot 模式）。

    单连接模式：同一时刻只接受一个 NapCat 客户端连接。
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        path: str = "/onebot/v11/ws",
        access_token: str | None = None,
        *,
        ping_interval: float = 20.0,
        ping_timeout: float = 20.0,
    ) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.path = path
        self.access_token = access_token
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout

        self._server = None
        self._client: _WSServer | None = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    async def start(self) -> None:
        self._server = await websockets.serve(
            self._handle_client,
            self.host,
            self.port,
            ping_interval=self.ping_interval,
            ping_timeout=self.ping_timeout,
            max_size=2**24,
        )
        logger.info(
            f"NapCat 正向 WS 服务监听: ws://{self.host}:{self.port}{self.path}"
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

    async def send(self, data: dict) -> None:
        if self._client is None:
            raise RuntimeError("NapCat 尚未连入")
        await self._client.send(json.dumps(data, ensure_ascii=False))

    async def _handle_client(self, ws: _WSServer) -> None:
        # 路径校验
        request_path = getattr(ws.request, "path", "/")
        if request_path != self.path:
            logger.warning(f"NapCat 连入路径不匹配: {request_path}（期望 {self.path}）")
            await ws.close(code=4004, reason="path mismatch")
            return

        # Token 校验
        if self.access_token:
            auth = ws.request.headers.get("Authorization", "")
            expected = f"Bearer {self.access_token}"
            if auth != expected:
                if not auth:
                    logger.warning(
                        "NapCat 连入被拒：未带 Authorization。"
                        "程序配置了 access_token 但 NapCat 那边没填 token / 填错了。"
                    )
                else:
                    logger.warning(
                        "NapCat 连入被拒：Authorization 头与程序配置的 token 不一致。"
                        "请检查两端 token 是否完全相同。"
                    )
                await ws.close(code=4001, reason="unauthorized")
                return

        # 单连接：拒绝并发
        if self._client is not None:
            logger.warning("已有 NapCat 连接，拒绝新连接")
            await ws.close(code=4002, reason="already connected")
            return

        self._client = ws
        logger.info(f"NapCat 已连入: {ws.remote_address}")

        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("NapCat 消息 JSON 解析失败，跳过")
                    continue
                asyncio.create_task(self._dispatch(data))
        finally:
            self._client = None
            logger.info("NapCat 连接已断开")
