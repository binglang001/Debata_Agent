"""Debata_Agent 配置系统。

公开 API：
    AppPaths              —— 跨平台路径管理
    RootConfig            —— 完整配置 schema
    SecretsManager        —— 加密的密钥管理器
    initialize_runtime_data —— 启动期数据根初始化
    load_config           —— 从磁盘加载并校验配置
    save_config           —— 保存配置到磁盘（原子写入）
    get_config            —— 获取已加载的全局单例
"""

from .config_migration import (
    CURRENT_CONFIG_VERSION,
    ConfigMigrationReport,
    migrate_config,
)
from .data_migration import DataMigrationError, ensure_data_root_initialized
from .loader import (
    ConfigError,
    get_config,
    load_config,
    save_config,
    set_active_config,
)
from .paths import AppPaths
from .schema import (
    AdapterConfig,
    AgentConfig,
    AgentsConfig,
    AppMeta,
    ASRFeatureConfig,
    BehaviorConfig,
    ContextConfig,
    EmbeddingFeatureConfig,
    FeaturesConfig,
    LongTermMemoryConfig,
    NapCatAdapterConfig,
    PersonaConfig,
    ProviderConfig,
    RateLimitConfig,
    ReasoningConfig,
    RootConfig,
    SummarizeConfig,
    ToolResultBudgetConfig,
    TTSFeatureConfig,
    TypingConfig,
    VisionFeatureConfig,
    WeatherFeatureConfig,
    WebSearchFeatureConfig,
    WhitelistConfig,
    default_tool_result_budgets,
)
from .secrets import SecretsError, SecretsManager
from .startup import initialize_runtime_data
from .versioning import (
    VersionRegistry,
    create_default_registry,
    get_application_version,
)

__all__ = [
    # paths
    "AppPaths",
    "initialize_runtime_data",
    "ensure_data_root_initialized",
    "DataMigrationError",
    "get_application_version",
    "create_default_registry",
    "VersionRegistry",
    # config migration
    "CURRENT_CONFIG_VERSION",
    "ConfigMigrationReport",
    "migrate_config",
    # schema
    "RootConfig",
    "AppMeta",
    "AdapterConfig",
    "NapCatAdapterConfig",
    "WhitelistConfig",
    "ProviderConfig",
    "ReasoningConfig",
    "AgentConfig",
    "AgentsConfig",
    "FeaturesConfig",
    "VisionFeatureConfig",
    "ASRFeatureConfig",
    "TTSFeatureConfig",
    "WeatherFeatureConfig",
    "WebSearchFeatureConfig",
    "EmbeddingFeatureConfig",
    "LongTermMemoryConfig",
    "PersonaConfig",
    "BehaviorConfig",
    "ContextConfig",
    "ToolResultBudgetConfig",
    "default_tool_result_budgets",
    "TypingConfig",
    "RateLimitConfig",
    "SummarizeConfig",
    # loader
    "load_config",
    "save_config",
    "get_config",
    "set_active_config",
    "ConfigError",
    # secrets
    "SecretsManager",
    "SecretsError",
]
