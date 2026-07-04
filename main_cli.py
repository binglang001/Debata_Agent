"""CLI 配置向导和命令行配置 helper。"""

from __future__ import annotations

import sys
from getpass import getpass
from typing import Any


def _cli_text(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    value = input(f"  {prompt}{suffix}: ").strip()
    return value if value else (default or "")


def _cli_int(prompt: str, default: int) -> int:
    while True:
        raw = _cli_text(prompt, str(default))
        try:
            return int(raw)
        except ValueError:
            print("  请输入整数。")


def _cli_yes_no(prompt: str, default: bool = False) -> bool:
    marker = "Y/n" if default else "y/N"
    while True:
        raw = input(f"  {prompt} [{marker}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes", "1", "true"}:
            return True
        if raw in {"n", "no", "0", "false"}:
            return False
        print("  请输入 y 或 n。")


def _cli_secret(prompt: str, *, has_existing: bool = False) -> str:
    reuse_hint = "（留空复用已保存；输入 clear 清除）" if has_existing else ""
    return getpass(f"  {prompt}{reuse_hint}: ").strip()


def _cli_choose(
    prompt: str,
    choices: list[tuple[str, str]],
    *,
    default: str,
) -> str:
    valid = {value for value, _label in choices}
    for idx, (value, label) in enumerate(choices, start=1):
        default_mark = "  *" if value == default else "   "
        print(f"{default_mark} {idx}. {label} ({value})")
    while True:
        raw = input(f"  {prompt} [{default}]: ").strip()
        if not raw:
            return default
        if raw.isdigit():
            index = int(raw) - 1
            if 0 <= index < len(choices):
                return choices[index][0]
        if raw in valid:
            return raw
        print("  请输入编号或括号里的 ID。")


def _cli_load_presets(paths) -> dict[str, Any]:
    from providers.presets_loader import load_all_presets

    return load_all_presets(paths.PROVIDER_PRESETS_DIR)


def _cli_default_model(
    presets: dict[str, Any],
    preset_id: str,
    fallback: str = "",
) -> str:
    preset = presets.get(preset_id)
    if preset and preset.models:
        return preset.models[0].id
    return fallback


def _cli_provider_config(
    *,
    paths,
    secrets,
    cfg,
    provider_id: str,
    current_provider,
    current_model: str,
    title: str,
    default_preset: str = "deepseek",
) -> tuple[str, str]:
    from app_config.schema import ProviderConfig

    presets = _cli_load_presets(paths)
    preset_choices = [
        (pid, str(preset.display_name))
        for pid, preset in sorted(presets.items())
    ]
    preset_choices.append(("custom", "自定义 OpenAI/Anthropic 兼容端点"))

    print(f"\n{title}")
    provider_id = _cli_text("Provider ID", provider_id) or provider_id
    cur_preset = getattr(current_provider, "preset", None) or default_preset
    if cur_preset not in presets:
        cur_preset = "custom" if current_provider else default_preset
    preset_id = _cli_choose("Provider 预设", preset_choices, default=cur_preset)

    if preset_id == "custom":
        protocol = _cli_choose(
            "协议",
            [("openai_compat", "OpenAI 兼容"), ("anthropic", "Anthropic")],
            default=getattr(current_provider, "protocol", None) or "openai_compat",
        )
        base_url = _cli_text(
            "Base URL",
            getattr(current_provider, "base_url", None) or "https://api.example.com/v1",
        )
        display_name = _cli_text(
            "显示名",
            getattr(current_provider, "display_name", None) or provider_id,
        )
        preset_value = None
    else:
        preset = presets[preset_id]
        protocol = None
        base_url = None
        display_name = preset.display_name
        preset_value = preset_id

    default_model = current_model or _cli_default_model(
        presets,
        preset_id,
        "deepseek-v4-flash",
    )
    model = _cli_text("模型 ID", default_model)

    default_key_id = getattr(current_provider, "api_key_id", None) or f"{provider_id}_key"
    key_id = _cli_text("密钥 ID（配置里引用这个 ID，不直接写 key）", default_key_id)
    has_existing = bool(key_id and secrets.has(key_id))
    api_key = _cli_secret("API Key", has_existing=has_existing)
    if api_key == "clear":
        if key_id:
            secrets.delete(key_id)
        api_key_id = None
    elif api_key:
        secrets.set(key_id, api_key)
        api_key_id = key_id
    elif has_existing:
        api_key_id = key_id
    else:
        keep = _cli_yes_no("暂时没有 key，仍保留这个密钥 ID？", False)
        api_key_id = key_id if keep else None

    cfg.providers[provider_id] = ProviderConfig(
        preset=preset_value,
        display_name=display_name,
        protocol=protocol,
        base_url=base_url,
        api_key_id=api_key_id,
    )
    return provider_id, model


def _cli_agent_config(
    provider_id: str,
    model: str,
    *,
    temperature: float,
    max_tokens: int,
):
    from app_config.schema import AgentConfig

    return AgentConfig(
        provider=provider_id,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def _cli_configure_napcat(secrets, current):
    from app_config.schema import NapCatAdapterConfig, WhitelistConfig

    print("\n[NapCat]")
    print("    1 = client：程序主动连接 NapCat 正向 WS（推荐）")
    print("    2 = server：程序监听，NapCat 反向 WS 连入")
    default_mode_choice = "1" if (current is None or current.mode == "client") else "2"
    ws_mode_input = _cli_text("选择", default_mode_choice)

    if ws_mode_input == "2":
        mode = "server"
        default_host = current.host if (current and current.mode == "server") else "0.0.0.0"
        default_port = current.port if current else 8080
        default_path = current.path if (current and current.mode == "server") else "/onebot/v11/ws"
        host_label = "程序监听地址"
    else:
        mode = "client"
        default_host = current.host if current else "127.0.0.1"
        default_port = current.port if current else 3001
        default_path = current.path if (current and current.mode == "client") else "/"
        host_label = "NapCat 地址"

    host = _cli_text(host_label, default_host)
    port = _cli_int("端口", int(default_port))
    path = _cli_text("WS 路径", default_path)

    current_token_id = getattr(current, "access_token_id", None) or "napcat_default_token"
    token_id = _cli_text("Token 密钥 ID", current_token_id)
    has_token = bool(token_id and secrets.has(token_id))
    token = _cli_secret("NapCat access token", has_existing=has_token)
    if token == "clear":
        secrets.delete(token_id)
        token_id = None
    elif token:
        secrets.set(token_id, token)
    elif not has_token:
        token_id = None

    return NapCatAdapterConfig(
        type="napcat",
        enabled=True,
        mode=mode,
        host=host,
        port=port,
        path=path,
        access_token_id=token_id,
        whitelist=current.whitelist if current else WhitelistConfig(mode="verify"),
    )


def _cli_configure_features(paths, secrets, cfg, main_provider_id: str) -> None:
    from app_config.schema import (
        EmbeddingFeatureConfig,
        LongTermMemoryConfig,
        TTSFeatureConfig,
        VisionFeatureConfig,
        WeatherFeatureConfig,
        WebSearchFeatureConfig,
    )

    features = cfg.features
    print("\n[可选功能]")
    features.web_search = WebSearchFeatureConfig(
        enabled=_cli_yes_no("启用网页搜索 web_search？", features.web_search.enabled),
        provider="ddg",
        max_results=features.web_search.max_results,
        timeout_seconds=features.web_search.timeout_seconds,
    )

    if _cli_yes_no("启用图片理解 describe_image？", features.vision.enabled):
        use_main = _cli_yes_no(
            "复用主模型 provider？",
            features.vision.provider in {None, main_provider_id},
        )
        if use_main:
            vision_provider = main_provider_id
        else:
            current_provider = cfg.providers.get(features.vision.provider or "vision_provider")
            vision_provider, _ = _cli_provider_config(
                paths=paths,
                secrets=secrets,
                cfg=cfg,
                provider_id=features.vision.provider or "vision_provider",
                current_provider=current_provider,
                current_model=features.vision.model,
                title="[图片理解 Provider]",
            )
        vision_model = _cli_text("图片理解模型 ID", features.vision.model or cfg.agents.chat.model)
        features.vision = VisionFeatureConfig(
            enabled=True,
            type="api",
            provider=vision_provider,
            model=vision_model,
        )
    else:
        features.vision = VisionFeatureConfig(enabled=False)

    if _cli_yes_no("启用天气 get_weather？", features.weather.enabled):
        weather_key_id = features.weather.api_key_id or "qweather_key"
        weather_key_id = _cli_text("和风天气密钥 ID", weather_key_id)
        has_weather = secrets.has(weather_key_id)
        weather_key = _cli_secret("和风天气 API Key", has_existing=has_weather)
        if weather_key == "clear":
            secrets.delete(weather_key_id)
            weather_key_id = None
        elif weather_key:
            secrets.set(weather_key_id, weather_key)
        elif not has_weather:
            print("  未提供天气 API Key，已关闭天气功能。")
            weather_key_id = None
        if weather_key_id:
            features.weather = WeatherFeatureConfig(
                enabled=True,
                api_key_id=weather_key_id,
                host=_cli_text("和风 API Host", features.weather.host),
            )
        else:
            features.weather = WeatherFeatureConfig(enabled=False)
    else:
        features.weather = WeatherFeatureConfig(enabled=False)

    if _cli_yes_no("启用语音回复 send_voice_message？", features.tts.enabled):
        tts_mode = _cli_choose(
            "TTS 类型",
            [("edge", "EdgeTTS API（无需密钥）"), ("xfyun", "讯飞 API"), ("local", "本地 VoxCPM2")],
            default=(
                "local"
                if features.tts.type == "local"
                else (features.tts.provider or "edge")
            ),
        )
        if tts_mode == "edge":
            features.tts = TTSFeatureConfig(
                enabled=True,
                type="api",
                provider="edge",
                extra_credentials={
                    "voice": _cli_text(
                        "EdgeTTS voice",
                        features.tts.extra_credentials.get("voice", "zh-CN-XiaoxiaoNeural"),
                    )
                },
            )
        elif tts_mode == "xfyun":
            key_id = features.tts.api_key_id or "tts_xfyun"
            key_id = _cli_text("讯飞 API Key 密钥 ID", key_id)
            has_key = secrets.has(key_id)
            api_key = _cli_secret("讯飞 API Key", has_existing=has_key)
            if api_key == "clear":
                secrets.delete(key_id)
                print("  未提供讯飞 API Key，已关闭 TTS。")
                features.tts = TTSFeatureConfig(enabled=False)
            elif api_key:
                secrets.set(key_id, api_key)
            elif not has_key:
                print("  未提供讯飞 API Key，已关闭 TTS。")
                features.tts = TTSFeatureConfig(enabled=False)
            else:
                app_id = _cli_text(
                    "讯飞 App ID",
                    features.tts.extra_credentials.get("app_id", ""),
                )
                api_secret = _cli_secret("讯飞 API Secret")
                extra = dict(features.tts.extra_credentials)
                extra["app_id"] = app_id
                if api_secret:
                    extra["api_secret"] = api_secret
                features.tts = TTSFeatureConfig(
                    enabled=True,
                    type="api",
                    provider="xfyun",
                    api_key_id=key_id,
                    extra_credentials=extra,
                )
        else:
            features.tts = TTSFeatureConfig(
                enabled=True,
                type="local",
                provider=None,
                model_dir=_cli_text(
                    "VoxCPM2 模型目录",
                    features.tts.model_dir or "data/models/voxcpm2",
                ),
                device=_cli_choose(
                    "设备",
                    [("auto", "自动"), ("cuda", "CUDA"), ("cpu", "CPU")],
                    default=features.tts.device,
                ),
            )
    else:
        features.tts = TTSFeatureConfig(enabled=False)

    rag_enabled = features.long_term_memory.mode == "rag" or features.embedding.enabled
    if _cli_yes_no("启用 RAG 历史召回？", rag_enabled):
        embedding_type = _cli_choose(
            "Embedding 类型",
            [("api", "API provider"), ("local", "本地 sentence-transformers")],
            default=features.embedding.type,
        )
        if embedding_type == "api":
            use_main = _cli_yes_no(
                "Embedding 复用主 provider？",
                features.embedding.provider in {None, main_provider_id},
            )
            if use_main:
                emb_provider = main_provider_id
            else:
                current_provider = cfg.providers.get(features.embedding.provider or "embedding_provider")
                emb_provider, _ = _cli_provider_config(
                    paths=paths,
                    secrets=secrets,
                    cfg=cfg,
                    provider_id=features.embedding.provider or "embedding_provider",
                    current_provider=current_provider,
                    current_model=features.embedding.api_model,
                    title="[Embedding Provider]",
                    default_preset="volcengine",
                )
            features.embedding = EmbeddingFeatureConfig(
                enabled=True,
                type="api",
                provider=emb_provider,
                api_model=_cli_text("Embedding 模型 ID", features.embedding.api_model),
            )
        else:
            features.embedding = EmbeddingFeatureConfig(
                enabled=True,
                type="local",
                local_quality=_cli_choose(
                    "本地 Embedding 质量",
                    [("performance", "高性能"), ("quality", "中文质量优先")],
                    default=features.embedding.local_quality,
                ),
                local_model_dir=_cli_text(
                    "本地 Embedding 模型目录",
                    features.embedding.local_model_dir
                    or "data/models/embedding/all-MiniLM-L6-v2",
                ),
            )
        features.long_term_memory = LongTermMemoryConfig(mode="rag")
    else:
        features.embedding = EmbeddingFeatureConfig(enabled=False)
        features.long_term_memory = LongTermMemoryConfig(mode="file")


def _run_cli_wizard_legacy(paths) -> None:
    """CLI 向导。两种模式自动切换：

    - 现有 config.yaml 可加载 → **amend 模式**：每项默认值是当前值，用户 Enter 保留。
    - 没有 config 或加载失败 → **fresh 模式**：用仓库默认值。

    密钥：secrets 里已有的不会重新询问，prompt 写"留空复用"。
    """
    from app_config import SecretsManager
    from app_config.loader import load_config, save_config
    from app_config.schema import (
        AgentConfig,
        AgentsConfig,
        BehaviorConfig,
        FeaturesConfig,
        LongTermMemoryConfig,
        NapCatAdapterConfig,
        PersonaConfig,
        ProviderConfig,
        RootConfig,
        WhitelistConfig,
    )

    # 尝试加载现有 config 作"amend 模式"的默认值
    existing: RootConfig | None = None
    if paths.CONFIG_FILE.exists():
        try:
            existing = load_config(paths, set_global=False)
        except Exception as e:
            print(f"⚠ 现有 config 加载失败（{e}）。将走全新配置流程。")
            existing = None

    cur_napcat = existing.adapters.get("default") if existing else None
    cur_persona = existing.persona.active if existing else "debata"

    print("=" * 60)
    if existing:
        print("Debata_Agent 配置向导 · amend 模式")
        print("（每项 Enter 保留当前值；只问需要的几项）")
    else:
        print("Debata_Agent 首次配置向导")
    print("=" * 60)

    secrets = SecretsManager(paths)
    secrets.initialize()
    has_deepseek = secrets.has("deepseek_main")
    has_napcat_token = secrets.has("napcat_default_token")

    # 1. DeepSeek
    print("\n[1/4] LLM 提供商配置")
    if existing and "deepseek_main" in existing.providers:
        print("  已有 DeepSeek provider 配置（model 等参数保留）。")
    else:
        print("  推荐使用 DeepSeek（注册：https://platform.deepseek.com）")
    if has_deepseek:
        api_key_prompt = "  粘贴新的 DeepSeek API Key（留空复用已保存的）: "
    else:
        api_key_prompt = "  粘贴你的 DeepSeek API Key: "
    api_key = getpass(api_key_prompt).strip()
    if not api_key and not has_deepseek:
        print("✗ 未提供 API Key 且 secrets 里也没有，向导退出。")
        sys.exit(1)

    # 2. NapCat 连接（现有值作默认）
    print("\n[2/4] NapCat 连接配置")
    print("  请对照 NapCat 那边的配置选择：")
    print("    1 = client：NapCat「正向 WS」（NapCat 监听等程序连入）→ 推荐")
    print("    2 = server：NapCat「反向 WS」（NapCat 主动连出到程序）")
    default_mode_choice = "1" if (cur_napcat is None or cur_napcat.mode == "client") else "2"
    ws_mode_input = input(f"  选择 [{default_mode_choice}]: ").strip() or default_mode_choice

    if ws_mode_input == "2":
        mode = "server"
        d_host = cur_napcat.host if (cur_napcat and cur_napcat.mode == "server") else "0.0.0.0"
        d_port = cur_napcat.port if cur_napcat else 8080
        d_path = cur_napcat.path if (cur_napcat and cur_napcat.mode == "server") else "/onebot/v11/ws"
    else:
        mode = "client"
        d_host = cur_napcat.host if cur_napcat else "127.0.0.1"
        d_port = cur_napcat.port if cur_napcat else 3001
        d_path = cur_napcat.path if (cur_napcat and cur_napcat.mode == "client") else "/"

    host_label = "程序监听地址" if mode == "server" else "NapCat 地址"
    host = input(f"  {host_label} [{d_host}]: ").strip() or d_host
    port = int(input(f"  端口 [{d_port}]: ").strip() or str(d_port))
    path = input(f"  WS 路径 [{d_path}]: ").strip() or d_path

    if has_napcat_token:
        napcat_token_prompt = "  粘贴新 access token（留空复用已存的；输入 'clear' 清除）: "
    else:
        napcat_token_prompt = "  NapCat access token（可留空）: "
    napcat_token = getpass(napcat_token_prompt).strip()

    # 3. 人格（默认 = 现有 active）
    print("\n[3/4] 人格选择")
    if existing:
        print(f"  当前激活：{cur_persona}")
    else:
        print("  仓库自带：debata（开箱即用）")
    persona_name = input(f"  人格目录名 [{cur_persona}]: ").strip() or cur_persona

    # 4. admin QQ（可选）
    print("\n[4/4] 管理员 QQ（用于好友/群验证通知，可选）")
    admin_qq = input("  你的 QQ 号（Enter 跳过）: ").strip()

    # ----- 写 secrets -----
    if api_key:
        secrets.set("deepseek_main", api_key)

    napcat_token_id: str | None
    if napcat_token == "clear":
        secrets.delete("napcat_default_token")
        napcat_token_id = None
    elif napcat_token:
        secrets.set("napcat_default_token", napcat_token)
        napcat_token_id = "napcat_default_token"
    elif has_napcat_token:
        napcat_token_id = "napcat_default_token"
    else:
        napcat_token_id = None

    # ----- 构造 RootConfig（amend 模式下基于现有，否则全新）-----
    new_napcat = NapCatAdapterConfig(
        type="napcat",
        enabled=True,
        mode=mode,
        host=host,
        port=port,
        path=path,
        access_token_id=napcat_token_id,
        whitelist=(cur_napcat.whitelist if cur_napcat else WhitelistConfig(mode="verify")),
    )

    if existing is not None:
        # amend：复用现有结构，只替换 NapCat / persona / 必要的 providers
        cfg = existing.model_copy(deep=True)
        cfg.adapters["default"] = new_napcat
        cfg.persona = PersonaConfig(active=persona_name)
        # 确保 deepseek_main provider 存在
        if "deepseek_main" not in cfg.providers:
            cfg.providers["deepseek_main"] = ProviderConfig(
                preset="deepseek",
                display_name="DeepSeek",
                api_key_id="deepseek_main",
            )
    else:
        # fresh
        cfg = RootConfig(
            version=2,
            adapters={"default": new_napcat},
            providers={
                "deepseek_main": ProviderConfig(
                    preset="deepseek",
                    display_name="DeepSeek",
                    api_key_id="deepseek_main",
                )
            },
            agents=AgentsConfig(
                chat=AgentConfig(
                    provider="deepseek_main",
                    model="deepseek-v4-flash",
                    temperature=0.6,
                    max_tokens=16384,
                ),
                proactive=AgentConfig(
                    provider="deepseek_main",
                    model="deepseek-v4-flash",
                    temperature=0.3,
                    max_tokens=64,
                ),
                summary=AgentConfig(
                    provider="deepseek_main",
                    model="deepseek-v4-flash",
                    temperature=0.1,
                    max_tokens=8192,
                ),
            ),
            features=FeaturesConfig(
                long_term_memory=LongTermMemoryConfig(mode="file"),
            ),
            persona=PersonaConfig(active=persona_name),
            behavior=BehaviorConfig(),
        )

    # 写入前预览，让用户确认（避免输错后又得手编 YAML）
    print("\n" + "-" * 60)
    print("即将写入以下配置：")
    print(f"  人格        : {persona_name}")
    print("  Provider    : DeepSeek（model=deepseek-v4-flash）")
    print(f"  Adapter mode: {mode}")
    print(f"  WS endpoint : ws://{host}:{port}{path}")
    if mode == "server":
        print("  NapCat 反向 WS 目标：填这台机器的局域网 IP，不要填 0.0.0.0 或 localhost")
    print(f"  Token       : {'(已绑定)' if napcat_token_id else '(无)'}")
    if admin_qq:
        print(f"  Admin QQ    : {admin_qq}（需手动添加到 persona_prompt.py）")
    print("-" * 60)
    confirm = input("确认写入？[Y/n]: ").strip().lower()
    if confirm in ("n", "no"):
        print("✗ 已取消，未写入。重跑 `python main.py --no-gui --setup` 可重新填。")
        sys.exit(0)

    save_config(paths, cfg)

    print("\n" + "=" * 60)
    print("✓ 配置已写入：")
    print(f"   {paths.CONFIG_FILE}")
    print(f"   密钥保存在 {paths.SECRETS_FILE}（已加密）")
    if admin_qq:
        print(f"\n注意：你提供的 admin QQ={admin_qq} 未写入 persona。")
        print(f"如需 admin 通知功能，请编辑 personas/{persona_name}/persona_prompt.py")
        print(f"在 PERSONA_VARS['admins'] 中追加 {{'name': '...', 'qq': '{admin_qq}'}}。")
    print("\n现在可以启动：python main.py --no-gui")
    print("=" * 60)


def _run_cli_wizard(paths) -> None:
    """Linux/SSH 友好的完整 CLI 配置向导。"""
    from app_config import SecretsManager
    from app_config.loader import load_config, save_config
    from app_config.schema import (
        AgentsConfig,
        BehaviorConfig,
        FeaturesConfig,
        PersonaConfig,
        ProviderConfig,
        RootConfig,
    )

    existing: RootConfig | None = None
    if paths.CONFIG_FILE.exists():
        try:
            existing = load_config(paths, set_global=False)
        except Exception as e:  # noqa: BLE001
            print(f"⚠ 现有 config 加载失败（{e}）。将走全新配置流程。")

    print("=" * 60)
    print("Debata_Agent CLI 配置向导")
    print("Enter 保留默认值；密钥不会写入 YAML，只保存到 secrets。")
    print("=" * 60)

    secrets = SecretsManager(paths)
    secrets.initialize()

    if existing is not None:
        cfg = existing.model_copy(deep=True)
    else:
        cfg = RootConfig(
            providers={
                "main_provider": ProviderConfig(
                    preset="deepseek",
                    display_name="DeepSeek",
                    api_key_id="main_provider_key",
                )
            },
            agents=AgentsConfig(
                chat=_cli_agent_config(
                    "main_provider",
                    "deepseek-v4-flash",
                    temperature=0.6,
                    max_tokens=16384,
                ),
                proactive=_cli_agent_config(
                    "main_provider",
                    "deepseek-v4-flash",
                    temperature=0.3,
                    max_tokens=64,
                ),
                summary=_cli_agent_config(
                    "main_provider",
                    "deepseek-v4-flash",
                    temperature=0.1,
                    max_tokens=8192,
                )
            ),
            features=FeaturesConfig(),
            behavior=BehaviorConfig(),
        )

    current_chat = cfg.agents.chat
    current_main_provider = cfg.providers.get(current_chat.provider)
    main_provider_id, main_model = _cli_provider_config(
        paths=paths,
        secrets=secrets,
        cfg=cfg,
        provider_id=current_chat.provider or "main_provider",
        current_provider=current_main_provider,
        current_model=current_chat.model,
        title="[主聊天模型]",
    )
    cfg.agents.chat = _cli_agent_config(
        main_provider_id,
        main_model,
        temperature=current_chat.temperature,
        max_tokens=current_chat.max_tokens,
    )

    print("\n[子 Agent]")
    if _cli_yes_no("启用主动思考 proactive？", cfg.agents.proactive is not None):
        current = cfg.agents.proactive
        proactive_model = _cli_text(
            "主动思考模型 ID",
            current.model if current else main_model,
        )
        cfg.agents.proactive = _cli_agent_config(
            main_provider_id,
            proactive_model,
            temperature=current.temperature if current else 0.3,
            max_tokens=current.max_tokens if current else 64,
        )
    else:
        cfg.agents.proactive = None

    if _cli_yes_no("启用滚动摘要 summary agent？", cfg.agents.summary is not None):
        current = cfg.agents.summary
        summary_model = _cli_text(
            "摘要模型 ID",
            current.model if current else main_model,
        )
        cfg.agents.summary = _cli_agent_config(
            main_provider_id,
            summary_model,
            temperature=current.temperature if current else 0.1,
            max_tokens=current.max_tokens if current else 8192,
        )
    else:
        cfg.agents.summary = None

    if _cli_yes_no("启用人格生成 persona_gen agent？", cfg.agents.persona_gen is not None):
        current = cfg.agents.persona_gen
        persona_model = _cli_text(
            "人格生成模型 ID",
            current.model if current else main_model,
        )
        cfg.agents.persona_gen = _cli_agent_config(
            main_provider_id,
            persona_model,
            temperature=current.temperature if current else 0.7,
            max_tokens=current.max_tokens if current else 8192,
        )
    else:
        cfg.agents.persona_gen = None

    _cli_configure_features(paths, secrets, cfg, main_provider_id)

    current_adapter = cfg.adapters.get("default")
    cfg.adapters["default"] = _cli_configure_napcat(secrets, current_adapter)

    print("\n[人格]")
    cfg.persona = PersonaConfig(
        active=_cli_text("人格目录名", cfg.persona.active or "debata")
    )
    admin_qq = _cli_text("管理员 QQ（可选，仅提示，不自动改 persona）", "")

    print("\n" + "-" * 60)
    print("即将写入：")
    print(f"  config      : {paths.CONFIG_FILE}")
    print(f"  persona     : {cfg.persona.active}")
    print(f"  chat        : {cfg.agents.chat.provider} / {cfg.agents.chat.model}")
    print(f"  proactive   : {'启用' if cfg.agents.proactive else '关闭'}")
    print(f"  summary     : {'启用' if cfg.agents.summary else '关闭'}")
    print(f"  web_search  : {'启用' if cfg.features.web_search.enabled else '关闭'}")
    print(f"  vision      : {'启用' if cfg.features.vision.enabled else '关闭'}")
    print(f"  weather     : {'启用' if cfg.features.weather.enabled else '关闭'}")
    print(f"  tts         : {'启用' if cfg.features.tts.enabled else '关闭'}")
    print(f"  memory      : {cfg.features.long_term_memory.mode}")
    napcat = cfg.adapters["default"]
    print(f"  napcat      : {napcat.mode} ws://{napcat.host}:{napcat.port}{napcat.path}")
    print("-" * 60)
    if not _cli_yes_no("确认写入？", True):
        print("已取消，未写入。")
        sys.exit(0)

    save_config(paths, cfg)
    print("\n✓ 配置已写入。")
    print(f"  配置文件：{paths.CONFIG_FILE}")
    print(f"  密钥库：  {paths.SECRETS_FILE}")
    if paths.RSA_PRIVATE_KEY_FILE.exists():
        print(f"  RSA 私钥：{paths.RSA_PRIVATE_KEY_FILE}（keyring 不可用时的本地兜底）")
    if admin_qq:
        print(f"  提醒：管理员 QQ={admin_qq} 需要手动写入 persona_prompt.py。")
    print("\n启动：python main.py --no-gui")


# ============================================================
# --list-secrets：列出所有密钥 ID
# ============================================================


def _run_list_secrets(paths) -> None:
    """列出 secrets.enc 中保存的所有密钥 ID（不显示值）。"""
    from app_config import SecretsManager

    secrets = SecretsManager(paths)
    secrets.initialize()
    ids = secrets.list_ids()
    if not ids:
        print("secrets 为空。")
    else:
        print(f"secrets 中已存储 {len(ids)} 条密钥：")
        for sid in ids:
            print(f"  - {sid}")


# ============================================================
# --napcat：只重新配置 NapCat 段
# ============================================================


def _run_napcat_setup(paths) -> None:
    """只重新配置 NapCat 适配器，不动其它配置。

    用于：用户改了 NapCat 那边的连接模式 / 端口 / token，想快速更新程序配置。
    """
    from app_config import SecretsManager
    from app_config.loader import load_config, save_config
    from app_config.schema import NapCatAdapterConfig, WhitelistConfig

    if not paths.CONFIG_FILE.exists():
        print(f"✗ 找不到 {paths.CONFIG_FILE}，请先跑 `python main.py --no-gui --setup`")
        sys.exit(1)

    secrets = SecretsManager(paths)
    secrets.initialize()
    has_napcat_token = secrets.has("napcat_default_token")

    cfg = load_config(paths)
    if "default" not in cfg.adapters:
        print("✗ 配置里没有 adapters.default 段。请用 --setup 重跑完整向导。")
        sys.exit(1)
    current = cfg.adapters["default"]

    print("=" * 60)
    print("NapCat 适配器配置")
    print("=" * 60)
    print(f"当前：mode={current.mode}, "
          f"endpoint=ws://{current.host}:{current.port}{current.path}, "
          f"token_id={current.access_token_id}")
    print()
    print("请对照你 NapCat 那边的配置选择：")
    print("    1 = client 模式：NapCat 配「正向 WS」（NapCat 监听等连入）→ 推荐")
    print("    2 = server 模式：NapCat 配「反向 WS」（NapCat 主动连出）")
    default_mode_choice = "1" if current.mode == "client" else "2"
    ws_mode_input = input(f"  选择 [{default_mode_choice}]: ").strip() or default_mode_choice

    if ws_mode_input == "2":
        mode = "server"
        default_path = current.path if current.mode == "server" else "/onebot/v11/ws"
        default_host = current.host if current.mode == "server" else "0.0.0.0"
        host = input(f"  程序监听地址 [{default_host}]: ").strip() or default_host
        port = int(input(f"  程序监听端口 [{current.port}]: ").strip() or str(current.port))
        path = input(f"  WS 路径 [{default_path}]: ").strip() or default_path
    else:
        mode = "client"
        default_path = current.path if current.mode == "client" else "/"
        host = input(f"  NapCat 地址 [{current.host}]: ").strip() or current.host
        port = int(input(f"  NapCat 端口 [{current.port}]: ").strip() or str(current.port))
        path = input(f"  WS 路径 [{default_path}]: ").strip() or default_path

    # Token
    if has_napcat_token:
        token_prompt = "  粘贴新 access token（留空复用 secrets 里已存的；输入 'clear' 清掉）: "
    else:
        token_prompt = "  access token（留空表示不用 token）: "
    new_token = getpass(token_prompt).strip()

    napcat_token_id: str | None
    if new_token == "clear":
        secrets.delete("napcat_default_token")
        napcat_token_id = None
    elif new_token:
        secrets.set("napcat_default_token", new_token)
        napcat_token_id = "napcat_default_token"
    elif has_napcat_token:
        napcat_token_id = "napcat_default_token"
    else:
        napcat_token_id = None

    # 写回 config
    new_napcat = NapCatAdapterConfig(
        type="napcat",
        enabled=True,
        mode=mode,
        host=host,
        port=port,
        path=path,
        access_token_id=napcat_token_id,
        whitelist=current.whitelist if current.whitelist else WhitelistConfig(),
    )
    cfg.adapters["default"] = new_napcat
    save_config(paths, cfg)

    endpoint = f"ws://{host}:{port}{path}"
    print()
    print("=" * 60)
    print("✓ NapCat 配置已更新。")
    if mode == "client":
        print(f"  程序将连接到: {endpoint}")
        print("  请确认 NapCat 那边「正向 WS」监听地址与此一致。")
    else:
        print(f"  程序将监听: {endpoint}")
        print("  跨设备时，NapCat 那边「反向 WS」目标地址要填这台机器的局域网 IP，不要填 0.0.0.0 或 localhost。")
    print(f"  Token: {'(已设置)' if napcat_token_id else '(无)'}")
    print()
    print("测试连接：python main.py --test-adapter")
    print("启动：    python main.py --no-gui")
    print("=" * 60)
