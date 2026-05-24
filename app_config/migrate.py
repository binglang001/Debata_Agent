"""从 V1（.env + config.yaml + personas/）一次性迁移到 V2 结构。

V1 → V2 映射：
    .env                                → secrets.enc（API 密钥）+ data/config.yaml（其余）
    config.yaml                         → data/config.yaml
    personas/{name}/persona_prompt.py   → data/personas/{name}/persona_prompt.py
    personas/{name}/memory.json         → 不迁移（用户已确认记忆从零开始）
    emoji/                              → data/emoji/

注意：
    - 此迁移幂等：重复运行不会覆盖已存在的新文件。
    - 不删除旧文件，由用户在 UI 中确认后再删。
    - 个人人格（如 yuexi）会保留在仓库 personas/ 下被 gitignore，
      迁移后也会复制到 data/personas/ 作为运行时副本。
"""

from __future__ import annotations

import logging
import shutil
from typing import Any

import yaml
from dotenv import dotenv_values

from .paths import AppPaths
from .schema import (
    AgentConfig,
    AgentsConfig,
    AppMeta,
    BehaviorConfig,
    FeaturesConfig,
    NapCatAdapterConfig,
    PersonaConfig,
    ProviderConfig,
    RateLimitConfig,
    RootConfig,
    SummarizeConfig,
    TypingConfig,
    VisionFeatureConfig,
    WeatherFeatureConfig,
    WebSearchFeatureConfig,
    WhitelistConfig,
)
from .secrets import SecretsManager

logger = logging.getLogger(__name__)


# 旧 .env 密钥名 → 新 secrets ID
LEGACY_SECRET_MAP: dict[str, str] = {
    "DEEPSEEK_API_KEY": "deepseek_main",
    "VOLCENGINE_API_KEY": "volcengine_main",
    "QWEATHER_KEY": "qweather",
    "ONEBOT_ACCESS_TOKEN": "napcat_default_token",
}


def detect_legacy(paths: AppPaths) -> dict[str, bool]:
    """检测旧配置存在情况。

    V2 扁平结构下，LEGACY_PERSONAS_DIR 与 PERSONAS_DIR 指向同一目录，
    所以 personas 永远不算 legacy（仓库自带的 diana/ 是 V2 合法目录）。
    """
    same_personas_dir = (
        paths.LEGACY_PERSONAS_DIR.resolve() == paths.PERSONAS_DIR.resolve()
    )
    same_config_path = (
        paths.LEGACY_CONFIG_FILE.exists()
        and paths.LEGACY_CONFIG_FILE.resolve() == paths.CONFIG_FILE.resolve()
    )
    return {
        "env": paths.LEGACY_ENV_FILE.exists(),
        "config_yaml": paths.LEGACY_CONFIG_FILE.exists() and not same_config_path,
        "personas": (
            not same_personas_dir
            and paths.LEGACY_PERSONAS_DIR.exists()
            and any(
                d.is_dir() and not d.name.startswith("_") and not d.name.startswith("__")
                for d in paths.LEGACY_PERSONAS_DIR.iterdir()
            )
        ),
        "emoji": paths.LEGACY_EMOJI_DIR.exists()
        and any(paths.LEGACY_EMOJI_DIR.iterdir()),
    }


def is_placeholder(value: str | None) -> bool:
    """判断字符串是不是用户没改的模板占位符（如 '你的XX_API_Key'）。"""
    if not value:
        return True
    placeholders = ("你的", "your_", "YOUR_", "<", "xxx", "...")
    return value.startswith(placeholders)


def migrate_secrets(paths: AppPaths, secrets: SecretsManager) -> list[str]:
    """把 .env 中的密钥迁移到 secrets.enc。返回成功迁移的 ID 列表。"""
    if not paths.LEGACY_ENV_FILE.exists():
        return []

    env = dotenv_values(paths.LEGACY_ENV_FILE)
    migrated: list[str] = []

    for env_key, secret_id in LEGACY_SECRET_MAP.items():
        val = env.get(env_key)
        if not is_placeholder(val) and not secrets.has(secret_id):
            secrets.set(secret_id, val)  # type: ignore[arg-type]
            migrated.append(secret_id)
            logger.info(f"密钥已迁移: {env_key} → {secret_id}")

    return migrated


def build_config_from_legacy(
    paths: AppPaths, secrets: SecretsManager
) -> RootConfig:
    """读取旧 .env + config.yaml，构造新的 RootConfig。"""
    env = (
        dict(dotenv_values(paths.LEGACY_ENV_FILE))
        if paths.LEGACY_ENV_FILE.exists()
        else {}
    )
    old_yaml: dict[str, Any] = {}
    if paths.LEGACY_CONFIG_FILE.exists():
        with open(paths.LEGACY_CONFIG_FILE, "r", encoding="utf-8") as f:
            old_yaml = yaml.safe_load(f) or {}

    old_model = old_yaml.get("model", {})
    old_agent = old_yaml.get("agent", {})
    old_typing = old_yaml.get("typing", {})
    old_rate_limit = old_yaml.get("rate_limit", {})
    old_summarize = old_yaml.get("summarize", {})

    # NapCat 适配器（沿用旧 NoneBot 的 server 模式以保证迁移后行为不变）
    napcat_token_id: str | None = (
        "napcat_default_token" if secrets.has("napcat_default_token") else None
    )
    napcat_legacy_port = int(env.get("PORT", "8080") or "8080")
    napcat_host = env.get("HOST", "127.0.0.1") or "127.0.0.1"
    napcat = NapCatAdapterConfig(
        type="napcat",
        enabled=True,
        mode="server",  # 旧 NoneBot 默认：程序作为服务端等 NapCat 反向连入
        host=napcat_host,
        port=napcat_legacy_port,
        path="/onebot/v11/ws",  # 旧 NoneBot 默认 path
        access_token_id=napcat_token_id,
        whitelist=WhitelistConfig(mode="verify"),
    )
    logger.info(
        f"NapCat 配置沿用旧 NoneBot 行为：mode=server, "
        f"监听 ws://{napcat_host}:{napcat_legacy_port}/onebot/v11/ws。"
        f"如果你的 NapCat 现在配的是「正向 WS」（NapCat 监听），"
        f"请用 `python main.py --napcat` 改成 mode=client + 对应地址。"
    )

    # 主提供商：DeepSeek
    providers: dict[str, ProviderConfig] = {}
    if secrets.has("deepseek_main") or is_placeholder(env.get("DEEPSEEK_API_KEY")):
        providers["deepseek_main"] = ProviderConfig(
            preset="deepseek",
            display_name="DeepSeek",
            api_key_id="deepseek_main" if secrets.has("deepseek_main") else None,
        )

    # 视觉模型用的火山引擎
    vision_provider_id: str | None = None
    if secrets.has("volcengine_main"):
        providers["volcengine"] = ProviderConfig(
            preset="volcengine",
            display_name="火山方舟",
            api_key_id="volcengine_main",
        )
        vision_provider_id = "volcengine"

    # Agents（沿用旧 model.pro/flash 字段）
    chat_model = old_model.get("pro", "deepseek-chat")
    flash_model = old_model.get("flash", "deepseek-chat")
    temperature = float(old_model.get("temperature", 0.6))
    max_tokens = int(old_model.get("max_tokens", 16384))
    first_token_timeout = float(old_model.get("first_token_timeout", 30))
    max_loops = int(old_agent.get("max_loops", 15))

    agents = AgentsConfig(
        chat=AgentConfig(
            provider="deepseek_main",
            model=chat_model,
            temperature=temperature,
            max_tokens=max_tokens,
            first_token_timeout_seconds=first_token_timeout,
            max_loops=max_loops,
        ),
        proactive=AgentConfig(
            provider="deepseek_main",
            model=flash_model,
            temperature=0.3,
            max_tokens=64,
        ),
        summary=AgentConfig(
            provider="deepseek_main",
            model=flash_model,
            temperature=0.1,
            max_tokens=8192,
        ),
    )

    # Features
    qweather_host = env.get("QWEATHER_HOST", "") or ""
    if is_placeholder(qweather_host):
        qweather_host = ""

    features = FeaturesConfig(
        vision=VisionFeatureConfig(
            enabled=vision_provider_id is not None,
            type="api",
            provider=vision_provider_id,
            model=env.get("VOLCENGINE_VISION_MODEL", "doubao-seed-1-6-vision-250815")
            or "doubao-seed-1-6-vision-250815",
            api_key_id="volcengine_main" if secrets.has("volcengine_main") else None,
        ),
        weather=WeatherFeatureConfig(
            enabled=secrets.has("qweather") and bool(qweather_host),
            api_key_id="qweather" if secrets.has("qweather") else None,
            host=qweather_host,
        ),
        web_search=WebSearchFeatureConfig(enabled=True),
    )

    behavior = BehaviorConfig(
        merge_window_seconds=float(old_agent.get("merge_window", 0.5)),
        recall_merge_window_seconds=float(old_agent.get("recall_merge_window", 2.0)),
        proactive_think_interval_seconds=float(old_agent.get("greeting_interval", 600)),
        default_history_fetch_count=int(old_summarize.get("chat_history_count", 100)),
        typing=TypingConfig(
            chars_per_second=float(old_typing.get("chars_per_second", 1)),
            max_delay_seconds=float(old_typing.get("max_delay", 2.0)),
        ),
        rate_limit=RateLimitConfig(
            window_seconds=int(old_rate_limit.get("window", 60)),
            max_messages=int(old_rate_limit.get("max_messages", 5)),
        ),
        summarize=SummarizeConfig(
            trigger_at_messages=int(old_summarize.get("trigger_at", 200)),
            range_start_messages=int(old_summarize.get("range_start", 50)),
            range_end_messages=int(old_summarize.get("range_end", 150)),
        ),
    )

    return RootConfig(
        version=2,
        app=AppMeta(),
        adapters={"default": napcat},
        providers=providers,
        agents=agents,
        features=features,
        persona=PersonaConfig(active=old_yaml.get("persona", "diana")),
        behavior=behavior,
    )


def migrate_personas(paths: AppPaths) -> list[str]:
    """迁移外置人格目录到 PERSONAS_DIR。

    V2 设计下，PERSONAS_DIR 直接是 项目根/personas/，旧 LEGACY_PERSONAS_DIR
    也指向同一位置。所以原"复制"语义退化：源即目标，无需复制。

    保留此函数作为扩展点 —— 未来可能支持从其它路径（如 ~/.diana_agent/personas/）
    迁移到 PERSONAS_DIR。当前实现：扫描 PERSONAS_DIR 下已有的人格目录并返回名字
    列表，便于迁移报告统计"哪些人格已就位"。
    """
    moved: list[str] = []
    if not paths.PERSONAS_DIR.exists():
        return moved

    for d in paths.PERSONAS_DIR.iterdir():
        if not d.is_dir():
            continue
        if d.name.startswith("_") or d.name.startswith("."):
            continue
        if d.name == "__pycache__":
            continue
        if not (d / "persona_prompt.py").exists():
            continue
        moved.append(d.name)
        logger.debug(f"人格已就位: {d.name}")

    return moved


def migrate_emoji(paths: AppPaths) -> int:
    """复制 emoji/ → data/emoji/。返回迁移的文件数。"""
    if not paths.LEGACY_EMOJI_DIR.exists():
        return 0

    paths.EMOJI_DIR.mkdir(parents=True, exist_ok=True)
    count = 0
    for f in paths.LEGACY_EMOJI_DIR.iterdir():
        if not f.is_file():
            continue
        target = paths.EMOJI_DIR / f.name
        if target.exists():
            continue
        shutil.copy2(f, target)
        count += 1

    if count:
        logger.info(f"表情包已迁移 {count} 个到 {paths.EMOJI_DIR}")
    return count


def run_full_migration(
    paths: AppPaths, secrets: SecretsManager
) -> dict[str, Any]:
    """一站式迁移。返回迁移报告。

    不会覆盖任何已存在的新文件。
    """
    paths.ensure_data_dirs()
    secrets.initialize()

    report: dict[str, Any] = {
        "detected": detect_legacy(paths),
        "migrated_secrets": [],
        "migrated_personas": [],
        "migrated_emoji_count": 0,
        "config_created": False,
        "config_path": str(paths.CONFIG_FILE),
    }

    # 1. 密钥
    report["migrated_secrets"] = migrate_secrets(paths, secrets)

    # 2. 主配置（仅当新配置不存在时）
    if not paths.CONFIG_FILE.exists():
        cfg = build_config_from_legacy(paths, secrets)
        from .loader import save_config
        save_config(paths, cfg, backup=False)
        report["config_created"] = True

    # 3. 人格目录
    report["migrated_personas"] = migrate_personas(paths)

    # 4. 表情包
    report["migrated_emoji_count"] = migrate_emoji(paths)

    return report
