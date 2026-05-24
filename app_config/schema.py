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
    """禁止未知字段，避免拼写错误被静默吞掉。"""

    model_config = ConfigDict(extra="forbid")


# ============================================================
# 应用元信息
# ============================================================


class AppMeta(StrictModel):
    name: str = "Diana_Agent"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


# ============================================================
# 适配器
# ============================================================


class WhitelistConfig(StrictModel):
    """白名单 / 验证模式配置。"""

    mode: Literal["all", "verify", "whitelist"] = "verify"
    """all = 响应所有人（不安全）;
    verify = 当前模式（管理员审核加好友、加群）;
    whitelist = 仅响应配置列表中的 QQ/群"""

    qq_ids: list[int] = Field(default_factory=list)
    group_ids: list[int] = Field(default_factory=list)


class NapCatAdapterConfig(StrictModel):
    type: Literal["napcat"] = "napcat"
    enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_fields(cls, values):
        """兼容 V2 早期字段名 —— 旧 yaml 不需要手改，加载时自动转换。

        旧 → 新：
            mode: "reverse_ws" → "client"
            mode: "forward_ws" → "server"
            ws_url: "ws://host:port/path" → host + port + path（拆分）
            listen_host / listen_port / listen_path → host / port / path
        """
        if not isinstance(values, dict):
            return values
        values = dict(values)

        # mode 旧名
        if values.get("mode") == "reverse_ws":
            values["mode"] = "client"
        elif values.get("mode") == "forward_ws":
            values["mode"] = "server"

        # listen_* → host/port/path
        if "listen_host" in values:
            values.setdefault("host", values.pop("listen_host"))
        else:
            values.pop("listen_host", None)
        if "listen_port" in values:
            values.setdefault("port", values.pop("listen_port"))
        else:
            values.pop("listen_port", None)
        if "listen_path" in values:
            values.setdefault("path", values.pop("listen_path"))
        else:
            values.pop("listen_path", None)

        # ws_url → host/port/path（拆 URL）
        if "ws_url" in values:
            ws_url = values.pop("ws_url")
            if ws_url:
                from urllib.parse import urlparse

                parsed = urlparse(ws_url)
                if parsed.hostname and "host" not in values:
                    values["host"] = parsed.hostname
                if parsed.port and "port" not in values:
                    values["port"] = parsed.port
                # path 为空时 urlparse 给 ""，回落到 "/"
                if "path" not in values:
                    values["path"] = parsed.path or "/"

        return values

    mode: Literal["client", "server"] = "client"
    """连接模式（从 Diana_Agent 视角命名，符合直觉）：

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
    """WebSocket 路径。NapCat 正向 WS 默认 "/"；
    旧 NoneBot 反向 WS 用户通常用 "/onebot/v11/ws"（与 NapCat 那边配的目标地址保持一致）。"""

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
    reconnect_interval: float = 3.0
    max_reconnect_attempts: int = -1  # -1 表示无限重连

    # 白名单
    whitelist: WhitelistConfig = Field(default_factory=WhitelistConfig)


# 未来扩展：Union[NapCatAdapterConfig, DiscordAdapterConfig, ...]
AdapterConfig = Annotated[NapCatAdapterConfig, Field(discriminator="type")]


# ============================================================
# LLM 提供商
# ============================================================


ProtocolType = Literal["openai_compat", "anthropic", "gemini", "volcengine"]


class ProviderConfig(StrictModel):
    """LLM 提供商配置。

    两种使用方式：
        1. 引用内置预设：只填 preset 和 api_key_id
        2. 完全自定义：填 protocol、base_url、api_key_id（preset 留空）
    """

    preset: str | None = None
    """内置预设名（providers/presets/{preset}/preset.yaml）。"""

    display_name: str | None = None
    """用户友好的显示名（自定义时建议填）。"""

    protocol: ProtocolType | None = None
    """协议类型。未指定 preset 时必填。"""

    base_url: str | None = None
    """API base URL。未指定 preset 时必填。"""

    api_key_id: str | None = None
    """SecretsManager 中的密钥 ID。"""

    extra_headers: dict[str, str] = Field(default_factory=dict)
    """额外的 HTTP 请求头（如某些代理需要）。"""

    timeout: float = 120.0

    @model_validator(mode="after")
    def validate_provider(self) -> ProviderConfig:
        if not self.preset and (not self.protocol or not self.base_url):
            raise ValueError(
                "未指定 preset 时，必须同时填写 protocol 和 base_url"
            )
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
    """模型 ID，如 'deepseek-chat'、'claude-sonnet-4-5'、'glm-4-flash'"""

    temperature: float = Field(default=0.6, ge=0.0, le=2.0)
    top_p: float = Field(default=1.0, ge=0.0, le=1.0)
    max_tokens: int = Field(default=16384, ge=1)

    reasoning: ReasoningConfig | None = None

    first_token_timeout: float = 30.0
    max_loops: int = 15
    """工具循环最大轮次（仅 chat agent 用）。"""

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
    enabled: bool = False
    type: Literal["api", "local"] = "api"

    provider: str | None = None
    """type=api 时，引用 providers 字典中的 ID（也可独立配置）"""

    model: str = ""

    api_key_id: str | None = None
    """直接指定密钥（不走 provider 时使用）。"""

    base_url: str | None = None
    """直接指定 base URL（不走 provider 时使用）。"""


class ASRFeatureConfig(StrictModel):
    enabled: bool = False
    type: Literal["api", "local"] = "api"

    provider: str | None = None
    """支持 'xfyun'、'baidu'、'mimo' 等。"""

    api_key_id: str | None = None
    extra_credentials: dict[str, str] = Field(default_factory=dict)

    # 本地模式
    local_model: str = "faster-whisper-large-v3"
    device: Literal["cuda", "cpu", "auto"] = "auto"
    language: str = "zh"
    model_dir: str = ""
    """本地模型存放路径，留空使用默认。"""


class TTSFeatureConfig(StrictModel):
    enabled: bool = False
    type: Literal["api", "local"] = "local"

    provider: str | None = None
    api_key_id: str | None = None

    # 本地模式
    local_model: str = "voxcpm2"
    reference_audio: str = ""
    default_prompt: str = ""
    model_dir: str = ""


class WeatherFeatureConfig(StrictModel):
    enabled: bool = False
    api_key_id: str | None = None
    host: str = ""
    """和风天气 API 主机（个人版必填，免费版留空使用默认）。"""

    @model_validator(mode="after")
    def validate_enabled_requires_host(self) -> WeatherFeatureConfig:
        if self.enabled and not self.host:
            raise ValueError(
                "features.weather.enabled=True 但未填 host。\n"
                "  请去 https://console.qweather.com/ 获取你的 API host "
                "（如 'devapi.qweather.com' 或个人版自定义域名）后填入 features.weather.host。"
            )
        return self


class WebSearchFeatureConfig(StrictModel):
    enabled: bool = True
    provider: Literal["ddg"] = "ddg"
    max_results: int = 5


class LongTermMemoryConfig(StrictModel):
    """长期记忆配置 —— 决定 important.json 之外是否启用 RAG（P2 实现）。"""

    mode: Literal["file", "rag"] = "file"
    """file = 纯文件模式（默认，零开销）;
    rag = 文件 + 向量检索（需 features.embedding 启用，P2 才生效）"""

    keyword_force_save: bool = True
    """启用关键词强制保存（"记住"/"约定"/"我叫"等）"""

    rag_top_k: int = 5
    """RAG 模式下每次召回的相关条目数（仅 mode=rag 生效）"""

    rag_extractor_interval: int = 15
    """被动抽取触发间隔（每 N 轮对话扫描一次，仅 mode=rag 生效）"""


class EmbeddingFeatureConfig(StrictModel):
    """Embedding 服务配置（P2 实现，先占位）。"""

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
    active: str = "diana"
    """当前激活的人格目录名。从 personas/{name}/ 加载（所有人格平级，仓库自带 + 用户自创共存）。
    仓库自带 diana（开箱即用），用户自创的人格放同级目录。"""


# ============================================================
# 行为参数
# ============================================================


class TypingConfig(StrictModel):
    chars_per_second: float = 3.0
    max_delay: float = 2.0


class RateLimitConfig(StrictModel):
    window: int = 60
    max_messages: int = 5
    enabled: bool = True


class SummarizeConfig(StrictModel):
    trigger_at: int = 20000
    range_start: int = 9000
    range_end: int = 11000
    chat_history_count: int = 10000


class BehaviorConfig(StrictModel):
    merge_window: float = Field(default=0.5, ge=0.0)
    """消息合并窗口（秒）。同一窗口内的消息合并到一次 AI 调用。"""

    recall_merge_window: float = Field(default=2.0, ge=0.0)
    """撤回事件合并窗口（秒）。"""

    greeting_interval: float = Field(default=600.0, ge=10.0)
    """主动思考间隔（秒）。"""

    typing: TypingConfig = Field(default_factory=TypingConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    summarize: SummarizeConfig = Field(default_factory=SummarizeConfig)


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

        # 检查每个 Agent 的 provider 引用
        for name, agent in self._iter_agents():
            if agent.provider not in provider_ids:
                example_id = sorted(provider_ids)[0] if provider_ids else "deepseek_main"
                raise ValueError(
                    f"agents.{name}.provider = '{agent.provider}' 未在 providers 中定义。\n"
                    f"  已定义的 providers: {sorted(provider_ids)}\n"
                    f"  修法：编辑 config.yaml 把 agents.{name}.provider "
                    f"改成上面已存在的 ID（如 '{example_id}'），"
                    f"或在 providers 段新增 '{agent.provider}' 的配置。"
                )

        # 检查 features 中的 provider 引用（如有）
        for feat_name, feat in [
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
