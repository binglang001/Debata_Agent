"""NapCat 事件解析 —— JSON → 统一事件类型。

NapCat 上报符合 OneBot V11 规范的 JSON。这里负责：
    1. 识别 post_type（message / notice / request / meta_event）
    2. 提取关键字段（user_id / group_id / message_id 等）
    3. 用 utils.parse_raw_cq 解析 CQ 码（与 message_pipeline 共用一份实现）
    4. 提取 media segments（图片/语音/文件 等），便于业务层统一处理
"""

from __future__ import annotations

import logging
import re
from html import unescape
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from adapters.types import (
    AnyEvent,
    IncomingMessage,
    IncomingNotice,
    IncomingRequest,
    MediaSegment,
    MediaType,
    MetaEvent,
    NoticeType,
    RequestType,
)
from utils import parse_raw_cq

logger = logging.getLogger(__name__)


def parse_napcat_event(adapter_name: str, raw: dict[str, Any]) -> AnyEvent | None:
    """主入口：把 NapCat 上报的 JSON 转成统一事件类型。

    不识别或缺字段时返回 None（调用方应丢弃）。
    """
    post_type = raw.get("post_type")
    if post_type == "message":
        return _parse_message(adapter_name, raw)
    if post_type == "notice":
        return _parse_notice(adapter_name, raw)
    if post_type == "request":
        return _parse_request(adapter_name, raw)
    if post_type == "meta_event":
        return _parse_meta(adapter_name, raw)
    # 没有 post_type：通常是 API 响应（有 echo 字段），不应到达这里
    if "echo" in raw or "retcode" in raw:
        return None
    logger.debug(f"未识别的 NapCat 事件 post_type={post_type}: keys={list(raw.keys())}")
    return None


# ============================================================
# Message
# ============================================================


def _parse_message(adapter_name: str, raw: dict[str, Any]) -> IncomingMessage | None:
    try:
        msg_type = raw["message_type"]  # "private" / "group"
        message_id = str(raw["message_id"])
        user_id = str(raw["user_id"])
        self_id = str(raw.get("self_id", ""))
    except KeyError as e:
        logger.warning(f"消息事件缺字段: {e}")
        return None

    sender = raw.get("sender") or {}
    nickname = (
        sender.get("card") or sender.get("nickname") or "未知"
        if msg_type == "group"
        else sender.get("nickname") or "未知"
    )
    group_id = str(raw["group_id"]) if msg_type == "group" else None

    raw_message_str = raw.get("raw_message", "") or ""
    message_payload = raw.get("message", []) or []
    message_segments = message_payload if isinstance(message_payload, list) else []

    # 提取媒体段
    media = _extract_media(message_segments)
    if not media and raw_message_str:
        media = _extract_media_from_cq(raw_message_str)

    # 提取 reply_to（如果有）
    reply_to: str | None = None
    for seg in message_segments:
        if isinstance(seg, dict) and seg.get("type") == "reply":
            rid = seg.get("data", {}).get("id")
            if rid:
                reply_to = str(rid)
                break

    # 重建可读文本（保留 @、表情、reply 等结构化标记）
    # 与 message_pipeline._build_readable_text 用同一份实现，避免漂移
    text = parse_raw_cq(raw_message_str, self_id)
    if not text and message_segments:
        text = _render_onebot_segments(message_segments, self_id)

    return IncomingMessage(
        adapter=adapter_name,
        timestamp=float(raw.get("time", 0)),
        self_id=self_id,
        message_id=message_id,
        scope=msg_type,
        user_id=user_id,
        nickname=nickname,
        group_id=group_id,
        text=text,
        raw_message=raw_message_str,
        media=media,
        reply_to=reply_to,
        raw=raw,
    )


def _extract_media(segments: list[Any]) -> list[MediaSegment]:
    """从 message 段数组中提取媒体资源。"""
    out: list[MediaSegment] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type", "")
        data = seg.get("data", {}) or {}

        if seg_type == "image":
            out.append(
                MediaSegment(
                    type=MediaType.IMAGE,
                    url=_clean_cq_value(data.get("url") or data.get("file")),
                    file_id=_clean_cq_value(data.get("file")),
                    extra={k: v for k, v in data.items() if k not in ("url", "file")},
                )
            )
        elif seg_type == "record":
            out.append(
                MediaSegment(
                    type=MediaType.VOICE,
                    file_id=_clean_cq_value(data.get("file")),
                    url=_clean_cq_value(data.get("url")),
                )
            )
        elif seg_type == "video":
            out.append(
                MediaSegment(
                    type=MediaType.VIDEO,
                    file_id=_clean_cq_value(data.get("file")),
                    url=_clean_cq_value(data.get("url")),
                )
            )
        elif seg_type == "file":
            url = _clean_cq_value(data.get("url") or data.get("path") or data.get("file_path"))
            file_id = _clean_cq_value(data.get("file_id") or data.get("file"))
            name = _clean_cq_value(data.get("file_name") or data.get("name")) or _basename(url or file_id)
            out.append(
                MediaSegment(
                    type=MediaType.FILE,
                    file_id=file_id,
                    name=name,
                    url=url,
                )
            )
        elif seg_type == "forward":
            out.append(
                MediaSegment(
                    type=MediaType.FORWARD,
                    file_id=_clean_cq_value(data.get("id")),
                    extra=_forward_extra(data),
                )
            )
    return out


def _render_onebot_segments(segments: list[Any], self_id: str) -> str:
    """把 OneBot 结构化 message 段数组渲染为可读文本。"""
    head: list[str] = []
    body: list[str] = []
    for seg in segments:
        text = _render_onebot_segment(seg, self_id)
        if not text:
            continue
        if isinstance(seg, dict) and seg.get("type") == "reply":
            head.append(text)
        else:
            body.append(text)
    return "".join(head + body)


def _render_onebot_segment(segment: Any, self_id: str) -> str:
    if not isinstance(segment, dict):
        return str(segment)
    seg_type = str(segment.get("type") or "unknown")
    data = segment.get("data") if isinstance(segment.get("data"), dict) else {}

    if seg_type == "text":
        return unescape(str(data.get("text") or ""))
    if seg_type == "at":
        qq = str(data.get("qq") or "")
        if qq == "all":
            return "@全体成员"
        if qq == self_id:
            return "@我"
        return f"@QQ{qq}"
    if seg_type == "reply":
        return f"[引用msg_id={data.get('id') or ''}]"
    if seg_type == "image":
        return "[图片]"
    if seg_type in {"record", "voice"}:
        return "[语音]"
    if seg_type == "video":
        return "[视频]"
    if seg_type == "face":
        return f"[表情{data.get('id') or ''}]"
    if seg_type == "file":
        return "[文件]"
    if seg_type == "forward":
        return _forward_label(data.get("id") or segment.get("id") or "", data)
    if seg_type == "json":
        return "[JSON]"
    if seg_type == "node":
        return "[合并转发节点]"
    return f"[{seg_type}]"


_CQ_SEGMENT_RE = re.compile(r"\[CQ:(?P<body>[^\]]+)\]")
_PARAM_SPLIT_RE = re.compile(r",(?=\w+=)")


def _extract_media_from_cq(raw_message: str) -> list[MediaSegment]:
    """兼容 message 为字符串时的 CQ 媒体段。

    NapCat/OneBot 可以按字符串或数组上报 message。数组模式会带结构化 data；
    字符串模式只能从 raw_message 的 CQ 参数兜底提取 file/url/name。
    """
    out: list[MediaSegment] = []
    for match in _CQ_SEGMENT_RE.finditer(raw_message):
        body = match.group("body")
        if "," in body:
            cq_type, params_str = body.split(",", 1)
        else:
            cq_type, params_str = body, ""
        params = _parse_cq_params(params_str)
        if cq_type == "image":
            file_id = params.get("file")
            out.append(
                MediaSegment(
                    type=MediaType.IMAGE,
                    file_id=file_id,
                    url=params.get("url") or file_id,
                )
            )
        elif cq_type == "record":
            file_id = params.get("file")
            out.append(
                MediaSegment(
                    type=MediaType.VOICE,
                    file_id=file_id,
                    url=params.get("url"),
                )
            )
        elif cq_type == "video":
            file_id = params.get("file")
            out.append(
                MediaSegment(
                    type=MediaType.VIDEO,
                    file_id=file_id,
                    url=params.get("url"),
                )
            )
        elif cq_type == "file":
            url = params.get("url") or params.get("path") or params.get("file_path")
            file_id = params.get("file_id") or params.get("file")
            name = params.get("file_name") or params.get("name") or _basename(url or file_id)
            out.append(
                MediaSegment(
                    type=MediaType.FILE,
                    file_id=file_id,
                    name=name,
                    url=url,
                )
            )
        elif cq_type == "forward":
            out.append(
                MediaSegment(
                    type=MediaType.FORWARD,
                    file_id=params.get("id"),
                    extra=_forward_extra(params),
                )
            )
    return out


def _forward_label(forward_id: Any, data: dict[str, Any]) -> str:
    fid = _clean_cq_value(forward_id) or ""
    title = _forward_title(data)
    if title:
        return f"[合并转发 id={fid} title={title}]"
    return f"[合并转发 id={fid}]"


def _forward_title(data: dict[str, Any]) -> str | None:
    for key in ("title", "summary", "name", "content"):
        value = _clean_cq_value(data.get(key))
        if value:
            return value
    return None


def _forward_extra(data: dict[str, Any]) -> dict[str, str]:
    extra: dict[str, str] = {}
    for key in ("title", "summary", "name", "content"):
        value = _clean_cq_value(data.get(key))
        if value:
            extra[key] = value
    return extra


def _parse_cq_params(params_str: str) -> dict[str, str]:
    if not params_str:
        return {}
    params: dict[str, str] = {}
    for part in _PARAM_SPLIT_RE.split(params_str):
        if "=" in part:
            key, value = part.split("=", 1)
            params[key] = unescape(value)
    return params


def _clean_cq_value(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return unescape(str(value))


def _basename(value: str | None) -> str | None:
    if not value:
        return None
    if "\\" in value or re.match(r"^[A-Za-z]:", value):
        return PureWindowsPath(value).name
    return PurePosixPath(value).name


# ============================================================
# Notice
# ============================================================


_NOTICE_TYPE_MAP: dict[str, NoticeType] = {
    "group_recall": NoticeType.GROUP_RECALL,
    "friend_recall": NoticeType.FRIEND_RECALL,
    "group_increase": NoticeType.GROUP_INCREASE,
    "group_decrease": NoticeType.GROUP_DECREASE,
    "group_admin": NoticeType.GROUP_ADMIN,
    "friend_add": NoticeType.FRIEND_ADD,
}


def _parse_notice(adapter_name: str, raw: dict[str, Any]) -> IncomingNotice | None:
    nt_raw = raw.get("notice_type", "")
    if nt_raw == "notify":
        # notify 下还有 sub_type 进一步细分（poke / lucky_king / honor / ...）
        sub_type = raw.get("sub_type", "")
        if sub_type == "poke":
            nt = NoticeType.POKE
        else:
            nt = NoticeType.OTHER
    else:
        nt = _NOTICE_TYPE_MAP.get(nt_raw, NoticeType.OTHER)

    return IncomingNotice(
        adapter=adapter_name,
        timestamp=float(raw.get("time", 0)),
        self_id=str(raw.get("self_id", "")),
        notice_type=nt,
        user_id=_opt_str(raw.get("user_id")),
        group_id=_opt_str(raw.get("group_id")),
        operator_id=_opt_str(raw.get("operator_id")),
        message_id=_opt_str(raw.get("message_id")),
        raw=raw,
    )


# ============================================================
# Request
# ============================================================


def _parse_request(adapter_name: str, raw: dict[str, Any]) -> IncomingRequest | None:
    req_type = raw.get("request_type", "")
    if req_type == "friend":
        rt = RequestType.FRIEND
    elif req_type == "group":
        sub = raw.get("sub_type", "")
        rt = RequestType.GROUP_ADD if sub == "add" else RequestType.GROUP_INVITE
    else:
        logger.debug(f"未识别请求 request_type={req_type}")
        return None

    flag = str(raw.get("flag", ""))
    if not flag:
        logger.warning(f"请求事件缺 flag: {raw}")
        return None

    return IncomingRequest(
        adapter=adapter_name,
        timestamp=float(raw.get("time", 0)),
        self_id=str(raw.get("self_id", "")),
        request_type=rt,
        flag=flag,
        user_id=str(raw.get("user_id", "")),
        group_id=_opt_str(raw.get("group_id")),
        comment=raw.get("comment", "") or "",
        raw=raw,
    )


# ============================================================
# Meta
# ============================================================


def _parse_meta(adapter_name: str, raw: dict[str, Any]) -> MetaEvent:
    return MetaEvent(
        adapter=adapter_name,
        timestamp=float(raw.get("time", 0)),
        self_id=str(raw.get("self_id", "")),
        meta_type=raw.get("meta_event_type", ""),
        sub_type=raw.get("sub_type", ""),
        raw=raw,
    )


# ============================================================
# 辅助
# ============================================================


def _opt_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v)
    return s if s else None
