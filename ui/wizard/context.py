"""向导共享上下文 + BaseStepView 基类。

WizardContext 收集向导全过程的所有用户选择，最后由 window.py 转成 RootConfig
+ SecretsManager 写入并保存。

每个 step view 接收 context、在显示时从 context 回填、在 save() 时写回 context。
所有 view 的接口统一在 BaseStepView。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from agents.persona_gen_agent import PersonaBrief

from .components import WhitelistState
from .flow import WIZARD_PATH_RECOMMENDED


# ============================================================
# 子配置块
# ============================================================


@dataclass(slots=True)
class MainModelChoice:
    """主聊天模型的选择结果。"""

    preset: str = "deepseek"
    """provider 预设名（providers/presets/{preset}）。'custom' 表示完全自定义。"""

    display_name: str = "DeepSeek"
    """显示名"""

    api_key: str = ""
    """API 密钥明文（向导结束时写入 SecretsManager）"""

    model: str = "deepseek-v4-flash"
    """模型 ID"""

    base_url: str = ""
    """自定义模式时填，preset 模式留空"""

    protocol: Literal["openai_compat", "anthropic"] = "openai_compat"
    """自定义模式时的协议"""

    temperature: float = 0.6
    top_p: float = 1.0
    max_tokens: int = 16384

    # 思考 / reasoning（DeepSeek R1、Claude thinking、GPT o1 等）
    reasoning_enabled: bool = False
    reasoning_budget: Literal["low", "medium", "high"] | None = None
    """思考深度。None = 用默认。"""
    reasoning_max_tokens: int | None = None
    """单独控制思考阶段的 token 上限（如 Claude thinking budget）。None = 不指定。"""


@dataclass(slots=True)
class SubAgentChoice:
    """proactive / summary 子 Agent 的选择。"""

    use_main: bool = True
    """True = 直接复用主模型 provider；False = 单独配置"""

    preset: str = "deepseek"
    api_key: str = ""
    model: str = "deepseek-v4-flash"
    temperature: float = 0.3
    max_tokens: int = 4096
    enabled: bool = True
    """False = 不启用这个 Agent（如不想要主动思考）"""

    reasoning_enabled: bool = False
    reasoning_budget: Literal["low", "medium", "high"] | None = None


@dataclass(slots=True)
class FeatureChoice:
    """单个 feature 开关 + 必要字段。"""

    enabled: bool = False
    api_key: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AdapterChoice:
    """NapCat 适配器配置。"""

    mode: Literal["client", "server"] = "client"
    host: str = "127.0.0.1"
    port: int = 3001
    path: str = "/"
    token: str = ""
    manage_process: bool = False
    process_path: str = ""
    whitelist: WhitelistState = field(default_factory=WhitelistState)


@dataclass(slots=True)
class PersonaChoice:
    """人格选择。"""

    source: Literal["builtin", "create", "import"] = "builtin"
    active: str = "debata"
    """source=builtin 时是内置名；source=create 时是 brief.name；source=import 时是导入后的目录名"""

    brief: PersonaBrief | None = None
    generated_xml: str = ""
    import_path: str = ""


# ============================================================
# 顶层 WizardContext
# ============================================================


@dataclass(slots=True)
class WizardContext:
    """向导全程共享的状态。"""

    path: str = WIZARD_PATH_RECOMMENDED
    """recommended / custom"""

    main: MainModelChoice = field(default_factory=MainModelChoice)
    proactive: SubAgentChoice = field(default_factory=SubAgentChoice)
    summary: SubAgentChoice = field(default_factory=SubAgentChoice)

    # features —— 每项 enabled + 可能的密钥
    vision: FeatureChoice = field(default_factory=FeatureChoice)
    asr: FeatureChoice = field(default_factory=FeatureChoice)
    tts: FeatureChoice = field(default_factory=FeatureChoice)
    weather: FeatureChoice = field(default_factory=FeatureChoice)
    web_search: FeatureChoice = field(
        default_factory=lambda: FeatureChoice(enabled=True)
    )
    long_term_memory_mode: Literal["file", "rag"] = "file"
    long_term_memory_keyword_trigger_save: bool = True

    # RAG embedding（mode=rag 时才用）
    embedding_type: Literal["api", "local"] = "api"
    embedding_provider: str = "volcengine"
    embedding_provider_preset: str = ""
    embedding_provider_display_name: str = ""
    embedding_provider_base_url: str = ""
    embedding_provider_protocol: Literal["openai_compat", "anthropic"] = "openai_compat"
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_local_quality: Literal["performance", "quality"] = "performance"
    embedding_local_model_dir: str = ""

    adapter: AdapterChoice = field(default_factory=AdapterChoice)

    persona: PersonaChoice = field(default_factory=PersonaChoice)

    admin_qq: str = ""
    """管理员 QQ。可选。若填了，会保存到 persona 的 PERSONA_VARS['admins']。"""

    admin_name: str = ""
    """管理员显示名。可选；生成自定义人格时会加入提示词。"""

    def to_persona_brief(self) -> PersonaBrief:
        """如果用户选择 create 模式，返回 brief；否则返回空 brief。"""
        brief = self.persona.brief or PersonaBrief(name=self.persona.active or "")
        brief.admin_name = brief.admin_name or self.admin_name
        brief.admin_qq = brief.admin_qq or self.admin_qq
        return brief


# ============================================================
# BaseStepView
# ============================================================


class BaseStepView(QWidget):
    """所有 step view 的基类。

    每个 step view 在被显示前由 window 调用 refresh() 从 context 回填。
    点击"下一步"时由 window 调用 save() 校验并写回 context。
    校验失败时 save() 返回 False 并通过 invalid_input 信号通知 window。
    """

    invalid_input = Signal(str)
    """校验失败时发出，参数是给用户看的错误信息（中文）。"""

    request_advance = Signal()
    """某些 view 内部点击行为想直接跳到下一步（如 welcome 选了路径就该自动 next）。
    window 接到后可以决定是否真的 advance。"""

    def __init__(self, context: WizardContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._context = context

    @property
    def context(self) -> WizardContext:
        return self._context

    def refresh(self) -> None:
        """显示前调用：把 context 中的当前值回填到控件。子类按需重写。"""

    def save(self) -> bool:
        """点击 next 时调用：校验并写回 context。

        校验通过返回 True；失败返回 False，并通过 invalid_input emit 错误。
        子类按需重写。
        """
        return True


__all__ = [
    "WizardContext",
    "MainModelChoice",
    "SubAgentChoice",
    "FeatureChoice",
    "AdapterChoice",
    "PersonaChoice",
    "BaseStepView",
]
