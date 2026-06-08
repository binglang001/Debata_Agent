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

SendInterruptPolicy = Literal["interrupt_all", "interrupt_priority", "atomic"]
SendReviewPolicy = Literal["review_all", "review_priority"]


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
        description="消息正文；可在开头加 [CQ:reply,id=msg_id] 引用回复。content、emoji、image 三选一。",
    )
    emoji: str | None = Field(
        default=None,
        description="表情包名称，不带文件后缀；必须从 task_context 的可用表情包列表中选择。content、emoji、image 三选一。",
    )
    image: str | None = Field(
        default=None,
        description="要发送的图片 URL 或 workspace 相对路径；不是表情包。content、emoji、image 三选一。",
    )
    order: int = Field(..., description="发送顺序，从小到大")
    delay: float | None = Field(
        default=None,
        description="本条发出后的等待秒数。不填时按消息长度自动估算。",
    )

    @model_validator(mode="after")
    def validate_payload_choice(self) -> PrivateMessageTarget:
        values = [
            value
            for value in (self.content, self.emoji, self.image)
            if str(value or "").strip()
        ]
        if len(values) != 1:
            raise ValueError("content、emoji、image 必须且只能填写一个")
        return self


class SendPrivateArgs(_ToolArgs):
    """send_private_messages 工具参数。"""

    targets: list[PrivateMessageTarget] = Field(
        ..., description="要发送的目标列表，至少 1 项"
    )
    reviewed_until_seq: int | None = Field(
        default=None,
        ge=0,
        description=(
            "发送前复核锚点：表示这次待发送内容基于已看到的最新入站 seq。"
            "不填时使用当前模型轮开始时的 seen_seq；收到 needs_review 后再次发送或 commit 应填返回的 latest_seq。"
        ),
    )
    review_policy: SendReviewPolicy = Field(
        default="review_priority",
        description=(
            "发送前复核策略。review_priority=只因未见高优先级消息暂停；"
            "review_all=目标会话有任何未见新消息都暂停复核。"
        ),
    )
    delivery_interrupt_policy: SendInterruptPolicy = Field(
        default="interrupt_all",
        description=(
            "发送被系统接收后的中断策略。interrupt_all=同会话新消息都会阻断发送或使旧回复 stale；"
            "interrupt_priority=只被确定性高优先级事件阻断，如私聊新消息、同触发用户追问、撤回；"
            "atomic=普通新消息不阻断，只适合固定通知/命令结果，不要用来逃避频繁打断。"
        ),
    )
    ignore_review_interrupts: bool = Field(
        default=False,
        description=(
            "发送工具调用被系统接受后，忽略后续普通新消息造成的软打断，"
            "不把这次发送改成 interrupted/attempt。不能绕过发送前 needs_review，"
            "也不能绕过撤回、权限变化、禁言、退群、发送失败等硬错误。"
            "默认 false。"
        ),
    )
    responding_to_message_ids: list[str] = Field(
        default_factory=list,
        description="可选：这次回复明确针对哪些消息 ID。程序用它确定 focus_user_ids；不确定不要编造。",
    )
    reply_to_message_id: str | None = Field(
        default=None,
        description="可选：这次发送要引用回复的消息 ID；程序会在第一条文本消息前补 CQ 引用。",
    )
    reason: str | None = Field(
        default=None,
        description="简短说明为什么这样发送以及为什么选择该复核/中断策略。用于日志和自检，不会发给 QQ。",
    )


class GroupMessageTarget(_ToolArgs):
    """群聊消息中的单条目标项。"""

    content: str | None = Field(
        default=None,
        description="消息正文；@人用 [CQ:at,qq=QQ号]；引用用 [CQ:reply,id=msg_id]。",
    )
    emoji: str | None = Field(
        default=None,
        description="表情包名称，不带文件后缀；必须从 task_context 的可用表情包列表中选择。content、emoji、image 三选一。",
    )
    image: str | None = Field(
        default=None,
        description="要发送的图片 URL 或 workspace 相对路径；不是表情包。content、emoji、image 三选一。",
    )
    order: int = Field(..., description="发送顺序，从小到大")
    delay: float | None = Field(
        default=None, description="本条发出后的等待秒数。不填时按长度估算。"
    )

    @model_validator(mode="after")
    def validate_payload_choice(self) -> GroupMessageTarget:
        values = [
            value
            for value in (self.content, self.emoji, self.image)
            if str(value or "").strip()
        ]
        if len(values) != 1:
            raise ValueError("content、emoji、image 必须且只能填写一个")
        return self


class SendGroupArgs(_ToolArgs):
    """send_group_message 工具参数。"""

    group_id: int = Field(..., description="群号")
    targets: list[GroupMessageTarget] = Field(
        ..., description="要发送的消息列表，至少 1 项"
    )
    reviewed_until_seq: int | None = Field(
        default=None,
        ge=0,
        description=(
            "发送前复核锚点：表示这次待发送内容基于已看到的最新入站 seq。"
            "不填时使用当前模型轮开始时的 seen_seq；收到 needs_review 后再次发送或 commit 应填返回的 latest_seq。"
        ),
    )
    review_policy: SendReviewPolicy = Field(
        default="review_priority",
        description=(
            "发送前复核策略。review_priority=只因未见高优先级消息暂停；"
            "review_all=目标群有任何未见新消息都暂停复核。"
        ),
    )
    delivery_interrupt_policy: SendInterruptPolicy = Field(
        default="interrupt_priority",
        description=(
            "发送被系统接收后的中断策略。interrupt_priority=推荐，普通群聊插话不阻断，"
            "但同触发用户追问、@机器人、撤回等确定性高优先级事件会阻断；"
            "interrupt_all=长回复/多段解释时使用，任何同会话新消息都阻断；"
            "atomic=普通新消息不阻断，只适合固定通知/命令结果，不要用来逃避频繁打断。"
        ),
    )
    ignore_review_interrupts: bool = Field(
        default=False,
        description=(
            "发送工具调用被系统接受后，忽略后续普通新消息造成的软打断，"
            "不把这次发送改成 interrupted/attempt。不能绕过发送前 needs_review，"
            "也不能绕过撤回、权限变化、禁言、退群、发送失败等硬错误。"
            "默认 false。"
        ),
    )
    responding_to_message_ids: list[str] = Field(
        default_factory=list,
        description="可选：这次回复明确针对哪些消息 ID。程序用它确定 focus_user_ids；不确定不要编造。",
    )
    reply_to_message_id: str | None = Field(
        default=None,
        description="可选：这次发送要引用回复的消息 ID；程序会在第一条文本消息前补 CQ 引用，适合旧回复被新消息隔开后锚定上下文。",
    )
    reason: str | None = Field(
        default=None,
        description="简短说明为什么这样发送以及为什么选择该复核/中断策略。用于日志和自检，不会发给 QQ。",
    )


class CommitSendAttemptArgs(_ToolArgs):
    """commit_send_attempt 工具参数。"""

    send_attempt_id: str = Field(
        ...,
        min_length=1,
        description="要确认提交的 send_attempt_id，来自发送工具返回的 needs_review。只能提交一次。",
    )
    reviewed_until_seq: int | None = Field(
        default=None,
        ge=0,
        description="复核后已看到的最新入站 seq。通常填 needs_review 返回的 latest_seq。",
    )
    delivery_interrupt_policy: SendInterruptPolicy = Field(
        default="interrupt_priority",
        description=(
            "确认发送被系统接收后的中断策略。短低风险回复用 interrupt_priority；"
            "长回复/多段解释用 interrupt_all；atomic 只用于固定通知/命令结果。"
        ),
    )
    reply_to_message_id: str | None = Field(
        default=None,
        description="可选：确认发送时给第一条文本消息补引用，避免被中间新消息隔开后串话。",
    )
    ignore_review_interrupts: bool = Field(
        default=False,
        description=(
            "仅在确认旧 attempt 前忽略软复核打断并继续提交；"
            "不能绕过撤回、权限变化、禁言、退群、发送失败等硬错误。默认 false。"
        ),
    )
    reason: str | None = Field(
        default=None,
        description="简短说明为什么复核后仍提交旧内容；用于日志和自检，不会发给 QQ。",
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


class GetMsgArgs(_ToolArgs):
    """get_msg 工具参数。"""

    message_id: int = Field(
        ...,
        description="要读取的 QQ 消息 ID。只能使用上下文中真实出现过的 msg_id，不要猜。",
    )


class SendPokeArgs(_ToolArgs):
    """send_poke 工具参数。"""

    user_id: int = Field(..., description="要戳一戳的目标 QQ 号，必须明确。")
    group_id: int | None = Field(
        default=None,
        description=(
            "群号。群聊戳一戳时填写；私聊戳一戳不填。"
            "不填且当前会话是群聊时，程序会自动使用当前群号。"
        ),
    )
    reason: str | None = Field(
        default=None,
        description="简短说明为什么戳一戳。用于日志和自检，不会发给 QQ。",
    )


class SetMsgEmojiLikeArgs(_ToolArgs):
    """set_msg_emoji_like 工具参数。"""

    message_id: int = Field(
        ...,
        description="要添加或取消表情回复的消息 ID。必须是真实消息 ID，不要猜。",
    )
    emoji_id: str = Field(
        ...,
        min_length=1,
        description=(
            "QQ 表情 ID。必须使用已知 ID；不确定时不要猜。"
            "常见 ID 可来自上下文、文档或用户明确给出的表情。"
        ),
    )
    set: bool = Field(
        default=True,
        description="true=添加表情回复，false=取消这个表情回复。",
    )
    reason: str | None = Field(
        default=None,
        description="简短说明为什么设置表情回复。用于日志和自检，不会发给 QQ。",
    )


class ToolSearchArgs(_ToolArgs):
    """tool_search 工具参数。"""

    tool_name: str = Field(
        ...,
        min_length=1,
        description="要查询说明和真实参数的工具名。",
    )
    detail: Literal["summary", "full"] = Field(
        default="summary",
        description="返回参数摘要还是完整 JSON schema。summary 默认足够直接调用；复杂嵌套时可用 full。",
    )
    intent: str | None = Field(
        default=None,
        description="可选：你为什么需要这个工具。用于返回更贴合的风险提醒，不会发给 QQ。",
    )


# ============================================================
# QQ 群管理 / 权限查询
# ============================================================


class GetGroupSelfRoleArgs(_ToolArgs):
    """get_group_self_role 工具参数。"""

    group_id: int | None = Field(
        default=None,
        description="群号。不填时使用当前群聊；私聊或无法推断当前群时必须填写。",
    )


class SetGroupKickArgs(_ToolArgs):
    """set_group_kick 工具参数。"""

    group_id: int = Field(..., description="群号")
    user_id: int = Field(..., description="要踢出的 QQ 号，必须明确，不要猜")
    reject_add_request: bool = Field(
        default=False,
        description="是否拒绝该用户再次加群。默认 false；只有用户明确要求拉黑/拒绝再加时才设 true。",
    )
    reason: str = Field(
        ...,
        min_length=2,
        description="为什么执行踢人。用于日志和自检，不会发给 QQ；必须来自明确用户请求。",
    )


class SetGroupBanArgs(_ToolArgs):
    """set_group_ban 工具参数。"""

    group_id: int = Field(..., description="群号")
    user_id: int = Field(..., description="要禁言的 QQ 号，必须明确，不要猜")
    duration_seconds: int = Field(
        ...,
        ge=1,
        le=2_592_000,
        description="禁言时长，单位秒。必须明确；例如 600=10分钟，1800=30分钟。",
    )
    reason: str = Field(
        ...,
        min_length=2,
        description="为什么执行禁言。用于日志和自检，不会发给 QQ；必须来自明确用户请求。",
    )


class SetGroupWholeBanArgs(_ToolArgs):
    """set_group_whole_ban 工具参数。"""

    group_id: int = Field(..., description="群号")
    enable: bool = Field(..., description="true=开启全员禁言，false=关闭全员禁言")
    reason: str = Field(
        ...,
        min_length=2,
        description="为什么执行全员禁言。用于日志和自检，不会发给 QQ；必须来自明确用户请求。",
    )


class SetGroupLeaveArgs(_ToolArgs):
    """set_group_leave 工具参数。"""

    group_id: int | None = Field(
        default=None,
        description="要退出的群号。不填时使用当前群聊；必须是当前群，不能让机器人跨群退群。",
    )
    reason: str = Field(
        ...,
        min_length=2,
        description="为什么退群。用于日志和自检，不会发给 QQ；必须来自明确用户请求。",
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

    memory_id: str | None = Field(
        default=None,
        description=(
            "要删除的重要记忆 ID。推荐使用 memory_id；必须来自重要记忆上下文中展示的 ID、"
            "save_important_memory 返回的 memory_id，或 existing_id。"
        ),
    )
    keyword: str | None = Field(
        default=None,
        description=(
            "旧版兼容参数：按关键词模糊删除。新调用不要使用；能看到记忆 ID 时必须填 memory_id，"
            "避免关键词误删多条记忆。"
        ),
    )

    @model_validator(mode="after")
    def validate_delete_target(self) -> DeleteMemoryArgs:
        memory_id = (self.memory_id or "").strip()
        keyword = (self.keyword or "").strip()
        self.memory_id = memory_id or None
        self.keyword = keyword or None
        if self.memory_id is None and self.keyword is None:
            raise ValueError("memory_id 或 keyword 至少填写一个；推荐使用 memory_id")
        return self


class UpdateMemoryArgs(_ToolArgs):
    """update_important_memory 工具参数。"""

    memory_id: str = Field(
        ...,
        min_length=1,
        description="要更新的重要记忆 ID。通常来自 long_term_memory 中的记忆标识或工具返回的 existing_id。",
    )
    memory_text: str = Field(
        ...,
        min_length=1,
        description=(
            "覆写后的完整记忆正文。必须客观、完整、有明确主语；不要只写补丁片段。"
            "例如写“张三的生日是7月8日”，不要写“你生日七月八号”。"
        ),
    )
    scope: str | None = Field(
        default=None,
        description="可选：同时更新记忆适用范围。可填 global、user:QQ号 或 group:群号；不填则保留原 scope。",
    )
    pinned: bool | None = Field(
        default=None,
        description="可选：同时更新是否置顶。不填则保留原 pinned。",
    )
    reason: str | None = Field(
        default=None,
        description="简短说明为什么更新旧记忆，而不是新增一条。只用于日志和自检。",
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
    offset: int = Field(
        default=0,
        ge=0,
        description="从第几条联系人开始返回，默认 0。返回 next_offset 时可用它续读。",
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=200,
        description="本页最多返回多少条，默认 100，最多 200。",
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
    recursive: bool = Field(
        default=True,
        description="是否递归尝试展开内层合并转发。默认开启。",
    )
    max_depth: int = Field(
        default=3,
        ge=0,
        le=5,
        description="递归展开内层合并转发的最大深度，0 表示只读取外层。",
    )
    output: Literal["auto", "json", "markdown"] = Field(
        default="auto",
        description="完整结果写入文件的格式。auto/json 写嵌套 JSON；markdown 写可读 Markdown。",
    )


class GetRecentChatMessagesArgs(_ToolArgs):
    """get_recent_chat_messages 工具参数。"""

    conversation_id: str | None = Field(
        default=None,
        description=(
            "要读取的当前运行期真实 QQ 会话，如 private:430666862 或 group:1039163467。"
            "不填则使用当前会话。"
        ),
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=1000,
        description="最多读取多少条最近消息，1 到 1000。返回的是连续最近窗口，不做头尾拼接。",
    )
    since_msg_id: str | None = Field(
        default=None,
        description="只读取此 msg_id 之后的消息；用于 stale/回执后确认当前真实聊天状态。",
    )
    before_msg_id: str | None = Field(
        default=None,
        description="只读取此 msg_id 之前的消息；用于向前翻连续历史窗口。",
    )
    include_raw: bool = Field(
        default=False,
        description="是否附带原始 CQ 消息。通常不要开启，只有排查图片/合并转发/引用等原始结构时使用。",
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

    archive_ids: list[str] = Field(
        default_factory=list,
        description="归档短 ID 列表；提供后优先按 ID 展开上下文。",
    )
    context_before: int = Field(
        default=0,
        ge=0,
        le=20,
        description="按 archive_ids 展开时，每个 ID 前取同会话多少条相邻记录。",
    )
    context_after: int = Field(
        default=0,
        ge=0,
        le=20,
        description="按 archive_ids 展开时，每个 ID 后取同会话多少条相邻记录。",
    )
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


class ArchiveTimeRange(_ToolArgs):
    """归档筛选时间段。"""

    start: str | None = Field(
        default=None,
        description="起始时间，如 2026-06-07 01:00；可只填 start。",
    )
    end: str | None = Field(
        default=None,
        description="结束时间，如 2026-06-07 02:00；可只填 end。",
    )


class FilterArchiveRecordsArgs(_ToolArgs):
    """filter_archive_records 工具参数。"""

    archive_ids: list[str] = Field(
        default_factory=list,
        description="可选：直接筛选这些归档短 ID。",
    )
    conversation_ids: list[str] = Field(
        default_factory=list,
        description="可选：会话范围，如 group:497686077 或 private:430666862。",
    )
    conversation_match: Literal["exact", "contains", "fuzzy"] = Field(
        default="exact",
        description="会话匹配方式。",
    )
    sender_ids: list[str] = Field(
        default_factory=list,
        description="可选：发送者 QQ/内部 ID 范围。",
    )
    sender_names: list[str] = Field(
        default_factory=list,
        description="可选：发送者昵称范围。",
    )
    sender_match: Literal["exact", "contains", "fuzzy"] = Field(
        default="exact",
        description="发送者昵称匹配方式。",
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="可选：正文关键词列表。",
    )
    keyword_match: Literal["exact", "contains", "fuzzy"] = Field(
        default="contains",
        description="关键词匹配方式。",
    )
    keyword_operator: Literal["all", "any"] = Field(
        default="all",
        description="all=所有关键词都命中；any=任一关键词命中。",
    )
    time_ranges: list[ArchiveTimeRange] = Field(
        default_factory=list,
        description="可选：多个时间段，同字段内 OR。",
    )
    message_kinds: list[str] = Field(
        default_factory=list,
        description="可选：消息类型，如 text、image、file、audio、mixed。",
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=200,
        description="最多返回多少条候选记录。",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="分页偏移。",
    )
    order: Literal["asc", "desc"] = Field(
        default="desc",
        description="按时间/写入顺序升序或降序。",
    )


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
            "workspace_glob/directory；不支持 URL。image_ref 仅用于已有可用图像理解能力时的资料整理，"
            "不能用来绕过 describe_image 失败。"
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
    max_loops: int | None = Field(
        default=None,
        ge=5,
        le=60,
        description=(
            "后台子 Agent 最多可调用工具多少轮。复杂资料整理可调高；"
            "必须在 5 到 60 之间。不填则使用主 Agent 当前默认值。"
        ),
    )
    timeout_seconds: int | None = Field(
        default=None,
        ge=60,
        le=3600,
        description=(
            "后台任务总超时秒数。一般不用填写；系统会根据工具轮数自动给足时间。"
            "超时后若结果文件已写出会返回已有结果，否则返回失败说明。"
        ),
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
    ignore_review_interrupts: bool = Field(
        default=False,
        description=(
            "语音发送被系统接受后，忽略后续普通新消息造成的软打断。"
            "不能绕过发送前 needs_review，也不能绕过撤回、权限变化、禁言、退群、发送失败等硬错误。"
            "默认 false。"
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
    offset: int = Field(
        default=0,
        ge=0,
        description="从第几条匹配结果开始返回，默认 0。返回 next_offset 时可用它续读。",
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=200,
        description="本页最多返回多少条，默认 100，最多 200。",
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
