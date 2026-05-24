"""NapCat 进程托管 —— 可选地拉起并监控 NapCat 子进程。

启用时机：
    用户在配置中填了 process_path 且 manage_process=True。

行为：
    - start() 拉起 NapCat 进程
    - 监控进程状态：崩溃后按 auto_restart 决定是否重启
    - stop() 优雅终止：先发 terminate，等待若干秒后 SIGKILL

进程托管不影响 WS 连接逻辑：连接模块照常重连即可，进程模块只确保 NapCat 存活。
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


class NapCatProcessManager:
    """管理 NapCat 子进程的生命周期。"""

    def __init__(
        self,
        executable: str | Path,
        args: list[str] | None = None,
        *,
        auto_restart: bool = True,
        restart_delay: float = 3.0,
        graceful_shutdown_timeout: float = 5.0,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.executable = Path(executable)
        self.args = args or []
        self.auto_restart = auto_restart
        self.restart_delay = restart_delay
        self.graceful_shutdown_timeout = graceful_shutdown_timeout
        self.cwd = Path(cwd) if cwd else self.executable.parent
        self.env = env

        self._process: asyncio.subprocess.Process | None = None
        self._monitor_task: asyncio.Task | None = None
        self._stop_requested = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        """启动 NapCat 进程并开始监控。"""
        if not self.executable.exists():
            raise FileNotFoundError(f"NapCat 可执行文件不存在: {self.executable}")
        if self._process is not None:
            logger.warning("NapCat 进程已在运行")
            return

        self._stop_requested.clear()
        await self._spawn_once()
        self._monitor_task = asyncio.create_task(self._monitor_loop(), name="napcat-process-monitor")

    async def stop(self) -> None:
        """优雅停止 NapCat 进程。"""
        self._stop_requested.set()

        if self._process is not None and self._process.returncode is None:
            logger.info("正在终止 NapCat 进程")
            try:
                if sys.platform == "win32":
                    self._process.terminate()
                else:
                    self._process.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass

            try:
                await asyncio.wait_for(
                    self._process.wait(), timeout=self.graceful_shutdown_timeout
                )
            except asyncio.TimeoutError:
                logger.warning(f"NapCat 未在 {self.graceful_shutdown_timeout}s 内退出，强制 kill")
                try:
                    self._process.kill()
                    await self._process.wait()
                except ProcessLookupError:
                    pass

        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None

        self._process = None
        logger.info("NapCat 进程已停止")

    async def _spawn_once(self) -> None:
        """启动一次进程（不带重启逻辑）。"""
        cmd = [str(self.executable), *self.args]
        logger.info(f"启动 NapCat: {cmd} (cwd={self.cwd})")

        kwargs: dict = {
            "cwd": str(self.cwd),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
        }
        if self.env is not None:
            new_env = dict(os.environ)
            new_env.update(self.env)
            kwargs["env"] = new_env

        if sys.platform == "win32":
            # Windows 下创建新进程组，便于 terminate 不影响父进程
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        self._process = await asyncio.create_subprocess_exec(*cmd, **kwargs)
        logger.info(f"NapCat 已启动 pid={self._process.pid}")

        # 启动一个 task 持续读取 NapCat stdout 转到日志
        if self._process.stdout is not None:
            asyncio.create_task(
                self._pipe_logs(self._process.stdout),
                name=f"napcat-stdout-{self._process.pid}",
            )

    async def _pipe_logs(self, stream: asyncio.StreamReader) -> None:
        """把 NapCat 输出转发到日志系统。"""
        while True:
            try:
                line = await stream.readline()
            except (ValueError, asyncio.CancelledError):
                break
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                logger.info(f"[NapCat] {text}")

    async def _monitor_loop(self) -> None:
        """监控进程退出 → 根据策略重启。"""
        while not self._stop_requested.is_set():
            if self._process is None:
                await asyncio.sleep(0.1)
                continue

            rc = await self._process.wait()
            if self._stop_requested.is_set():
                logger.info(f"NapCat 进程已退出（手动停止）: rc={rc}")
                return

            logger.warning(f"NapCat 进程意外退出: rc={rc}")
            self._process = None

            if not self.auto_restart:
                logger.info("auto_restart=False，不重启")
                return

            try:
                await asyncio.wait_for(
                    self._stop_requested.wait(), timeout=self.restart_delay
                )
                return  # 等待期间收到 stop 信号
            except asyncio.TimeoutError:
                pass

            try:
                await self._spawn_once()
            except Exception as e:
                logger.error(f"NapCat 重启失败: {e}")
                if not self.auto_restart:
                    return
