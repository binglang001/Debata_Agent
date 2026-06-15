"""配置文件的 Pydantic schema。

设计原则：
    - dict 而非 list 存储有 ID 的对象（adapters/providers），便于查询和合并
    - 所有 API 密钥通过 `*_key_id` 字段引用 SecretsManager 中的密钥 ID
    - 每个 Agent 独立指定 provider + 模型 + 参数
    - 可选功能（features）通过 `enabled` 字段开关
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ============================================================
# 基础设施
# ============================================================


class StrictModel(BaseModel):
    """所有配置模型基类。

    use_attribute_docstrings=True 让 Pydantic 把"字段后紧跟的 docstring"
    作为 `Field.description`，避免在每个字段重复写 `Field(description=...)`。
    GUI 编辑器 / JSON schema 生成器可以直接读到说明。
    """
    """禁止未知字段，避免拼写错误被静默吞掉。"""

    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)


# ============================================================
# 应用元信息
# ============================================================


class AppMeta(StrictModel):
    name: str = "Debata_Agent"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    theme: Literal["auto", "light", "dark"] = "auto"


# ============================================================
# 适配器
# ============================================================


class WhitelistConfig(StrictModel):
    """入站消息过滤模式。由 message_pipeline 入口检查 sender QQ / 群 ID。"""

    mode: Literal["open", "verify", "whitelist"] = "verify"
    """open = 对所有人开放（不安全：陌生人发消息也会触发 AI 回复，可能产生意外 API 费用）;
    verify = 管理员审核（陌生人加好友 / 加群时由 admin 审核，已加好友的不再过滤）;
    whitelist = 仅响应 qq_ids / group_ids 列表中的对象。"""

    qq_ids: list[int] = Field(default_factory=list)
    """仅在 mode=whitelist 下生效：允许的好友 QQ 号列表。"""

    group_ids: list[int] = Field(default_factory=list)
    """仅在 mode=whitelist 下生效：允许的群号列表。"""


class NapCatAdapterConfig(StrictModel):
    type: Literal["napcat"] = "napcat"
    enabled: bool = True

    mode: Literal["client", "server"] = "client"
    """连接模式（从 Debata_Agent 视角命名，符合直觉）：

        - "client"：程序作为 WebSocket **客户端**，主动连 NapCat 的 WS 端口。
          对应 NapCat 那边配置「正向 WS」（NapCat 监听，等程序连入）。
        - "server"：程序作为 WebSocket **服务端**，监听端口等 NapCat 连入。
          对应 NapCat 那边配置「反向 WS」（NapCat 主动连出到程序）。

    两种模式用同一组 host/port/path 字段，含义对称：
        - client：程序去连 ws://{host}:{port}{path}
        - server：程序在 {host}:{port}{path} 监听
    """

    # WS 连接端点（两种模式共用同一组字段）
    host: str = "127.0.0.1"
    port: int = 3001
    path: str = "/"
    """WebSocket 路径。client 模式（NapCat 正向 WS）默认 "/"；
    server 模式时与 NapCat 那边配的目标地址保持一致即可。"""

    @field_validator("path", mode="before")
    @classmethod
    def normalize_ws_path(cls, v) -> str:
        """兼容旧配置/UI 把 path 写成 null 或空串，并保证以 / 开头。"""
        if v is None:
            return "/"
        raw = str(v).strip()
        if not raw:
            return "/"
        if not raw.startswith("/"):
            return "/" + raw
        return raw

    # 鉴权：填 SecretsManager 中的【密钥 ID】，不是 token 本身。
    # 例如 "napcat_default_token" 这种短标识，对应 secrets 中保存的实际 token。
    access_token_id: str | None = None
    """SecretsManager 中的密钥 ID（如 "napcat_default_token"）。
    None 表示不使用 token。⚠️ 这是引用 ID，不是 token 字符串本身。"""

    @field_validator("access_token_id")
    @classmethod
    def validate_token_id_format(cls, v: str | None) -> str | None:
        """挡住把 token 值直接填到 access_token_id 字段的常见错误。

        合法 ID 应该是短标识符（snake_case / 字母数字下划线）；
        如果看起来像 token（长 + 含混合大小写或非字母数字），抛错。
        """
        if v is None or v == "":
            return v
        # token 值通常 16+ 字符、混合大小写、可能含 `/`、`+`、`=`
        looks_like_token = (
            len(v) >= 12
            and any(c.isupper() for c in v)
            and any(c.islower() for c in v)
            and not all(c.isalnum() or c == "_" for c in v)
        ) or (len(v) >= 20 and any(c.isupper() for c in v) and any(c.islower() for c in v))
        if looks_like_token:
            raise ValueError(
                f"access_token_id={v!r} 看起来像 token 本身，而不是引用 ID。\n"
                f"这个字段应该填 secrets 中的密钥 ID（如 'napcat_default_token'），"
                f"而不是 token 字符串。\n"
                f"如果你想保存新 token，请用 `python main.py --napcat` 重新配置。"
            )
        return v

    # 进程托管
    manage_process: bool = False
    process_path: str = ""
    process_args: list[str] = Field(default_factory=list)
    auto_restart: bool = True

    # 重连参数
    reconnect_interval_seconds: float = 3.0
    max_reconnect_attempts: int = -1  # -1 表示无限重连
    reconnect_backoff_max_seconds: float = 60.0
    """指数退避封顶。reconnect_interval_seconds * 2^n 超过此值即按此值重连。"""

    # 心跳与超时（之前硬编码在 connection.py / adapter.py 里，现在统一配置）
    ping_interval_seconds: float = 20.0
    """WebSocket ping 间隔（秒）。NapCat 没有应用层心跳，靠 WS ping 维持。"""

    ping_timeout_seconds: float = 20.0
    """WS ping 未收到 pong 多久判定为断连。"""

    initial_connect_timeout_seconds: float = 10.0
    """旧配置兼容：初次连接等待超时。新逻辑优先使用 startup_connect_timeout_seconds。"""

    startup_connect_timeout_seconds: float = 1.5
    """Runtime 启动时最多等待首次连接的时间；超过后后台继续重连。"""

    api_timeout_seconds: float = 30.0
    """调用 OneBot API（send_msg / get_friend_list 等）的超时。"""

    api_wait_connected_timeout_seconds: float = 3.0
    """调用 OneBot API 前等待连接建立的最长时间。0 表示不等待。"""

    fast_reconnect_attempts: int = 5
    """启动早期或断线后的快速重试次数。"""

    fast_reconnect_interval_seconds: float = 0.3
    """快速重试间隔。"""

    reconnect_jitter_seconds: float = 0.2
    """慢速重连等待的随机抖动上限。"""

    process_warmup_seconds: float = 2.0
    """manage_process=True 时拉起 NapCat 后等待启动的时间。"""

    voice_fetch_delay_seconds: float = 1.0
    """fetch_voice_text 前等 NapCat 处理语音的延迟。"""

    # 白名单
    whitelist: WhitelistConfig = Field(default_factory=WhitelistConfig)


# 未来扩展：Union[NapCatAdapterConfig, DiscordAdapterConfig, ...]
AdapterConfig = Annotated[NapCatAdapterConfig, Field(discriminator="type")]


# ============================================================
# LLM 提供商
# ============================================================


ProtocolType = Literal["openai_compat", "anthropic"]
"""支持的协议类型。其它厂商（gemini / volcengine / qwen / glm 等）都走 openai_compat。
要新增协议须先在 providers/registry.py 的 PROTOCOL_REGISTRY 注册具体实现。"""


class ProviderConfig(StrictModel):
    """LLM 提供商配置。

    两种使用方式：
        1. 引用内置预设：只填 preset 和 api_key_id
        2. 完全自定义：填 protocol、base_url、api_key_id（preset 留空）
    """

    preset: str | None = None
    """内置预设名（providers/presets/{preset}/preset.yaml）。"""

    display_name: str | None = None
    """显示名。preset 模式下若留空则取 preset.yaml 的 display_name；自定义模式建议填。"""

    protocol: ProtocolType | None = None
    """协议类型。未指定 preset 时必填。仅支持 openai_compat / anthropic。"""

    base_url: str | None = None
    """API base URL。未指定 preset 时必填。"""

    api_key_id: str | None = None
    """SecretsManager 中的密钥 ID。"""

    extra_headers: dict[str, str] = Field(default_factory=dict)
    """额外的 HTTP 请求头（如某些代理需要）。"""

    timeout_seconds: float = 120.0
    """整体请求超时（秒）。流式响应的首 token 超时另由 AgentConfig.first_token_timeout_seconds 控制。"""

    @model_validator(mode="after")
    def validate_provider(self) -> ProviderConfig:
        if not self.preset and (not self.protocol or not self.base_url):
            raise ValueError(
                "未指定 preset 时，必须同时填写 protocol 和 base_url"
            )
        # preset + protocol/base_url 可共存（preset 提供默认，字段值覆盖；常见于代理 URL 场景）
        return self


# ============================================================
# Agent 配置
# ============================================================


class ReasoningConfig(StrictModel):
    enabled: bool = False
    budget: Literal["low", "medium", "high"] | None = None
    max_tokens: int | None = None


class AgentConfig(StrictModel):
    """单个 Agent 的模型参数配置。"""

    provider: str
    """provider ID（必须在顶层 providers 字典中存在）"""

    model: str
    """模型 ID，如 'deepseek-v4-flash'、'claude-sonnet-4-5'、'glm-4.7-flash'"""

    temperature: float = Field(default=0.6, ge=0.0, le=2.0)
    """采样温度。聊天主体推荐 0.6；总结/路由用 0.1~0.3。"""

    top_p: float = Field(default=1.0, ge=0.0, le=1.0)
    """nucleus sampling 阈值。通常保持 1.0。"""

    max_tokens: int = Field(default=16384, ge=1)
    """单次响应最长 token 数。chat agent 推荐 16k；proactive 64 就够。"""

    reasoning: ReasoningConfig | None = None
    """思考/推理配置（DeepSeek R1、Claude thinking 等）。不填则不启用。"""

    first_token_timeout_seconds: float = 30.0
    """首 token 超时（秒）。流式模式下，首字未到即超时重试。"""

    max_loops: int = 25
    """兼容旧配置；不再作为主 runner 的普通硬截断上限。"""

    tool_loop_reminder_interval: int = Field(default=8, ge=1)
    """连续多少个工具轮后触发一次普通提醒。"""

    tool_loop_final_warning_count: int = Field(default=4, ge=1)
    """普通提醒达到多少次后进入最终警告周期。"""

    tool_loop_final_grace_loops: int = Field(default=2, ge=0)
    """最终警告发出后还允许多少个工具轮。"""

    tool_loop_final_max_tokens: int = Field(default=4096, ge=512)
    """工具循环最终收尾/部分结果整理输出上限。"""

    refocus_interval: int = Field(default=5, ge=0)
    """Task Contract 重注入间隔（轮）。0 = 禁用。"""


class AgentsConfig(StrictModel):
    """各类 Agent 的配置。"""

    chat: AgentConfig
    """主聊天 Agent（必填）。"""

    proactive: AgentConfig | None = None
    """主动思考 Agent（不填则禁用主动问候）。"""

    summary: AgentConfig | None = None
    """记忆总结 Agent（不填则使用 chat 配置）。"""

    persona_gen: AgentConfig | None = None
    """人格生成 Agent（不填则使用 chat 配置）。"""


# ============================================================
# 可选功能
# ============================================================


class VisionFeatureConfig(StrictModel):
    """图像理解。type=api 走 provider/api_key_id；type=local 走 model_dir（P3 实装）。

    enabled=True 时 Runtime 会实例化 VisionService 并注册 describe_image 工具。
    """

    enabled: bool = False
    type: Literal["api", "local"] = "api"

    # type=api 字段
    provider: str | None = None
    """type=api 时引用 providers 字典中的 ID（必填）。模型见 model 字段。"""

    model: str = ""
    """多模态模型 ID（如 doubao-seed-1-6-vision / glm-4v / qwen-vl-max）。"""

    max_tokens: int = Field(default=1024, ge=128)
    """图像理解模型单次输出上限。"""

    api_key_id: str | None = None
    """可独立指定密钥 ID（不填则用 providers[provider].api_key_id）。"""

    base_url: str | None = None
    """可独立指定 base_url（不填则用 providers[provider].base_url）。"""

    # type=local 字段（P3 启用）
    model_dir: str = ""

    @model_validator(mode="after")
    def validate_enabled_fields(self) -> VisionFeatureConfig:
        if not self.enabled:
            return self
        if self.type == "api":
            if not self.provider and not self.api_key_id:
                raise ValueError(
                    "features.vision.enabled=True 且 type=api，但 provider 和 api_key_id 都为空。\n"
                    "  请指定 provider（引用 providers 段中的 ID）或填 api_key_id + base_url。"
                )
        else:  # local
            if not self.model_dir:
                raise ValueError(
                    "features.vision.enabled=True 且 type=local，但 model_dir 为空。\n"
                    "  请填本地模型目录路径（如 data/models/qwen-vl/）。"
                )
        return self


class ASRFeatureConfig(StrictModel):
    """语音识别旧配置。

    当前 QQ/NapCat 渠道统一使用 NapCat 内置 fetch_ptt_text；这些字段仅为兼容旧配置保留。
    """

    enabled: bool = False
    type: Literal["api", "local"] = "local"

    # type=api 字段
    provider: str | None = None
    """支持 'baidu'、'xfyun'、'volcengine' 等。"""
    api_key_id: str | None = None
    extra_credentials: dict[str, str] = Field(default_factory=dict)

    # type=local 字段
    local_model: str = "large-v3"
    device: Literal["cuda", "cpu", "auto"] = "auto"
    language: str = "zh"
    model_dir: str = ""

    @model_validator(mode="after")
    def validate_enabled_fields(self) -> ASRFeatureConfig:
        if not self.enabled:
            return self
        if self.type == "api":
            if not self.provider:
                raise ValueError(
                    "features.asr.enabled=True 且 type=api，但 provider 为空。\n"
                    "  请填 provider（如 'xfyun'）+ 对应的 api_key_id / extra_credentials。"
                )
        else:  # local
            if not self.local_model:
                raise ValueError(
                    "features.asr.enabled=True 且 type=local，但 local_model 为空。"
                )
        return self


class TTSFeatureConfig(StrictModel):
    """语音合成配置。本地模式走 VoxCPM2 插件；API 模式推荐 EdgeTTS，也支持讯飞。"""

    enabled: bool = False
    type: Literal["api", "local"] = "api"

    # type=api 字段
    provider: str | None = "edge"
    """API provider。推荐 edge（无需密钥）；讯飞使用 xfyun。百度/火山已从新运行时入口移除。"""
    api_key_id: str | None = None
    extra_credentials: dict[str, str] = Field(default_factory=dict)
    """API 扩展配置。通用 voice 表示说话人；讯飞还需要 app_id/api_secret。"""

    # type=local 字段
    local_model: str = "voxcpm2"
    reference_audio: str = ""
    default_prompt: str = ""
    model_dir: str = ""
    device: Literal["cuda", "cpu", "auto"] = "auto"
    load_denoiser: bool = False
    cfg_value: float = Field(default=2.0, gt=0.0)
    inference_timesteps: int = Field(default=10, ge=1)

    @model_validator(mode="after")
    def validate_enabled_fields(self) -> TTSFeatureConfig:
        if not self.enabled:
            return self
        if self.type == "api":
            provider = self.provider or "edge"
            if provider == "edge":
                self.provider = "edge"
                return self
            if provider == "xfyun" and not self.api_key_id:
                raise ValueError(
                    "features.tts.enabled=True 且 type=api/provider=xfyun，但 api_key_id 为空。"
                )
            if provider not in {"edge", "xfyun"}:
                raise ValueError(
                    "features.tts.enabled=True 且 type=api，但 provider 不受支持。\n"
                    "  可用 provider: edge（推荐，无需密钥）、xfyun。"
                )
        else:  # local
            if not self.local_model:
                raise ValueError(
                    "features.tts.enabled=True 且 type=local，但 local_model 为空。"
                )
        return self


class WeatherFeatureConfig(StrictModel):
    """和风天气 API。enabled=True 时 Runtime 实例化 WeatherService 并注册 get_weather 工具。"""

    enabled: bool = False
    api_key_id: str | None = None
    """SecretsManager 中的 API 密钥 ID。enabled=True 时必填。"""

    host: str = "devapi.qweather.com"
    """和风天气 API 主机。免费版用默认 'devapi.qweather.com'；
    商业版用控制台分配的专属域名（如 'xxx.re.qweatherapi.com'）。"""

    @model_validator(mode="after")
    def validate_enabled_requires_credentials(self) -> WeatherFeatureConfig:
        if self.enabled and not self.api_key_id:
            raise ValueError(
                "features.weather.enabled=True 但未填 api_key_id。\n"
                "  请去 https://console.qweather.com/ 获取 API 密钥，"
                "用 `python main.py --setup` 保存后在此填入对应 ID。"
            )
        return self


class WebSearchFeatureConfig(StrictModel):
    """DuckDuckGo 网页搜索。无需 API 密钥。

    enabled=True 时 Runtime 实例化 WebSearchService 并注册 web_search 工具。
    """

    enabled: bool = True
    provider: Literal["ddg"] = "ddg"
    """搜索后端。当前仅支持 DuckDuckGo。"""

    max_results: int = 5
    """单次搜索返回的最大结果数。"""

    timeout_seconds: float = 10.0
    """单次搜索的网络超时。"""


class LongTermMemoryConfig(StrictModel):
    """长期记忆配置。"""

    mode: Literal["file", "rag"] = "file"
    """file = 不启用 RAG 历史召回，重要记忆仍保存并注入；
    rag = 在重要记忆之外增加会话历史向量检索（需 features.embedding 启用）"""

    rag_top_k: int = 5
    """RAG 模式下每次召回的相关历史条目数（仅 mode=rag 生效）"""

    rag_extractor_interval: int = 15
    """预留：被动结构化抽取触发间隔。当前 RAG 索引历史原文，不替代重要记忆。"""


class EmbeddingFeatureConfig(StrictModel):
    """Embedding 服务配置。API 模式复用已有 provider /embeddings，本地模式走 sentence-transformers。"""

    enabled: bool = False
    type: Literal["api", "local"] = "api"

    provider: str | None = None
    """API 模式：引用 providers 中的 ID 或独立 provider 名（如 volcengine/glm/openai）"""

    api_key_id: str | None = None
    api_model: str = ""

    local_quality: Literal["performance", "quality"] = "performance"
    """本地模式：performance=all-MiniLM-L6-v2（23MB）, quality=bge-large-zh-v1.5（400MB）"""

    local_model_dir: str = ""


class FeaturesConfig(StrictModel):
    vision: VisionFeatureConfig = Field(default_factory=VisionFeatureConfig)
    asr: ASRFeatureConfig = Field(default_factory=ASRFeatureConfig)
    tts: TTSFeatureConfig = Field(default_factory=TTSFeatureConfig)
    weather: WeatherFeatureConfig = Field(default_factory=WeatherFeatureConfig)
    web_search: WebSearchFeatureConfig = Field(default_factory=WebSearchFeatureConfig)
    embedding: EmbeddingFeatureConfig = Field(default_factory=EmbeddingFeatureConfig)
    long_term_memory: LongTermMemoryConfig = Field(default_factory=LongTermMemoryConfig)


# ============================================================
# 人格
# ============================================================


class PersonaConfig(StrictModel):
    active: str = "debata"
    """当前激活的人格目录名。从 personas/{name}/ 加载（所有人格平级，仓库自带 + 用户自创共存）。
    仓库自带 debata（开箱即用），用户自创的人格放同级目录。"""


# ============================================================
# 人格管理
# ============================================================


class PersonaManagementAgentConfig(StrictModel):
    """人格管理后台 Agent 的模型参数配置。"""

    provider: str = ""
    """provider ID。留空则运行时继承 agents.chat.provider。"""

    model: str = ""
    """模型 ID。留空则运行时继承 agents.chat.model。"""

    temperature: float = Field(default=0.6, ge=0.0, le=2.0)
    """采样温度。默认与主聊天 Agent 保持一致。"""

    top_p: float = Field(default=1.0, ge=0.0, le=1.0)
    """nucleus sampling 阈值。通常保持 1.0。"""

    max_tokens: int = Field(default=16384, ge=1)
    """单次响应最长 token 数。"""

    reasoning: ReasoningConfig | None = None
    """思考/推理配置。不填则不启用。"""

    first_token_timeout_seconds: float = 30.0
    """首 token 超时（秒）。流式模式下，首字未到即超时重试。"""


class PersonaManagementPersonaAgentConfig(PersonaManagementAgentConfig):
    """人格档案维护 Agent。"""

    timer_interval_minutes: int = Field(default=30, ge=1)
    """定时维护人格档案的间隔（分钟）。"""

    min_interval_seconds: int = Field(default=300, ge=0)
    """两次人格档案维护之间的最小间隔（秒）。"""


class PersonaManagementSocialAgentConfig(PersonaManagementAgentConfig):
    """社交关系维护 Agent。"""

    enabled: bool = True
    """是否启用社交关系后台维护。"""

    interval_minutes: int = Field(default=30, ge=1)
    """社交关系后台维护间隔（分钟）。"""


class PersonaManagementSubconsciousConfig(PersonaManagementAgentConfig):
    """潜意识后台处理配置。"""

    enabled: bool = True
    """是否启用潜意识后台处理。"""

    interval_minutes: int = Field(default=30, ge=1)
    """潜意识后台处理间隔（分钟）。"""

    merge_window_seconds: float = Field(default=30.0, ge=0.0)
    """潜意识事件合并窗口（秒）。"""

    max_window_seconds: float = Field(default=300.0, ge=0.0)
    """潜意识事件最长等待窗口（秒）。"""

    wake_keywords: list[str] = Field(default_factory=list)
    """触发潜意识唤醒的关键词列表。为空则仅按分数判断。"""

    min_wake_score: float = Field(default=0.5, ge=0.0, le=1.0)
    """触发潜意识唤醒的最低分数。"""


class PersonaManagementCollapseConfig(StrictModel):
    """精力耗尽后的昏睡兜底配置。"""

    grace_minutes: int = Field(default=60, ge=0)
    """精力归零后允许维持清醒的宽限时间（分钟）。"""

    sleep_hours: float = Field(default=12.0, ge=0.0)
    """触发昏睡后的默认睡眠时长。"""

    mood_penalty: float = Field(default=20.0, ge=0.0)
    """触发昏睡后的心情惩罚。"""


class PersonaManagementEnergyConfig(StrictModel):
    """精力状态配置。"""

    mode: Literal["disabled", "tool"] = "disabled"
    """disabled = 关闭；tool = 由工具/运行时状态提供。"""

    decay_per_hour: float = Field(default=1.5, ge=0.0)
    """清醒时每小时精力衰减。"""

    recovery_per_hour_sleep: float = Field(default=8.333, ge=0.0)
    """睡眠时每小时精力恢复。"""

    recovery_per_hour_eat: float = Field(default=15.0, ge=0.0)
    """进食时每小时精力恢复。"""

    long_sleep_threshold_minutes: int = Field(default=120, ge=0)
    """长睡眠判定阈值（分钟）。"""

    max_sleep_minutes: int = Field(default=720, ge=0)
    """单次睡眠最长时长（分钟）。"""

    collapse: PersonaManagementCollapseConfig = Field(
        default_factory=PersonaManagementCollapseConfig
    )
    """精力耗尽后的昏睡兜底。"""


class PersonaManagementSatietyConfig(StrictModel):
    """饱腹状态配置。"""

    mode: Literal["disabled", "tool"] = "disabled"
    """disabled = 关闭；tool = 由工具/运行时状态提供。"""

    decay_per_hour: float = Field(default=1.0, ge=0.0)
    """非进食时每小时饱腹衰减。"""

    recovery_per_minute: float = Field(default=0.5, ge=0.0)
    """进食时每分钟饱腹恢复。"""

    max_eat_minutes: int = Field(default=60, ge=0)
    """单次进食最长时长（分钟）。"""


class PersonaManagementMoodConfig(StrictModel):
    """心情状态配置。"""

    decay_per_hour: float = Field(default=0.5, ge=0.0)
    """心情每小时自然回落量。"""

    social_boost: float = Field(default=3.0, ge=0.0)
    """良性社交带来的心情增益。"""


class PersonaManagementConsolidationConfig(StrictModel):
    """人格管理记忆整合配置。"""

    daily_fallback_hour: int = Field(default=4, ge=0, le=23)
    """每日兜底整合小时。"""


class PersonaManagementPhysiologyConfig(StrictModel):
    """生理状态配置。"""

    energy: PersonaManagementEnergyConfig = Field(default_factory=PersonaManagementEnergyConfig)
    """精力状态。"""

    satiety: PersonaManagementSatietyConfig = Field(default_factory=PersonaManagementSatietyConfig)
    """饱腹状态。"""


class AgeBracketConfig(StrictModel):
    """年龄分档规则。"""

    name: str
    """分档名称。"""

    min: int = Field(ge=0)
    """命中此分档的最小年龄（含）。"""

    max: int | None = Field(default=None, ge=0)
    """命中此分档的最大年龄（含）。None 表示无上限。"""

    energy_decay_mult: float = Field(default=1.0, ge=0.0)
    """精力衰减乘数。"""

    energy_recovery_mult: float = Field(default=1.0, ge=0.0)
    """精力睡眠恢复乘数。"""

    satiety_decay_mult: float = Field(default=1.0, ge=0.0)
    """饱腹衰减乘数。"""

    mood_volatility_mult: float = Field(default=1.0, ge=0.0)
    """心情波动乘数。"""

    bedtime_hour: float = Field(default=23.0, ge=0.0, lt=24.0)
    """建议入睡小时。"""

    wakeup_hour: float = Field(default=7.0, ge=0.0, lt=24.0)
    """建议醒来小时。"""

    ideal_sleep_hours: float = Field(default=8.0, ge=0.0)
    """理想睡眠时长。"""

    monologue_style: str = ""
    """独白风格提示。"""

    emotional_hint: str = ""
    """情绪提示。"""

    social_hint: str = ""
    """社交提示。"""

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_age_bounds(cls, data: object) -> object:
        if isinstance(data, dict):
            data = dict(data)
            if "min" not in data and "min_age" in data:
                data["min"] = data.pop("min_age")
            if "max" not in data and "max_age" in data:
                data["max"] = data.pop("max_age")
        return data


def default_age_brackets() -> list[AgeBracketConfig]:
    """人格年龄系统的默认六档分表。"""
    return [
        AgeBracketConfig(
            name="幼年",
            min=0,
            max=5,
            energy_decay_mult=1.25,
            energy_recovery_mult=1.15,
            satiety_decay_mult=1.25,
            mood_volatility_mult=1.4,
            bedtime_hour=20.0,
            wakeup_hour=7.0,
            ideal_sleep_hours=11.0,
            monologue_style="短句、直觉、依赖照护",
            emotional_hint="情绪表达直接，容易因需求未满足而波动。",
            social_hint="更依赖熟悉对象的回应和陪伴。",
        ),
        AgeBracketConfig(
            name="儿童",
            min=6,
            max=12,
            energy_decay_mult=1.15,
            energy_recovery_mult=1.1,
            satiety_decay_mult=1.15,
            mood_volatility_mult=1.25,
            bedtime_hour=21.0,
            wakeup_hour=7.0,
            ideal_sleep_hours=10.0,
            monologue_style="好奇、具体、带一点跳跃",
            emotional_hint="对新鲜事物反应强，挫败后恢复也较快。",
            social_hint="需要明确、温和、及时的互动反馈。",
        ),
        AgeBracketConfig(
            name="少年",
            min=13,
            max=17,
            energy_decay_mult=1.05,
            energy_recovery_mult=1.05,
            satiety_decay_mult=1.1,
            mood_volatility_mult=1.2,
            bedtime_hour=22.0,
            wakeup_hour=7.0,
            ideal_sleep_hours=9.0,
            monologue_style="自我意识强，表达更敏感",
            emotional_hint="关系和评价更容易牵动情绪。",
            social_hint="重视被理解和被尊重。",
        ),
        AgeBracketConfig(
            name="青年",
            min=18,
            max=29,
            energy_decay_mult=1.0,
            energy_recovery_mult=1.0,
            satiety_decay_mult=1.0,
            mood_volatility_mult=1.0,
            bedtime_hour=23.0,
            wakeup_hour=7.0,
            ideal_sleep_hours=8.0,
            monologue_style="自然、灵活、有探索感",
            emotional_hint="情绪表达较完整，仍保留即时反应。",
            social_hint="能在独处和互动之间保持平衡。",
        ),
        AgeBracketConfig(
            name="成年",
            min=30,
            max=59,
            energy_decay_mult=1.05,
            energy_recovery_mult=0.95,
            satiety_decay_mult=0.95,
            mood_volatility_mult=0.85,
            bedtime_hour=22.5,
            wakeup_hour=6.5,
            ideal_sleep_hours=7.5,
            monologue_style="稳定、克制、重视因果",
            emotional_hint="情绪波动较缓，更偏向整理和消化。",
            social_hint="重视可靠、持续、边界清楚的关系。",
        ),
        AgeBracketConfig(
            name="年长",
            min=60,
            max=None,
            energy_decay_mult=1.2,
            energy_recovery_mult=0.85,
            satiety_decay_mult=0.9,
            mood_volatility_mult=0.75,
            bedtime_hour=21.5,
            wakeup_hour=5.5,
            ideal_sleep_hours=7.0,
            monologue_style="沉稳、回顾、慢节奏",
            emotional_hint="情绪表达更内敛，但会受长期关系牵动。",
            social_hint="重视陪伴、记忆延续和稳定回应。",
        ),
    ]


class PersonaManagementAgeConfig(StrictModel):
    """人格年龄系统配置。"""

    default_age: int | None = Field(default=None, ge=0)
    """默认年龄。None 表示未设置年龄，运行时不注入年龄系统。"""

    overrides: dict[str, int] = Field(default_factory=dict)
    """按对象 ID 覆盖年龄。键由运行时约定，值为年龄。"""

    brackets: list[AgeBracketConfig] = Field(default_factory=default_age_brackets)
    """年龄分档表。运行时按年龄命中对应分档。"""


class PersonaManagementConfig(StrictModel):
    """人格管理功能总开关和后台配置。"""

    enabled: bool = False
    """是否启用人格管理后台系统。默认关闭。"""

    persona_agent: PersonaManagementPersonaAgentConfig = Field(
        default_factory=PersonaManagementPersonaAgentConfig
    )
    """人格档案维护 Agent。provider/model 留空时继承 agents.chat。"""

    social_agent: PersonaManagementSocialAgentConfig = Field(
        default_factory=PersonaManagementSocialAgentConfig
    )
    """社交关系维护 Agent。provider/model 留空时继承 agents.chat。"""

    subconscious: PersonaManagementSubconsciousConfig = Field(
        default_factory=PersonaManagementSubconsciousConfig
    )
    """潜意识后台处理配置。provider/model 留空时继承 agents.chat。"""

    physiology: PersonaManagementPhysiologyConfig = Field(
        default_factory=PersonaManagementPhysiologyConfig
    )
    """生理状态配置。"""

    mood: PersonaManagementMoodConfig = Field(default_factory=PersonaManagementMoodConfig)
    """心情状态配置。"""

    consolidation: PersonaManagementConsolidationConfig = Field(
        default_factory=PersonaManagementConsolidationConfig
    )
    """人格管理记忆整合配置。"""

    age: PersonaManagementAgeConfig = Field(default_factory=PersonaManagementAgeConfig)
    """人格年龄系统配置。default_age=None 时不启用年龄注入。"""


# ============================================================
# 行为参数
# ============================================================


class TypingConfig(StrictModel):
    """遗留打字速度配置；发送间隔由模型逐条填写 target.delay。"""

    chars_per_second: float = 1.0
    """已废弃：不再用于发送工具。"""

    english_chars_per_second: float = 5.0
    """已废弃：不再用于发送工具。"""

    @model_validator(mode="before")
    @classmethod
    def drop_deprecated_delay_fields(cls, data: object) -> object:
        if isinstance(data, dict):
            data = dict(data)
            data.pop("min_delay_seconds", None)
            data.pop("max_delay_seconds", None)
            data.pop("clamp_model_delay", None)
        return data


class RateLimitConfig(StrictModel):
    """对非好友的速率限制（防被陌生人刷屏）。好友/管理员不受限。"""

    window_seconds: int = 60
    """滑动窗口（秒）。"""

    max_messages: int = 5
    """窗口内允许的最大消息数。"""

    enabled: bool = True
    """关闭后所有用户都不限速。"""


class SummarizeConfig(StrictModel):
    """滚动摘要压缩触发阈值。"""

    trigger_at_tokens: int | None = None
    """活跃窗口估算 token 达到此值时触发滚动摘要。显式配置优先于百分比推导。"""

    target_after_tokens: int | None = None
    """滚动摘要后活跃窗口目标 token。显式配置优先于百分比推导。"""

    trigger_at_context_percent: int = Field(default=75, ge=50, le=100)
    """未显式配置 trigger_at_tokens 时，按工作上下文预算此百分比推导滚动摘要触发阈值。"""

    target_after_context_percent: int = Field(default=50, ge=50, le=100)
    """未显式配置 target_after_tokens 时，按工作上下文预算此百分比推导活跃窗口目标。"""

    retry_target_after_context_percent: int = Field(default=30, ge=5, le=100)
    """第一次压缩后仍超预算或压缩失败时，重试压缩使用的活跃窗口目标百分比。"""

    @model_validator(mode="before")
    @classmethod
    def drop_deprecated_working_range_fields(cls, data: object) -> object:
        if isinstance(data, dict):
            data = dict(data)
            data.pop("range_start_messages", None)
            data.pop("range_end_messages", None)
            data.pop("trigger_at_messages", None)
        return data


class ToolResultBudgetConfig(StrictModel):
    """单个工具结果的 token 预算。

    inline_budget_tokens 控制工具可直接回传给模型的预算。
    artifact_threshold_tokens 供资料型工具判断何时改写 workspace 文件。
    hard_cap_tokens 是事故兜底，不应作为正常截断机制。
    """

    inline_budget_tokens: int = Field(default=800, ge=256)
    artifact_threshold_tokens: int | None = Field(default=None, ge=256)
    hard_cap_tokens: int | None = Field(default=None, ge=512)


class ContextLengthBudgetRule(StrictModel):
    """按 provider 预设 context_length 推导工作上下文预算的规则。"""

    min_context_length_tokens: int = Field(ge=1)
    """模型 context_length 至少达到此值时命中。"""

    budget_tokens: int = Field(ge=1024)
    """命中后使用的工作上下文预算。"""


class ContextBudgetRecommendationConfig(StrictModel):
    """max_context_tokens 为空时的自动推荐规则。"""

    model_name_budget_tokens: dict[str, int] = Field(
        default_factory=lambda: {
            "deepseek-v4-pro": 350_000,
            "deepseek-v4": 300_000,
            "claude": 150_000,
        }
    )
    """模型名包含指定片段时使用的预算。按配置顺序匹配。"""

    context_length_rules: list[ContextLengthBudgetRule] = Field(
        default_factory=lambda: [
            ContextLengthBudgetRule(
                min_context_length_tokens=1_000_000,
                budget_tokens=300_000,
            ),
            ContextLengthBudgetRule(
                min_context_length_tokens=200_000,
                budget_tokens=150_000,
            ),
            ContextLengthBudgetRule(
                min_context_length_tokens=128_000,
                budget_tokens=96_000,
            ),
        ]
    )
    """provider 预设 context_length 命中规则。按配置顺序匹配。"""

    context_length_scale_percent: int = Field(default=75, ge=1, le=100)
    """低于所有阈值时，按模型 context_length 的此百分比推导。"""

    min_scaled_budget_tokens: int = Field(default=4096, ge=1024)
    """按百分比推导时的最低工作上下文预算。"""

    fallback_budget_tokens: int = Field(default=96_000, ge=1024)
    """模型名和 context_length 都无法命中时的兜底预算。"""


def default_tool_result_budgets() -> dict[str, ToolResultBudgetConfig]:
    """项目推荐的各工具结果预算。默认偏保守，大资料应走 artifact/分页。"""
    return {
        # 动作类
        "send_private_messages": ToolResultBudgetConfig(inline_budget_tokens=600),
        "send_group_message": ToolResultBudgetConfig(inline_budget_tokens=600),
        "send_voice_message": ToolResultBudgetConfig(inline_budget_tokens=800),
        "upload_file": ToolResultBudgetConfig(inline_budget_tokens=500),
        "recall_message": ToolResultBudgetConfig(inline_budget_tokens=400),
        "set_friend_add_request": ToolResultBudgetConfig(inline_budget_tokens=400),
        "set_group_add_request": ToolResultBudgetConfig(inline_budget_tokens=400),
        "no_action": ToolResultBudgetConfig(inline_budget_tokens=256),
        "schedule_wakeup": ToolResultBudgetConfig(inline_budget_tokens=700),
        # 查询类
        "list_contacts": ToolResultBudgetConfig(
            inline_budget_tokens=1200,
            artifact_threshold_tokens=1200,
        ),
        "get_user_info": ToolResultBudgetConfig(inline_budget_tokens=800),
        "get_weather": ToolResultBudgetConfig(inline_budget_tokens=1000),
        "web_search": ToolResultBudgetConfig(inline_budget_tokens=1800),
        # 资料类
        "describe_image": ToolResultBudgetConfig(
            inline_budget_tokens=1800,
            artifact_threshold_tokens=2500,
        ),
        "read_file": ToolResultBudgetConfig(
            inline_budget_tokens=2500,
            artifact_threshold_tokens=2500,
        ),
        "run_python": ToolResultBudgetConfig(
            inline_budget_tokens=2500,
            artifact_threshold_tokens=2500,
        ),
        "get_forward_msg": ToolResultBudgetConfig(
            inline_budget_tokens=1000,
            artifact_threshold_tokens=1000,
        ),
        "recall_history": ToolResultBudgetConfig(
            inline_budget_tokens=3000,
            artifact_threshold_tokens=3000,
        ),
        "get_recent_chat_messages": ToolResultBudgetConfig(
            inline_budget_tokens=3000,
            artifact_threshold_tokens=3000,
        ),
        # 子 Agent
        "start_agent_task": ToolResultBudgetConfig(inline_budget_tokens=600),
        "summarize_chat_history": ToolResultBudgetConfig(inline_budget_tokens=600),
        "summarize_conversation": ToolResultBudgetConfig(inline_budget_tokens=600),
    }


class ContextConfig(StrictModel):
    """上下文预算配置。max_context_tokens 是工作预算，不是模型硬上限。"""

    max_context_tokens: int | None = None
    """None = 根据当前模型预设 context_length 推导工作预算。"""

    recommended_context_budget: ContextBudgetRecommendationConfig = Field(
        default_factory=ContextBudgetRecommendationConfig
    )
    """max_context_tokens=None 时使用的自动推荐规则。"""

    reserve_output_tokens: int = Field(default=8192, ge=1024)
    """为模型输出保留的 token。"""

    prompt_overhead_estimate_tokens: int = Field(default=12000, ge=0)
    """系统提示、工具 schema 等非历史内容的预估开销。"""

    memory_token_budget: int = Field(default=4096, ge=256)
    """长期重要记忆注入预算。Phase A 先作为预算字段预留，Phase D 精细使用。"""

    summary_token_budget: int = Field(default=4096, ge=256)
    """滚动摘要注入预算。"""

    tool_result_default_budget_tokens: int = Field(default=800, ge=256)
    """未单独配置的工具结果 inline 默认预算。"""

    tool_result_default_hard_cap_tokens: int = Field(default=3000, ge=512)
    """未单独配置的工具结果事故兜底上限。"""

    tool_result_budgets: dict[str, ToolResultBudgetConfig] = Field(
        default_factory=default_tool_result_budgets
    )
    """按工具名配置结果预算。普通用户不建议手动修改。"""

    tool_result_soft_limit_tokens: int = Field(default=600, ge=64)
    """旧配置兼容：单条工具结果软阈值。新逻辑优先使用 tool_result_budgets。"""

    tool_result_hard_cap_tokens: int = Field(default=1500, ge=128)
    """旧配置兼容：单条工具结果硬上限。新逻辑优先使用 tool_result_budgets。"""

    tool_result_soft_overrides: dict[str, int] = Field(default_factory=dict)
    """旧配置兼容：按工具名覆盖软阈值，如 {"describe_image": 900}。"""

    @model_validator(mode="before")
    @classmethod
    def drop_deprecated_working_window_fields(cls, data: object) -> object:
        if isinstance(data, dict):
            data = dict(data)
            for field in (
                "min_working_history_tokens",
                "current_conversation_min_records",
                "runtime_record_keep_count",
                "send_receipt_keep_count",
                "no_action_keep_count",
            ):
                data.pop(field, None)
        return data


class BehaviorConfig(StrictModel):
    merge_window_seconds: float = Field(default=0.5, ge=0.0)
    """消息合并窗口（秒）。同一窗口内的消息合并到一次 AI 调用。"""

    recall_merge_window_seconds: float = Field(default=2.0, ge=0.0)
    """撤回事件合并窗口（秒）。"""

    proactive_think_interval_seconds: float = Field(default=600.0, ge=10.0)
    """主动思考间隔（秒）。每过此时长由 ProactiveAgent 判定一次是否需要主动开口。
    主动思考依赖 agents.proactive 配置；若未配置则此字段无效。"""

    proactive_context_token_budget: int = Field(default=4096, ge=1024)
    """主动思考路由器可使用的上下文预算。默认 4K，避免后台判断吃掉过多上下文和成本。"""

    proactive_router_text_limit_tokens: int = Field(default=256, ge=32)
    """主动路由输入文本片段的截断预算。"""

    proactive_router_tool_result_inline_tokens: int = Field(default=96, ge=32)
    """主动路由工具结果内联预算。"""

    proactive_router_tool_result_hard_cap_tokens: int = Field(default=160, ge=64)
    """主动路由工具结果事故兜底上限。"""

    proactive_router_summary_limit_tokens: int = Field(default=1024, ge=128)
    """主动路由历史摘要文本上限。"""

    proactive_router_history_token_budget: int = Field(default=16384, ge=1024)
    """主动路由可读取的历史 token 预算。"""

    pending_request_timeout_seconds: float = Field(default=1800.0, ge=60.0)
    """好友/群加入请求暂存的过期时间（秒）。超时后未审核的请求被丢弃。"""

    default_history_fetch_count: int = Field(default=100, ge=1)
    """summarize_chat_history 工具拉取群历史的默认 count。
    （此字段不属于历史总结流程；与 SummarizeConfig 的阈值字段无关。）"""

    typing: TypingConfig = Field(default_factory=TypingConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    summarize: SummarizeConfig = Field(default_factory=SummarizeConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)

    @model_validator(mode="before")
    @classmethod
    def drop_deprecated_persona_refine_history_turns(cls, data: object) -> object:
        if isinstance(data, dict) and "persona_refine_history_turns" in data:
            data = dict(data)
            data.pop("persona_refine_history_turns", None)
        return data


# ============================================================
# 根配置
# ============================================================


class RootConfig(StrictModel):
    """完整配置树的根。"""

    version: int = 2

    app: AppMeta = Field(default_factory=AppMeta)
    adapters: dict[str, NapCatAdapterConfig] = Field(default_factory=dict)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    agents: AgentsConfig
    features: FeaturesConfig = Field(default_factory=FeaturesConfig)
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    persona_management: PersonaManagementConfig = Field(
        default_factory=PersonaManagementConfig
    )
    behavior: BehaviorConfig = Field(default_factory=BehaviorConfig)

    @field_validator("version")
    @classmethod
    def validate_version(cls, v: int) -> int:
        if v < 2:
            raise ValueError(
                f"配置版本过旧（v{v}），请使用迁移工具升级到 v2"
            )
        return v

    @model_validator(mode="after")
    def validate_references(self) -> RootConfig:
        """确保所有 provider 引用、密钥 ID 引用都指向已存在的对象。"""
        provider_ids = set(self.providers.keys())

        def ensure_provider_exists(path: str, provider: str) -> None:
            if provider in provider_ids:
                return
            example_id = sorted(provider_ids)[0] if provider_ids else "deepseek_main"
            raise ValueError(
                f"{path}.provider = '{provider}' 未在 providers 中定义。\n"
                f"  已定义的 providers: {sorted(provider_ids)}\n"
                f"  修法：编辑 config.yaml 把 {path}.provider "
                f"改成上面已存在的 ID（如 '{example_id}'），"
                f"或在 providers 段新增 '{provider}' 的配置。"
            )

        # 检查每个 Agent 的 provider 引用
        for name, agent in self._iter_agents():
            ensure_provider_exists(f"agents.{name}", agent.provider)

        # 人格管理后台 Agent 允许空 provider/model，由运行时继承 agents.chat。
        for name, agent in self._iter_persona_management_agents():
            if agent.provider:
                ensure_provider_exists(f"persona_management.{name}", agent.provider)

        # 检查 features 中的 provider 引用（如有）
        for _feat_name, feat in [
            ("vision", self.features.vision),
            ("asr", self.features.asr),
            ("tts", self.features.tts),
        ]:
            prov = getattr(feat, "provider", None)
            if feat.enabled and prov and prov not in provider_ids:
                # features 的 provider 既可以引用 providers 字典，也可以是独立标识
                # 这里仅在引用看起来像 ID 时提示
                pass  # 宽松：features 的 provider 可能是 SDK 内部的标识

        return self

    def _iter_agents(self):
        """遍历所有非空 Agent 配置，返回 (name, AgentConfig)。"""
        yield "chat", self.agents.chat
        for name in ("proactive", "summary", "persona_gen"):
            agent = getattr(self.agents, name)
            if agent is not None:
                yield name, agent

    def _iter_persona_management_agents(self):
        """遍历人格管理后台 Agent，返回 (name, PersonaManagementAgentConfig)。"""
        yield "persona_agent", self.persona_management.persona_agent
        yield "social_agent", self.persona_management.social_agent
        yield "subconscious", self.persona_management.subconscious
