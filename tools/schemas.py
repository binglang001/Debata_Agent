"""所有工具的 Pydantic args 模型。

集中放在这里方便维护：要看支持哪些工具、各自的参数，看这个文件就够了。
每个模型对应一个工具，OpenAI tool schema 由 ToolSpec.to_openai_schema() 自动派生。

字段命名约定：
    - 与 OneBot 风格保持一致（user_id / group_id / message_id 等）
    - description 写给 LLM 看，要简明扼要描述使用时机
    - 默认值尽量给（让 LLM 调用时少传参）
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _ToolArgs(BaseModel):
    """所有工具 args 的基类。

    禁用 extra 字段以拦截 LLM 幻觉出未声明的参数。
    """

    model_config = ConfigDict(extra="forbid")


# ============================================================
# Messaging（消息发送 / 撤回 / 文件）
# ============================================================


class PrivateMessageTarget(_ToolArgs):
    """私聊消息中的单条目标项。"""

    target_qq: int = Field(..., description="接收者 QQ 号")
    content: str | None = Field(
        default=None,
        description="消息正文；可在开头加 [CQ:reply,id=msg_id] 引用回复。content 与 image 二选一。",
    )
    image: str | None = Field(
        default=None,
        description="表情包文件名（emoji 目录下的文件名）。content 与 image 二选一。",
    )
    order: int = Field(..., description="发送顺序，从小到大")
    delay: float | None = Field(
        default=None,
        description="本条发出后的等待秒数。不填时按消息长度自动估算。",
    )


class SendPrivateArgs(_ToolArgs):
    """send_private_messages 工具参数。"""

    targets: list[PrivateMessageTarget] = Field(
        ..., description="要发送的目标列表，至少 1 项"
    )
    send_only: bool = Field(
        default=False,
        description="True 则正常发送后直接结束本轮（不等待 LLM 反思）。",
    )


class GroupMessageTarget(_ToolArgs):
    """群聊消息中的单条目标项。"""

    content: str | None = Field(
        default=None,
        description="消息正文；@人用 [CQ:at,qq=QQ号]；引用用 [CQ:reply,id=msg_id]。",
    )
    image: str | None = Field(
        default=None, description="表情包文件名。content 与 image 二选一。"
    )
    order: int = Field(..., description="发送顺序，从小到大")
    delay: float | None = Field(
        default=None, description="本条发出后的等待秒数。不填时按长度估算。"
    )


class SendGroupArgs(_ToolArgs):
    """send_group_message 工具参数。"""

    group_id: int = Field(..., description="群号")
    targets: list[GroupMessageTarget] = Field(
        ..., description="要发送的消息列表，至少 1 项"
    )
    send_only: bool = Field(
        default=False, description="True 则正常发送后直接结束本轮。"
    )


class RecallMessageArgs(_ToolArgs):
    """recall_message 工具参数。"""

    message_id: int = Field(
        ..., description="要撤回的消息 ID。仅可撤回 2 分钟内发出的消息。"
    )


class UploadFileArgs(_ToolArgs):
    """upload_file 工具参数。"""

    target_type: Literal["private", "group"] = Field(
        ..., description="目标类型：私聊或群聊"
    )
    target_id: int = Field(..., description="QQ 号或群号")
    file_path: str = Field(
        ..., description="本地文件路径（必须在白名单目录下）"
    )
    file_name: str | None = Field(
        default=None, description="显示文件名。不填则用源文件名。"
    )


# ============================================================
# Memory（重要记忆）
# ============================================================


class SaveMemoryArgs(_ToolArgs):
    """save_important_memory 工具参数。"""

    memory_text: str = Field(
        ...,
        min_length=1,
        description="要永久保存的重要信息（人物、约定、秘密等）",
    )


class DeleteMemoryArgs(_ToolArgs):
    """delete_important_memory 工具参数。"""

    keyword: str = Field(
        ...,
        min_length=1,
        description="要删除的记忆关键词，模糊匹配包含此关键词的条目",
    )


# ============================================================
# Platform（联系人/群信息/请求处理）
# ============================================================


class ListContactsArgs(_ToolArgs):
    """list_contacts 工具参数。scope=group_members 时 group_id 必填。"""

    scope: Literal["friends", "groups", "group_members"] = Field(
        ..., description="查询范围：friends=好友列表, groups=群列表, group_members=指定群的成员"
    )
    group_id: int | None = Field(
        default=None, description="scope=group_members 时必填，填目标群号"
    )

    @model_validator(mode="after")
    def validate_group_id(self) -> ListContactsArgs:
        if self.scope == "group_members" and self.group_id is None:
            raise ValueError("scope=group_members 时 group_id 必填，请提供目标群号")
        return self


class GetUserInfoArgs(_ToolArgs):
    """get_user_info 工具参数。"""

    user_id: int = Field(..., description="目标用户的 QQ 号")


class GetForwardMsgArgs(_ToolArgs):
    """get_forward_msg 工具参数。"""

    forward_id: str = Field(
        ...,
        min_length=1,
        description="合并转发消息的 ID，从 [合并转发 id=xxx] 标记中获取",
    )


class SetFriendRequestArgs(_ToolArgs):
    """set_friend_add_request 工具参数。"""

    flag: str = Field(..., description="请求的 flag，从系统通知中获取")
    approve: bool = Field(..., description="是否同意")
    remark: str | None = Field(
        default=None, description="好友备注（通过时可选）"
    )


class SetGroupRequestArgs(_ToolArgs):
    """set_group_add_request 工具参数。"""

    flag: str = Field(..., description="请求的 flag，从系统通知中获取")
    sub_type: Literal["add", "invite"] = Field(
        ..., description="add=加群申请，invite=邀请入群"
    )
    approve: bool = Field(..., description="是否同意")
    reason: str | None = Field(
        default=None, description="拒绝理由（拒绝时可选）"
    )


class SummarizeChatArgs(_ToolArgs):
    """summarize_chat_history 工具参数。"""

    group_id: int = Field(..., description="要总结的群号")
    custom_prompt: str | None = Field(
        default=None, description="自定义总结提示词。不填使用默认提示。"
    )


# ============================================================
# Control（控制流）
# ============================================================


class NoActionArgs(_ToolArgs):
    """no_action 工具参数（无参数）。"""

    # 空模型即可——LLM 知道不传参数


class ScheduleWakeupArgs(_ToolArgs):
    """schedule_wakeup 工具参数。"""

    delay_seconds: int = Field(
        ..., ge=1, description="多少秒后唤醒（必须 ≥ 1）"
    )
    reminder: str = Field(
        ..., min_length=1, description="唤醒时收到的提醒内容"
    )


# ============================================================
# Feature（可选功能：视觉/搜索/天气）
# ============================================================


class DescribeImageArgs(_ToolArgs):
    """describe_image 工具参数。"""

    image_url: str = Field(
        ...,
        min_length=1,
        description="图片 URL，从消息中的 [图片 url=xxx] 标记获取",
    )
    prompt: str | None = Field(
        default=None,
        description="自定义理解提示，例如识别文字、分析表情。不填默认描述图片内容。",
    )


class WebSearchArgs(_ToolArgs):
    """web_search 工具参数。"""

    query: str = Field(
        ..., min_length=1, description="搜索关键词"
    )


class GetWeatherArgs(_ToolArgs):
    """get_weather 工具参数。"""

    city: str = Field(
        ..., min_length=1, description="城市名称，如 宁德、北京"
    )
    days: int = Field(
        default=1, ge=1, le=30, description="预报天数 1-30，默认 1"
    )
