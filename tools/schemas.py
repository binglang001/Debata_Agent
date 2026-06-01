"""所有工具的 Pydantic args 模型。

集中放在这里方便维护：要看支持哪些工具、各自的参数，看这个文件就够了。
每个模型对应一个工具，OpenAI tool schema 由 ToolSpec.to_openai_schema() 自动派生。

字段命名约定：
    - 与 OneBot 风格保持一致（user_id / group_id / message_id 等）
    - description 写给 LLM 看，要简明扼要描述使用时机
    - 默认值尽量给（让 LLM 调用时少传参）
"""

from __future__ import annotations

from typing import Any, Literal

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
        ...,
        description=(
            "要发送的本地文件路径，必须位于 workspace 内。"
            "可传相对路径如 'report.pdf'；不要传用户电脑其它目录里的路径。"
        ),
    )
    file_name: str | None = Field(
        default=None, description="发给 QQ 时显示的文件名。可选；不填时自动使用 file_path 的文件名。"
    )


# ============================================================
# Memory（重要记忆）
# ============================================================


class SaveMemoryArgs(_ToolArgs):
    """save_important_memory 工具参数。"""

    memory_text: str = Field(
        ...,
        min_length=1,
        description=(
            "要长期保存的一句话重要信息，仅用于人物身份、偏好、稳定约定、长期目标等。"
            "不要保存普通寒暄、一次性请求或已经过期的信息。"
        ),
    )
    scope: str | None = Field(
        default=None,
        description=(
            "记忆适用范围。默认由当前会话自动推断；可填 global、user:QQ号 或 group:群号。"
        ),
    )
    pinned: bool = Field(
        default=False,
        description="是否置顶。置顶记忆会在任何会话中注入，只用于非常稳定且全局重要的信息。",
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
        ...,
        description=(
            "查询范围：friends=好友列表，groups=群列表，group_members=某个群的成员列表。"
            "当你不知道 QQ 号/群号或需要找联系人时使用。"
        ),
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
        default=None,
        description="自定义总结目标。不填会总结群成员概况、群聊基本信息、日常话题和成员印象。",
    )


class SummarizeConversationArgs(_ToolArgs):
    """summarize_conversation 工具参数。"""

    conversation_id: str | None = Field(
        default=None,
        description=(
            "要总结的本地会话标签，如 private:430666862 或 group:1087440069。"
            "不填则使用当前会话；仍无法判断时总结本地全局历史。"
        ),
    )
    range_hint: str | None = Field(
        default=None,
        description="用户给出的范围线索，如 2026-05-30、昨晚、某个话题关键词；按本地原文/metadata 简单匹配。",
    )
    goal: str | None = Field(
        default=None,
        description="总结目标或关注点。不填则按时间线概括关键事实、决定、未完成事项和人物偏好。",
    )
    max_tokens: int = Field(
        default=4096,
        ge=512,
        le=16384,
        description="兼容旧参数；现在总结由后台子 Agent 写文件，通常不需要填写。",
    )


class RecallHistoryArgs(_ToolArgs):
    """recall_history 工具参数。"""

    conversation_id: str | None = Field(
        default=None,
        description=(
            "要检索的会话标签，如 private:430666862 或 group:1087440069。"
            "不填则在全局归档中检索。"
        ),
    )
    keyword: str | None = Field(
        default=None,
        description="关键词；用于从永久归档中按原文检索。可不填。",
    )
    time_range: str | None = Field(
        default=None,
        description="时间范围关键词，如 2026-05-30 / 凌晨 / 昨晚。Phase A 按原文和 metadata 做简单文本匹配。",
    )
    limit: int = Field(default=20, ge=1, le=100, description="最多返回多少条记录")


class AgentTaskSource(_ToolArgs):
    """start_agent_task 的资料来源。"""

    type: Literal[
        "workspace_path",
        "tool_call_id",
        "tool_result_file",
        "forward_id",
        "conversation_history",
        "message_id",
        "image_ref",
        "inline_text",
        "inline_json",
        "workspace_glob",
        "directory",
    ] = Field(
        ...,
        description=(
            "资料类型。不支持 URL；网页类资料需先由其它工具保存到 workspace 后再传 workspace_path。"
        ),
    )
    value: str | None = Field(
        default=None,
        description="通用值：路径、tool_call_id、forward_id、message_id、图片引用或短文本。",
    )
    conversation_id: str | None = Field(
        default=None,
        description="conversation_history/message_id 可用：private:QQ 或 group:群号。",
    )
    keyword: str | None = Field(
        default=None,
        description="conversation_history 可用：按关键词检索历史。",
    )
    time_range: str | None = Field(
        default=None,
        description="conversation_history 可用：时间范围线索，如 2026-05-30。",
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=500,
        description="conversation_history 可用：最多收集多少条记录。",
    )
    data: Any = Field(
        default=None,
        description="inline_json 可用：直接传入的小型 JSON 资料。",
    )


class StartAgentTaskArgs(_ToolArgs):
    """start_agent_task 工具参数。"""

    prompt: str = Field(
        ...,
        min_length=1,
        description=(
            "必须填写：交给后台子 Agent 的完整任务说明。写清要处理哪些资料、输出什么、"
            "是否保留发送者/时间/证据。"
        ),
    )
    sources: list[AgentTaskSource] = Field(
        default_factory=list,
        description=(
            "资料来源列表。支持 workspace_path/tool_call_id/tool_result_file/forward_id/"
            "conversation_history/message_id/image_ref/inline_text/inline_json/"
            "workspace_glob/directory；不支持 URL。"
        ),
    )
    output_format: Literal["markdown", "json", "text"] = Field(
        default="markdown",
        description="期望输出格式。",
    )
    output_name: str | None = Field(
        default=None,
        description="期望结果文件名，可不填；系统会保存到 workspace/agent_tasks/ 下。",
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
        ...,
        ge=1,
        description=(
            "从现在开始等待多少秒后唤醒。必须把“2 分钟后”换算成 120，"
            "把“1 小时后”换算成 3600；不要传绝对时间字符串。"
        ),
    )
    mode: Literal["send_message", "wakeup"] = Field(
        default="wakeup",
        description=(
            "定时任务模式：send_message=到点直接向目标发送 message_text，不再调用模型；"
            "wakeup=到点唤醒 Agent，由 Agent 按 reminder 执行较复杂任务。"
        ),
    )
    reminder: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "mode=wakeup 时必填：到点后系统给 Agent 看的自包含任务上下文。"
            "由于唤醒时不会重新附带完整旧历史，必须写清原始请求、到点后具体做什么、要调用的工具。"
            "用于较复杂的继续任务，例如到点后查询状态、整理结果、再决定是否发消息。"
        ),
    )
    message_text: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "mode=send_message 时必填：到点后直接发送给目标的固定消息正文。"
            "用于普通提醒/叫人/定时发送，不需要再唤醒 Agent 理解任务。"
        ),
    )
    target_type: Literal["private", "group"] | None = Field(
        default=None,
        description="消息目标类型。mode=send_message 时应填写；私聊填 private，群聊填 group。未填时系统会尝试使用当前会话作为默认目标。",
    )
    target_id: int | None = Field(
        default=None,
        description="QQ 号或群号；与 target_type 配套使用。mode=send_message 时应填写。",
    )

    @model_validator(mode="after")
    def validate_schedule_mode(self) -> ScheduleWakeupArgs:
        if self.mode == "send_message":
            if not (self.message_text or "").strip():
                raise ValueError("mode=send_message 时 message_text 必填")
        elif not (self.reminder or "").strip():
            raise ValueError("mode=wakeup 时 reminder 必填")
        return self


# ============================================================
# Feature（可选功能：视觉/搜索/天气）
# ============================================================


class DescribeImageArgs(_ToolArgs):
    """describe_image 工具参数。"""

    image_url: str = Field(
        ...,
        min_length=1,
        description=(
            "图片 URL 或 workspace 中的图片路径。从用户消息的 [图片 url=...]、"
            "[图片 workspace=...] 标记中获取；收到图片时先用此工具理解内容。"
        ),
    )
    prompt: str | None = Field(
        default=None,
        description="兼容旧参数。建议改用 question；不填则只获取图片摘要。",
    )
    question: str | None = Field(
        default=None,
        description="针对图片的具体问题，如“提取图中所有文字”或“分析这个表情含义”。不填时返回简短概览。",
    )


class WebSearchArgs(_ToolArgs):
    """web_search 工具参数。"""

    query: str = Field(
        ...,
        min_length=1,
        description="搜索关键词。需要当前/最新信息、事实核查、网页资料时使用；关键词应具体，不要只写“搜索一下”。",
    )


class GetWeatherArgs(_ToolArgs):
    """get_weather 工具参数。"""

    city: str = Field(
        ..., min_length=1, description="城市名称或区县名，如 北京、上海浦东、广州天河。用户没说城市时先根据上下文判断，仍不明确再询问。"
    )
    days: int = Field(
        default=1, ge=1, le=7, description="预报天数 1-7，默认 1（和风天气免费版上限）"
    )


class SendVoiceMessageArgs(_ToolArgs):
    """send_voice_message 工具参数。"""

    target_type: Literal["private", "group"] = Field(
        ..., description="发到私聊还是群"
    )
    target_id: int = Field(..., description="对方 QQ 号 / 群号")
    text: str = Field(
        ..., min_length=1, description="要合成成语音的文本（保持口语化）"
    )
    prompt: str = Field(
        ...,
        min_length=4,
        description=(
            "必填：一句话描述语气/音色/节奏，如「年轻女性，轻松调侃，语速中等，尾音自然」。"
            "语气提示会显著影响 TTS 质量，不要省略。"
        ),
    )


# ============================================================
# Workspace（沙箱文件操作 + Python 执行）
# ============================================================


class ReadFileArgs(_ToolArgs):
    """read_file 工具参数。"""

    path: str = Field(
        ...,
        min_length=1,
        description=(
            "相对 workspace 的文件路径，如 'note.md'、'sub/data.json'、'incoming/问卷.pdf'。"
            "用户消息里带 workspace= 路径时，优先用这个工具读取。"
        ),
    )
    max_bytes: int = Field(
        default=12000,
        ge=1,
        le=500000,
        description="本页最多读取字节数（防超大文件返回）。默认 12KB",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="从第几行开始读取，默认 0。返回 next_offset 时可用它续读后续内容。",
    )
    max_lines: int = Field(
        default=200,
        ge=1,
        le=2000,
        description="本页最多读取多少行，默认 200。",
    )


class WriteFileArgs(_ToolArgs):
    """write_file 工具参数。覆盖式写入。"""

    path: str = Field(
        ..., min_length=1, description="相对 workspace 的文件路径"
    )
    content: str = Field(
        ..., description="写入的全部内容（覆盖原文件）"
    )


class EditFileArgs(_ToolArgs):
    """edit_file 工具参数。字符串替换。"""

    path: str = Field(
        ..., min_length=1, description="相对 workspace 的文件路径"
    )
    old: str = Field(
        ..., min_length=1, description="要替换的旧字符串（必须在文件中唯一出现，否则失败）"
    )
    new: str = Field(..., description="替换为这个字符串")


class ListFilesArgs(_ToolArgs):
    """list_files 工具参数。"""

    path: str = Field(
        default=".",
        description="相对 workspace 的目录路径。默认 '.' 即 workspace 根",
    )
    pattern: str = Field(
        default="*",
        description="glob 模式，如 '*.txt' 或 '**/*.py'。默认 '*' 列所有",
    )


class DeleteFileArgs(_ToolArgs):
    """delete_file 工具参数。"""

    path: str = Field(
        ..., min_length=1, description="相对 workspace 的文件路径（**不能**是目录）"
    )


class RunPythonArgs(_ToolArgs):
    """run_python 工具参数。"""

    code: str = Field(
        ..., min_length=1, description="要执行的 Python 代码。在 workspace 目录里跑"
    )
    timeout_seconds: int = Field(
        default=30, ge=1, le=120, description="超时秒数（1-120）"
    )
