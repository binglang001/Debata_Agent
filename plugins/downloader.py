"""插件模型下载器 —— stream 下载 + sha256 + 重试 + 代理。

调用方：plugins.PluginManager.install(name, on_progress)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path

import httpx

from .base import DownloadProgressCallback, DownloadSource

logger = logging.getLogger(__name__)

_RETRY_INTERVALS = (1.0, 3.0, 5.0)


def _emit(
    cb: DownloadProgressCallback | None,
    filename: str,
    done: int,
    total: int,
    msg: str,
) -> None:
    """安全调用进度回调，回调异常不抛。"""
    if cb is None:
        return
    try:
        cb(filename, done, total, msg)
    except Exception:  # noqa: BLE001 —— UI 闭包失败不能炸下载流程
        pass


def _build_proxy_url() -> str | None:
    return (
        os.getenv("HTTPS_PROXY")
        or os.getenv("HTTP_PROXY")
        or os.getenv("https_proxy")
        or os.getenv("http_proxy")
    )


async def download_sources(
    sources: list[DownloadSource],
    target_dir: Path,
    on_progress: DownloadProgressCallback | None = None,
) -> None:
    """串行下载所有 sources 到 target_dir。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    proxy = _build_proxy_url()

    timeout = httpx.Timeout(connect=15.0, read=60.0, write=30.0, pool=15.0)
    headers = {
        "User-Agent": "Debata-Agent model downloader",
        "Accept": "*/*",
    }
    async with httpx.AsyncClient(
        proxy=proxy,
        follow_redirects=True,
        timeout=timeout,
        headers=headers,
    ) as client:
        for src in sources:
            dest = target_dir / src.dest_filename
            dest.parent.mkdir(parents=True, exist_ok=True)

            # 已存在且 sha256 匹配 → 跳过
            if dest.exists() and src.sha256:
                file_hash = await _sha256_file(dest)
                if file_hash == src.sha256:
                    logger.info(f"文件已存在且校验通过，跳过: {src.dest_filename}")
                    _emit(on_progress, src.dest_filename, src.size_bytes, src.size_bytes, "已完成（跳过）")
                    continue

            await _download_one(client, src, dest, on_progress)


async def _download_one(
    client: httpx.AsyncClient,
    src: DownloadSource,
    dest: Path,
    on_progress: DownloadProgressCallback | None,
) -> None:
    """下载单个文件，失败重试 3 次。"""
    last_error = ""
    for attempt in range(4):  # 1 次初试 + 3 次重试
        try:
            if attempt > 0:
                wait = _RETRY_INTERVALS[attempt - 1]
                logger.info(f"重试 {src.dest_filename}（第 {attempt} 次，等 {wait}s）")
                _emit(on_progress, src.dest_filename, 0, src.size_bytes, f"重试中({attempt}/3)")
                await asyncio.sleep(wait)
            await _stream_download(client, src, dest, on_progress)
            return
        except ValueError:
            raise  # sha256 失败不重试
        except asyncio.CancelledError:
            logger.info(f"下载已取消 {src.dest_filename}")
            _emit(on_progress, src.dest_filename, 0, src.size_bytes, "已取消")
            if dest.exists():
                try:
                    dest.unlink()
                except OSError:
                    pass
            raise
        except (httpx.HTTPError, OSError) as e:
            last_error = str(e) or repr(e)
            logger.warning(
                "下载失败 %s (attempt %s/4): %s: %s",
                src.dest_filename,
                attempt + 1,
                type(e).__name__,
                last_error,
            )
            # 清理残留
            if dest.exists():
                try:
                    dest.unlink()
                except OSError:
                    pass

    raise RuntimeError(f"文件 {src.dest_filename} 下载失败：网络错误重试耗尽。最后错误：{last_error}")


async def _stream_download(
    client: httpx.AsyncClient,
    src: DownloadSource,
    dest: Path,
    on_progress: DownloadProgressCallback | None,
) -> None:
    """stream 下载 + 进度回调 + sha256 校验。"""
    _emit(on_progress, src.dest_filename, 0, src.size_bytes, "开始下载")
    last_emit = 0.0
    sha = hashlib.sha256()

    async with client.stream("GET", src.url) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", -1)) if resp.headers.get("content-length") else -1
        done = 0
        with open(dest, "wb") as f:
            async for chunk in resp.aiter_bytes(65536):
                f.write(chunk)
                sha.update(chunk)
                done += len(chunk)
                now = time.monotonic()
                if now - last_emit >= 0.5:
                    _emit(on_progress, src.dest_filename, done, total, "下载中")
                    last_emit = now

    file_hash = sha.hexdigest()
    if src.sha256 and file_hash != src.sha256:
        try:
            dest.unlink()
        except OSError:
            pass
        raise ValueError(
            f"sha256 校验失败：{src.dest_filename} (期望 {src.sha256[:16]}..., 实际 {file_hash[:16]}...)"
        )

    _emit(on_progress, src.dest_filename, src.size_bytes, src.size_bytes, "完成")
    logger.info(f"下载完成: {src.dest_filename} ({src.size_bytes} bytes)")


async def _sha256_file(path: Path) -> str:
    """异步计算文件 sha256。"""
    import asyncio

    def _calc():
        sha = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha.update(chunk)
        return sha.hexdigest()

    return await asyncio.to_thread(_calc)
