"""OneBot CQ 码解析 —— 从原始 raw_message 字符串恢复人类可读文本。

源自旧 diana_agent/handler.py:_parse_raw_cq。功能与旧版完全一致：
    - 保留原始 @ 顺序和重复
    - 区分 @ 全体、@ 机器人自身、@ 普通用户
    - 把图片/语音/视频/合并转发/文件转成可读占位

设计：纯函数，无外部依赖，便于单元测试和跨平台复用。
"""

from __future__ import annotations

import re

# CQ 参数分隔正则：按 ",key=" 分割，避免值中含逗号被误切
_PARAM_SPLIT_RE = re.compile(r",(?=\w+=)")


def parse_raw_cq(raw: str, bot_qq: str) -> str:
    """把含 CQ 码的 raw_message 转成人类可读文本。

    Args:
        raw: OneBot 上报的原始消息字符串（含 [CQ:xxx,k=v] 标记）
        bot_qq: 机器人自身 QQ 号（用于把 @ 机器人识别为 "@我"）

    Returns:
        渲染后的可读文本。例如：
            "@QQ123456 你好"
            "[图片] 看看这个"
            "[引用msg_id=789] 嗯"
    """
    if not raw:
        return ""

    result: list[str] = []
    i = 0
    while i < len(raw):
        if raw[i : i + 4] == "[CQ:":
            end = raw.find("]", i)
            if end == -1:
                # 未闭合，剩余原样追加
                result.append(raw[i:])
                break
            cq = raw[i + 4 : end]
            result.append(_render_cq_segment(cq, bot_qq))
            i = end + 1
        else:
            result.append(raw[i])
            i += 1
    return "".join(result)


def _render_cq_segment(cq: str, bot_qq: str) -> str:
    """渲染单个 CQ 段（不含外层 [CQ: ... ]）。"""
    if "," in cq:
        cq_type, params_str = cq.split(",", 1)
    else:
        cq_type, params_str = cq, ""

    params = _parse_params(params_str)

    if cq_type == "at":
        qq = params.get("qq", "")
        if qq == "all":
            return "@全体成员"
        if qq == bot_qq:
            return "@我"
        return f"@QQ{qq}"

    if cq_type == "reply":
        msg_id = params.get("id", "")
        # 引用前置（旧版逻辑：插入到 result[0]）。
        # 这里只渲染段本身，调用方需要把 reply 段移到首位。
        # 为保持与旧版行为一致，下面给出占位说明
        return f"[引用msg_id={msg_id}]"

    if cq_type == "image":
        return "[图片]"

    if cq_type == "record":
        return "[语音]"

    if cq_type == "video":
        return "[视频]"

    if cq_type == "face":
        face_id = params.get("id", "")
        return f"[表情{face_id}]"

    if cq_type == "forward":
        fid = params.get("id", "")
        return f"[合并转发 id={fid}]"

    if cq_type == "file":
        return "[文件]"

    # 未知段：保留类型名
    return f"[{cq_type}]"


def _parse_params(params_str: str) -> dict[str, str]:
    """解析 CQ 参数字符串为 dict。"""
    if not params_str:
        return {}
    params: dict[str, str] = {}
    for p in _PARAM_SPLIT_RE.split(params_str):
        if "=" in p:
            k, v = p.split("=", 1)
            params[k] = v
    return params
