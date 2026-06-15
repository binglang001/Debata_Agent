"""Media/readable-text helpers for MessagePipeline.

This module is a mechanical split from `core.message_pipeline`. Keep behavior
equivalent; do not change media placeholder, download, ASR, or file rendering
logic while moving methods.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from adapters.types import IncomingMessage, MediaSegment, MediaType
from utils import parse_raw_cq

logger = logging.getLogger("core.message_pipeline")


class MediaPipelineMixin:
    async def _build_readable_text(self, event: IncomingMessage) -> str:
        """把 IncomingMessage 重建为人类可读文本（CQ 解析 + 媒体 URL/转录附加）。

        events.parse_napcat_event 已经把 raw_message 走过 parse_raw_cq，
        结果存在 event.text 里。这里只在 event.text 为空（异常路径）时回退重算。
        然后扫描 event.media，把占位升级为含 URL / 转录 / workspace 路径的版本：
            - 图片：[图片] → [图片 url=... workspace=<相对路径>]
            - 语音：[语音] → [音频消息: 转录文本 workspace=<相对路径>]
            - 文件：[文件] → [文件 url=... workspace=<相对路径>]
        媒体文件会下载落地到 data/workspace/incoming/，让 AI 可以用 read_file 直接读。
        """
        text = event.text
        if not text and event.raw_message:
            bot_qq = str(getattr(event, "self_id", ""))
            text = parse_raw_cq(event.raw_message, bot_qq)
        text = text or ""

        # 升级媒体占位为含 URL / 转录的版本
        for seg in event.media:
            try:
                if seg.type == MediaType.IMAGE:
                    source = await self._image_media_source(seg)
                    ws_path = None
                    if source:
                        ws_path = await self._save_media_to_workspace(
                            source, suggested_name=f"img_{event.message_id}.jpg"
                        )
                    if ws_path and seg.url:
                        replacement = f"[图片 workspace={ws_path} url={seg.url}]"
                    elif ws_path:
                        replacement = f"[图片 workspace={ws_path}]"
                    elif seg.url:
                        replacement = f"[图片 url={seg.url}]"
                    else:
                        replacement = "[图片]"
                    if "[图片]" in text:
                        text = text.replace("[图片]", replacement, 1)
                    else:
                        text = f"{text} {replacement}".strip()
                elif seg.type in (MediaType.VOICE, MediaType.RECORD):
                    # 先把语音文件落到 workspace，供本地/API ASR 使用；失败不阻塞后续 fallback。
                    ws_path = None
                    if seg.url:
                        ws_path = await self._save_media_to_workspace(
                            seg.url, suggested_name=f"voice_{event.message_id}.amr"
                        )
                    voice_text = await self._transcribe_voice_with_asr(event, ws_path)
                    if not voice_text:
                        voice_text = await self._fetch_voice_text_from_adapter(event)
                    suffix_parts = []
                    if seg.url:
                        suffix_parts.append(f"url={seg.url}")
                    if ws_path:
                        suffix_parts.append(f"workspace={ws_path}")
                    suffix = f" {' '.join(suffix_parts)}" if suffix_parts else ""
                    placeholder = (
                        f"[音频消息: {voice_text}{suffix}]"
                        if voice_text
                        else f"[音频消息: 未识别{suffix}]"
                    )
                    if "[语音]" in text:
                        text = text.replace("[语音]", placeholder, 1)
                    else:
                        text = f"{text} {placeholder}".strip()
                elif seg.type == MediaType.FILE:
                    url: str | None = seg.url
                    if not url and seg.file_id:
                        try:
                            url = await self.adapter.get_file_url(seg.file_id)
                        except NotImplementedError:
                            url = None
                        except Exception as e:
                            logger.warning(f"获取文件 URL 失败 file_id={seg.file_id}: {e}")
                    source = url or seg.file_id
                    ws_path = None
                    if source:
                        ws_path = await self._save_media_to_workspace(
                            source,
                            suggested_name=seg.name
                            or f"file_{event.message_id}",
                        )
                    suffix_parts = []
                    if source:
                        suffix_parts.append(f"url={source}")
                    if ws_path:
                        suffix_parts.append(f"workspace={ws_path}")
                    suffix = f" {' '.join(suffix_parts)}" if suffix_parts else ""
                    replacement = (
                        f"[文件{suffix}]"
                        if source
                        else "[文件: 获取URL失败]"
                    )
                    text = text.replace("[文件]", replacement, 1)
                elif seg.type == MediaType.FORWARD:
                    replacement = await self._forward_media_text(seg)
                    text = self._replace_or_append_forward_text(text, seg, replacement)
            except Exception as e:
                # 单段媒体抽取失败不应阻塞主链路
                logger.exception(f"媒体段处理失败 type={seg.type}: {e}")

        return text

    async def _forward_media_text(self, seg: MediaSegment) -> str:
        """合并转发要让模型看到可展开线索，能读到时附带短预览。"""
        label = self._forward_media_label(seg)
        forward_id = (seg.file_id or "").strip()
        if not forward_id:
            return f"{label}：缺少 forward_id，无法自动读取内容。"

        adapter = getattr(self, "adapter", None)
        if adapter is None or not hasattr(adapter, "get_forward_msg"):
            return f"{label}：未自动读取内容；可调用 get_forward_msg(forward_id=\"{forward_id}\") 获取完整内容。"

        try:
            from tools.platform_tools import build_forward_tree, forward_tree_to_markdown

            tree = await build_forward_tree(
                adapter,
                forward_id,
                recursive=True,
                max_depth=2,
            )
        except NotImplementedError:
            return f"{label}：当前适配器不支持自动读取；可调用 get_forward_msg(forward_id=\"{forward_id}\") 尝试获取。"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"自动读取合并转发失败 forward_id={forward_id}: {e}")
            error = self._trim_forward_text(str(e), limit=120)
            return f"{label}：自动读取失败（{error}）；可调用 get_forward_msg(forward_id=\"{forward_id}\") 重试。"

        if tree.get("status") != "ok":
            error = self._trim_forward_text(str(tree.get("error") or "未知错误"), limit=120)
            return f"{label}：自动读取失败（{error}）；可调用 get_forward_msg(forward_id=\"{forward_id}\") 重试。"

        preview = self._trim_forward_preview(forward_tree_to_markdown(tree))
        if not preview:
            count = int(tree.get("message_count") or 0)
            return f"{label}：已读取 {count} 条，但预览为空；可调用 get_forward_msg(forward_id=\"{forward_id}\") 获取完整内容。"
        return (
            f"{label}\n节点预览：\n{preview}\n"
            f"如需完整内容，调用 get_forward_msg(forward_id=\"{forward_id}\")。"
        )

    @staticmethod
    def _forward_media_label(seg: MediaSegment) -> str:
        forward_id = (seg.file_id or "").strip()
        parts = [f"id={forward_id}"] if forward_id else ["id="]
        title = MediaPipelineMixin._forward_media_title(seg)
        if title:
            parts.append(f"title={title}")
        return "[合并转发 " + " ".join(parts) + "]"

    @staticmethod
    def _forward_media_title(seg: MediaSegment) -> str:
        extra = seg.extra if isinstance(seg.extra, dict) else {}
        for key in ("title", "summary", "name", "content"):
            value = extra.get(key)
            if value not in (None, ""):
                return MediaPipelineMixin._trim_forward_text(str(value), limit=80)
        if seg.name:
            return MediaPipelineMixin._trim_forward_text(seg.name, limit=80)
        return ""

    @staticmethod
    def _replace_or_append_forward_text(
        text: str,
        seg: MediaSegment,
        replacement: str,
    ) -> str:
        forward_id = (seg.file_id or "").strip()
        candidates = [
            MediaPipelineMixin._forward_media_label(seg),
            f"[合并转发 id={forward_id}]",
        ]
        for candidate in candidates:
            if candidate and candidate in text:
                return text.replace(candidate, replacement, 1)
        if replacement in text:
            return text
        return f"{text} {replacement}".strip()

    @staticmethod
    def _trim_forward_preview(markdown: str, *, limit: int = 1200) -> str:
        text = "\n".join(line.rstrip() for line in str(markdown or "").splitlines()).strip()
        return MediaPipelineMixin._trim_forward_text(text, limit=limit)

    @staticmethod
    def _trim_forward_text(text: str, *, limit: int) -> str:
        clean = " ".join(str(text or "").split())
        if len(clean) <= limit:
            return clean
        return clean[:limit].rstrip() + "...（截断）"

    async def _image_media_source(self, seg: MediaSegment) -> str | None:
        """优先用平台 file_id 换本地图片路径，普通 URL 只做兜底。"""
        resolver = getattr(self.adapter, "get_image_url", None)
        if seg.file_id and resolver is not None:
            try:
                source = await resolver(seg.file_id)
                if source:
                    import re
                    from urllib.parse import unquote, urlparse

                    source = str(source).strip()
                    if re.match(r"^[A-Za-z]:[\\/]", source) and Path(source).is_file():
                        return source
                    parsed = urlparse(source)
                    if parsed.scheme in ("http", "https"):
                        return source
                    if parsed.scheme == "file":
                        file_path = unquote(parsed.path)
                        if file_path.startswith("/") and len(file_path) > 3 and file_path[2] == ":":
                            file_path = file_path[1:]
                        if Path(file_path).is_file():
                            return source
                    elif not parsed.scheme and Path(source).is_file():
                        return source
            except (AttributeError, NotImplementedError):
                pass
            except Exception as e:  # noqa: BLE001
                logger.warning(f"获取图片文件失败 file_id={seg.file_id}: {e}")
        return seg.url or seg.file_id

    async def _transcribe_voice_with_asr(
        self, event: IncomingMessage, ws_path: str | None
    ) -> str:
        """优先使用已注入的 ASR 服务识别 workspace 中的语音文件。"""
        if self.asr is None:
            return ""
        if not ws_path or self.workspace_dir is None:
            logger.warning(
                f"ASR 已启用但语音文件不可用，回退适配器转写 msg_id={event.message_id}"
            )
            return ""
        audio_path = self.workspace_dir / ws_path
        try:
            text = await self.asr.transcribe(audio_path)
            return (text or "").strip()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"ASR 识别失败 msg_id={event.message_id}，回退适配器转写: {e}"
            )
            return ""

    async def _fetch_voice_text_from_adapter(self, event: IncomingMessage) -> str:
        """适配器自带语音转文字 fallback。"""
        try:
            text = await self.adapter.fetch_voice_text(event.message_id)
            return (text or "").strip()
        except NotImplementedError:
            return ""
        except Exception as e:
            logger.warning(f"适配器语音转文字失败 msg_id={event.message_id}: {e}")
            return ""

    async def _save_media_to_workspace(
        self, url: str, suggested_name: str
    ) -> str | None:
        """下载/复制媒体到 data/workspace/incoming/，返回相对 workspace 的路径。

        失败仅 warn，返回 None；不阻塞主链路。
        NapCat 文件消息可能给 http(s) URL，也可能给本机 temp 路径；两种都保存到 workspace。
        """
        if not self.workspace_dir:
            return None
        try:
            import re
            import shutil
            from html import unescape
            from urllib.parse import unquote, urlparse

            import httpx

            url = unescape((url or "").strip())
            incoming = self.workspace_dir / "incoming"
            incoming.mkdir(parents=True, exist_ok=True)
            parsed = urlparse(url)
            source_path: Path | None = None
            if re.match(r"^[A-Za-z]:[\\/]", url):
                source_path = Path(url)
            elif parsed.scheme == "file":
                file_path = unquote(parsed.path)
                if re.match(r"^/[A-Za-z]:/", file_path):
                    file_path = file_path[1:]
                source_path = Path(file_path)
            elif not parsed.scheme and not url.startswith(("http://", "https://")):
                source_path = Path(url)

            if source_path is not None and source_path.exists():
                if not Path(suggested_name).suffix and source_path.suffix:
                    suggested_name = f"{suggested_name}{source_path.suffix}"

            # 清理文件名：去掉路径分隔符与不安全字符
            safe_name = re.sub(r"[^\w.\-]", "_", suggested_name)[:80] or "file.bin"
            dest = incoming / safe_name
            # 同名加 _1 _2 后缀
            counter = 0
            while dest.exists():
                counter += 1
                stem = dest.stem
                suffix = dest.suffix
                # 去掉旧 counter 后缀
                stem = re.sub(r"_\d+$", "", stem)
                dest = incoming / f"{stem}_{counter}{suffix}"

            if source_path is not None:
                if not source_path.exists() or not source_path.is_file():
                    return None
                await asyncio.to_thread(shutil.copy2, source_path, dest)
            else:
                if not url.startswith(("http://", "https://")):
                    return None
                async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    await asyncio.to_thread(dest.write_bytes, resp.content)
            return f"incoming/{dest.name}"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"下载媒体到 workspace 失败 url={url[:60]}: {e}")
            return None
