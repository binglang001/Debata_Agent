"""NapCat 事件解析 —— JSON → 统一事件类型。

NapCat 上报符合 OneBot V11 规范的 JSON。这里负责：
    1. 识别 post_type（message / notice / request / meta_event）
    2. 提取关键字段（user_id / group_id / message_id 等）
    3. 用 _parse_raw_message 解析 CQ 码（保留位置和重复 @ 等细节）
    4. 提取 media segments（图片/语音/文件 等），便于业务层统一处理
"""

from __future__ import annotations

import logging
import re
from typing import Any

from adapters.types import (
    AnyEvent,
    EventType,
    IncomingMessage,
    IncomingNotice,
    IncomingRequest,
    MediaSegment,
    MediaType,
    MetaEvent,
    NoticeType,
    RequestType,
)

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
    message_segments = raw.get("message", []) or []

    # 提取媒体段
    media = _extract_media(message_segments)

    # 提取 reply_to（如果有）
    reply_to: str | None = None
    for seg in message_segments:
        if isinstance(seg, dict) and seg.get("type") == "reply":
            rid = seg.get("data", {}).get("id")
            if rid:
                reply_to = str(rid)
                break

    # 重建可读文本（保留 @、表情、reply 等结构化标记）
    self_qq_for_display = self_id
    text = _parse_raw_message(raw_message_str, self_qq_for_display)

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
                    url=data.get("url") or data.get("file"),
                    file_id=data.get("file"),
                    extra={k: v for k, v in data.items() if k not in ("url", "file")},
                )
            )
        elif seg_type == "record":
            out.append(
                MediaSegment(
                    type=MediaType.VOICE,
                    file_id=data.get("file"),
                    url=data.get("url"),
                )
            )
        elif seg_type == "video":
            out.append(
                MediaSegment(
                    type=MediaType.VIDEO,
                    file_id=data.get("file"),
                    url=data.get("url"),
                )
            )
        elif seg_type == "file":
            out.append(
                MediaSegment(
                    type=MediaType.FILE,
                    file_id=data.get("file") or data.get("file_id"),
                    name=data.get("file_name") or data.get("name"),
                    url=data.get("url"),
                )
            )
        elif seg_type == "forward":
            out.append(
                MediaSegment(
                    type=MediaType.FORWARD,
                    file_id=data.get("id"),
                )
            )
    return out


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
    "notify": NoticeType.POKE,  # sub_type=poke 时
}


def _parse_notice(adapter_name: str, raw: dict[str, Any]) -> IncomingNotice | None:
    nt_raw = raw.get("notice_type", "")
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
# CQ 码解析（从旧 handler.py:_parse_raw_cq 迁移并增强）
# ============================================================


_CQ_PARAM_SPLIT = re.compile(r",(?=\w+=)")


def _parse_raw_message(raw: str, bot_qq: str) -> str:
    """把 CQ 码字符串解析为人类可读文本。

    保留：
        - @ 的精确位置和重复（绕过 NoneBot 适配器的合并优化）
        - reply 信息（提取到开头作为 [引用msg_id=xxx]）
        - 表情/图片/语音等占位

    这是从旧实现（handler.py:_parse_raw_cq）原样迁移过来的逻辑，仅作了输入参数化。
    """
    if not raw:
        return ""

    result: list[str] = []
    i = 0
    while i < len(raw):
        if raw[i : i + 4] == "[CQ:":
            end = raw.find("]", i)
            if end == -1:
                result.append(raw[i:])
                break
            cq = raw[i + 4 : end]
            if "," in cq:
                cq_type = cq[: cq.index(",")]
                params_str = cq[cq.index(",") + 1 :]
            else:
                cq_type = cq
                params_str = ""

            params: dict[str, str] = {}
            if params_str:
                for p in _CQ_PARAM_SPLIT.split(params_str):
                    if "=" in p:
                        k, v = p.split("=", 1)
                        params[k] = v

            if cq_type == "at":
                qq = params.get("qq", "")
                if qq == "all":
                    result.append("@全体成员")
                elif qq == bot_qq:
                    result.append("@我")
                else:
                    result.append(f"@QQ{qq}")
            elif cq_type == "reply":
                msg_id = params.get("id", "")
                result.insert(0, f"[引用msg_id={msg_id}]")
            elif cq_type == "image":
                result.append("[图片]")
            elif cq_type == "record":
                result.append("[语音]")
            elif cq_type == "video":
                result.append("[视频]")
            elif cq_type == "face":
                face_id = params.get("id", "")
                result.append(f"[表情{face_id}]")
            elif cq_type == "forward":
                fid = params.get("id", "")
                result.append(f"[合并转发 id={fid}]")
            elif cq_type == "file":
                result.append("[文件]")
            else:
                result.append(f"[{cq_type}]")
            i = end + 1
        else:
            result.append(raw[i])
            i += 1

    return "".join(result).strip()


# ============================================================
# 辅助
# ============================================================


def _opt_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v)
    return s if s else None
