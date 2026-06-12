"""Debata_Agent 程序入口。

启动顺序：
    1. 解析命令行参数（--no-gui / --config 路径等）
    2. 配置 logging（按 config.app.log_level）
    3. 安装高性能事件循环（Linux/Mac 上用 uvloop）
    4. 检测配置是否就绪 → 否则启动配置向导
    5. 实例化 Runtime → 启动 → 等待 stop
    6. 如启用 GUI 则把 tray 跑在 qasync 桥接的事件循环里
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from getpass import getpass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def setup_logging(level: str = "INFO", *, project_root: Path | None = None) -> None:
    """配置标准 logging。

    格式：[YYYY-MM-DD HH:MM:SS] [LEVEL] [module] message
    """
    root = project_root or Path(__file__).resolve().parent
    logs_dir = root / "data" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    file_handler = RotatingFileHandler(
        logs_dir / "debata.log",
        maxBytes=20 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=[console, file_handler],
        force=True,
    )
    for noisy_logger in ("qasync", "websockets"):
        logging.getLogger(noisy_logger).setLevel(logging.INFO)


def install_uvloop() -> None:
    """在 Linux/Mac 上安装 uvloop（如已安装）。Windows 跳过。"""
    if sys.platform == "win32":
        return
    try:
        import uvloop  # type: ignore
        uvloop.install()
        logging.getLogger(__name__).info("已启用 uvloop")
    except ImportError:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="debata", description="Debata_Agent —— 让虚拟角色活过来的通用框架"
    )
    parser.add_argument(
        "--no-gui",
        action="store_true",
        help="纯命令行模式，不启动 PySide6 GUI",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="",
        help="自定义配置文件路径（默认在 data/config.yaml）",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="强制运行首次配置向导（即使已有 config.yaml）",
    )
    parser.add_argument(
        "--napcat",
        action="store_true",
        help="只重新配置 NapCat 适配器（mode/地址/端口/token），不动其它配置",
    )
    parser.add_argument(
        "--test-adapter",
        action="store_true",
        help="启动 NapCat adapter 5 秒，看能不能连上对端，然后退出",
    )
    parser.add_argument(
        "--list-secrets",
        action="store_true",
        help="列出 secrets.enc 中存储的所有密钥 ID（不显示值）",
    )
    return parser.parse_args()


async def run_headless(project_root: Path, config_file: Path | None = None) -> None:
    """无 GUI 模式：直接跑 Runtime。"""
    from core import Runtime

    rt = Runtime(project_root=project_root, config_file=config_file)
    try:
        await rt.start()
        await rt.wait_until_stop()
    finally:
        await rt.shutdown()


def _find_missing_secrets(cfg, secrets) -> list[str]:
    """检查 cfg 引用的所有 *_key_id / access_token_id 是否在 secrets 中存在。

    返回缺失项的人类可读描述列表（如 'providers.deepseek_main.api_key_id=deepseek_main'）。
    """
    missing: list[str] = []
    # providers
    for pid, p in (cfg.providers or {}).items():
        if p.api_key_id and secrets.get(p.api_key_id) is None:
            missing.append(f"providers.{pid}.api_key_id = '{p.api_key_id}'")
    # adapters
    for aid, a in (cfg.adapters or {}).items():
        tok_id = getattr(a, "access_token_id", None)
        if tok_id and secrets.get(tok_id) is None:
            missing.append(f"adapters.{aid}.access_token_id = '{tok_id}'")
    # features
    for fname in ("vision", "asr", "tts", "weather", "embedding"):
        feat = getattr(cfg.features, fname, None)
        if feat is None or not getattr(feat, "enabled", False):
            continue
        kid = getattr(feat, "api_key_id", None)
        if kid and secrets.get(kid) is None:
            missing.append(f"features.{fname}.api_key_id = '{kid}'")
    return missing


def run_with_gui(project_root: Path, force_wizard: bool = False, config_file: Path | None = None) -> None:
    """GUI 模式统一入口：qasync 桥接 + 向导 / 仪表盘 + 系统托盘 + graceful shutdown。

    - 没有 config 或 force_wizard=True：先弹向导，完成后接力启动 Runtime + Dashboard
    - 已有 config：直接启动 Runtime + Dashboard
    - Dashboard 在 Runtime.start() 完成后才创建（避免 page 访问空 runtime 崩溃）

    Windows 上 Ctrl+C 不能通过 add_signal_handler 捕获，
    用 signal.signal(SIGINT) 设个标志位，再用 QTimer 轮询。
    """
    import asyncio as _asyncio
    import signal as _signal
    from pathlib import Path

    import qasync  # type: ignore
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from app_config import AppPaths, SecretsManager
    from core import Runtime
    from ui.dashboard.main_window import DashboardWindow
    from ui.theme import cached_qss, palette_for_theme
    from ui.tray import Tray
    from ui.wizard.window import WizardWindow

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(cached_qss(palette_for_theme("auto")))

    _icon_path = Path(__file__).parent / "ui" / "icon.png"
    if _icon_path.exists():
        app.setWindowIcon(QIcon(str(_icon_path)))
        # Windows：让任务栏显示自定义图标而不是 python.exe 默认图标
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("debata.agent")

    loop = qasync.QEventLoop(app)
    _asyncio.set_event_loop(loop)

    paths = AppPaths(project_root=project_root, config_file=config_file)
    paths.ensure_data_dirs()

    state: dict = {
        "rt": None,
        "dashboard": None,
        "tray": None,
        "wizard": None,
        "shutting_down": False,
    }

    def begin_shutdown() -> None:
        if state["shutting_down"]:
            return
        state["shutting_down"] = True
        logger.info("准备退出 Debata...")
        rt = state["rt"]
        if rt is not None:
            try:
                rt.request_stop()
            except Exception:
                pass
        if state["tray"]:
            state["tray"].hide()
        if state["dashboard"]:
            state["dashboard"].close()
        if state["wizard"]:
            try:
                state["wizard"]._completed_emitted = True
            except Exception:
                pass
            state["wizard"].close()

        async def _do_shutdown() -> None:
            rt = state["rt"]
            if rt is not None:
                try:
                    await asyncio.wait_for(rt.shutdown(), timeout=20.0)
                except asyncio.TimeoutError:
                    logger.warning("Shutdown 超时，继续退出")
                except Exception:
                    logger.exception("Shutdown 失败")
            QTimer.singleShot(300, app.quit)

        loop.create_task(_do_shutdown())

    def start_dashboard() -> None:
        """Runtime + Dashboard + Tray 一站式启动。

        Runtime 启动成功后才创建 Dashboard，避免 page 访问空 runtime。
        """
        # 关掉向导窗口（如果还在）
        if state["wizard"] is not None:
            try:
                state["wizard"].close()
            except Exception as e:
                logger.warning(f"关闭向导窗口异常: {e}")
            state["wizard"] = None

        rt = Runtime(project_root=project_root, config_file=config_file)
        state["rt"] = rt

        async def _boot() -> None:
            try:
                await rt.start()
            except Exception as e:
                logger.exception("Runtime 启动失败")
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.critical(
                    None,
                    "未能启动",
                    f"Runtime 启动失败：\n{e}\n\n请检查配置或日志后重试。",
                )
                begin_shutdown()
                return

            # ---- Runtime 已就绪：现在才创建 Dashboard ----
            dashboard = DashboardWindow(rt)
            state["dashboard"] = dashboard
            dashboard.quit_requested.connect(begin_shutdown)

            def _open_dashboard() -> None:
                dashboard.show()
                dashboard.raise_()
                dashboard.activateWindow()

            def _on_restart() -> None:
                async def _restart_async() -> None:
                    ok = False
                    message = ""
                    try:
                        rt.request_stop()
                        await rt.shutdown()
                    except Exception:
                        logger.exception("重启时 shutdown 失败")
                        message = "停止 Runtime 时失败，请查看日志。"
                    try:
                        await rt.start()
                        ok = True
                    except Exception:
                        logger.exception("重启时 start 失败")
                        message = "启动 Runtime 时失败，请查看日志。"
                    try:
                        dashboard.notify_runtime_restart_finished(ok, message)
                    except Exception:
                        logger.exception("通知重启结果失败")
                loop.create_task(_restart_async())

            # 设置页 / 托盘都能触发重启
            dashboard.restart_requested.connect(_on_restart)

            tray = Tray(
                on_open=_open_dashboard,
                on_quit=begin_shutdown,
                on_restart=_on_restart,
                runtime_provider=lambda: state["rt"],
            )
            state["tray"] = tray
            tray.show()
            tray.showMessage(
                "Debata_Agent",
                "已在托盘待命。点托盘图标打开仪表盘。",
                msecs=2500,
            )
            _open_dashboard()

        loop.create_task(_boot())

    def _safe_initialize_secrets(sm: SecretsManager) -> bool:
        """initialize 失败（一般是换环境或 keyring 损坏）时弹窗让用户选择清空重做。

        返回 True 表示 sm 已 initialize 成功，可继续；False 表示用户选退出。
        """
        from app_config.secrets import SecretsError
        from ui.widgets import show_message
        try:
            sm.initialize()
            return True
        except SecretsError as e:
            logger.warning(f"密钥初始化失败：{e}")
            if show_message(
                None,
                "密钥解密失败",
                f"无法用当前 keyring 中的 RSA 私钥解开 AES 主密钥：\n\n{e}\n\n"
                "常见原因：换了机器 / 重装系统 / 更换 Windows 用户 / keyring 数据丢失。\n\n"
                "选择「清空重做」会删掉旧的 secrets 文件和 keyring 里的 RSA 私钥，"
                "重新生成一对全新的密钥并进入向导（你需要重新填 API key 等密钥）。"
                "yaml 配置不会被动到。",
                confirm_text="清空重做",
                cancel_text="退出",
                is_danger=True,
            ):
                sm.reset_all()
                try:
                    sm.initialize()
                    return True
                except Exception as e2:  # noqa: BLE001
                    show_message(
                        None, "重置后仍失败",
                        f"重置后再次 initialize 还是失败：{e2}\n\n请联系开发者。",
                        confirm_text="退出",
                    )
                    return False
            return False

    def _enter_wizard() -> None:
        secrets = SecretsManager(paths)
        if not _safe_initialize_secrets(secrets):
            begin_shutdown()
            return
        wizard = WizardWindow(paths, secrets)
        state["wizard"] = wizard
        wizard.completed.connect(start_dashboard)
        wizard.cancelled.connect(begin_shutdown)
        wizard.show()

    # ---- 决定走向导还是直接启动 ----
    if force_wizard or not paths.CONFIG_FILE.exists():
        _enter_wizard()
    else:
        # 有 config：再校验它引用的密钥是否齐全
        from app_config.loader import load_config
        from ui.widgets import show_message
        secrets_check = SecretsManager(paths)
        secrets_ok = _safe_initialize_secrets(secrets_check)
        if not secrets_ok:
            begin_shutdown()
            # 不 return：仍要进入下方代码 loop.run_forever，begin_shutdown 调度的 quit 才能跑到
            # 跳过 config / 启动检查；直接落到 Ctrl+C 设置 + loop.run_forever（很快被 quit 终结）
            cfg_check = None
        else:
            try:
                cfg_check = load_config(paths)
            except Exception as e:  # noqa: BLE001
                logger.exception("配置加载失败")
                if show_message(
                    None,
                    "配置文件读不动",
                    f"加载 {paths.CONFIG_FILE} 失败：{e}\n\n"
                    f"可以选择重跑向导覆盖现有配置，或退出手工修复。",
                    confirm_text="重新配置", cancel_text="退出",
                ):
                    _enter_wizard()
                else:
                    begin_shutdown()
            else:
                app.setStyleSheet(cached_qss(palette_for_theme(cfg_check.app.theme)))
                missing = _find_missing_secrets(cfg_check, secrets_check)
                if missing:
                    logger.warning(f"启动前检测：密钥缺失 {missing}")
                    if show_message(
                        None,
                        "密钥不齐",
                        "检测到以下配置项引用的密钥已丢失：\n\n  · "
                        + "\n  · ".join(missing)
                        + "\n\nDebata 不会以空密钥强行启动（这只会让 API 全报 401）。"
                        "\n\n请重新配置以覆盖现有 yaml 中的密钥字段。",
                        confirm_text="重新配置", cancel_text="退出",
                    ):
                        _enter_wizard()
                    else:
                        begin_shutdown()
                else:
                    start_dashboard()

    # ---- Ctrl+C ----
    stop_flag = {"value": False}

    def _sigint_handler(*_args) -> None:
        stop_flag["value"] = True
    _signal.signal(_signal.SIGINT, _sigint_handler)

    def _check_sigint() -> None:
        if stop_flag["value"]:
            stop_flag["value"] = False
            begin_shutdown()

    sigint_timer = QTimer()
    sigint_timer.setInterval(200)
    sigint_timer.timeout.connect(_check_sigint)
    sigint_timer.start()

    with loop:
        loop.run_forever()


# ============================================================
# main
# ============================================================


def main() -> None:
    """同步入口。"""
    args = parse_args()
    project_root = Path(__file__).resolve().parent

    from app_config import AppPaths

    config_file = Path(args.config) if args.config else None
    paths = AppPaths(project_root=project_root, config_file=config_file)
    paths.ensure_data_dirs()
    setup_logging("INFO", project_root=project_root)
    install_uvloop()

    if args.napcat:
        _run_napcat_setup(paths)
        return

    if args.test_adapter:
        asyncio.run(_test_adapter(project_root, config_file=config_file))
        return

    if args.list_secrets:
        _run_list_secrets(paths)
        return

    if args.no_gui:
        # CLI 模式
        if args.setup or not paths.CONFIG_FILE.exists():
            _run_cli_wizard(paths)
            return
        try:
            asyncio.run(run_headless(project_root, config_file=config_file))
        except KeyboardInterrupt:
            print("\n收到 Ctrl+C，已退出。")
    else:
        # GUI 模式：统一入口，内部判断是否要先弹向导
        run_with_gui(project_root, force_wizard=args.setup, config_file=config_file)


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


# ============================================================
# --test-adapter：启动 5 秒看连接情况
# ============================================================


async def _test_adapter(project_root: Path, config_file: Path | None = None) -> None:
    """启动 NapCat adapter 5 秒，报告连接情况。"""
    from core import Runtime

    print("=" * 60)
    print("测试 NapCat 适配器连接（5 秒）")
    print("=" * 60)

    rt = Runtime(project_root=project_root, config_file=config_file)
    try:
        await rt.start()
    except Exception as e:
        print(f"\n✗ Runtime 启动失败：{e}")
        return

    # 启动后立刻打印配置摘要，方便用户判断"程序在用什么配置"
    adapter_name, cfg = next(iter(rt.config.adapters.items()))
    endpoint = f"ws://{cfg.host}:{cfg.port}{cfg.path}"
    print("\n程序配置：")
    print(f"  adapter  = {adapter_name}")
    print(f"  mode     = {cfg.mode}")
    print(f"  endpoint = {endpoint}")
    print(f"  token_id = {cfg.access_token_id or '(无)'}")
    if cfg.mode == "client":
        print(f"  NapCat 这边应该: 「正向 WS」监听 {endpoint}")
    else:
        print(f"  程序监听地址: {endpoint}")
        print("  NapCat 这边应该: 「反向 WS」目标填这台机器的局域网 IP + 同端口/路径")

    print("\nAdapter 已启动。等 5 秒看连接情况...")
    for i in range(5):
        await asyncio.sleep(1)
        connected = rt.adapter.is_connected
        marker = "✓" if connected else "·"
        print(f"  [{i+1}/5] {marker} is_connected={connected}")
        if connected:
            print("\n✓ 连接成功！")
            break
    else:
        print("\n✗ 5 秒内未建立连接。检查：")
        adapter_name, cfg = next(iter(rt.config.adapters.items()))
        endpoint = f"ws://{cfg.host}:{cfg.port}{cfg.path}"
        if cfg.mode == "client":
            print(f"   - NapCat 那边「正向 WS」是否在 {endpoint} 监听？")
            print("   - Token 是否一致？")
        else:
            print("   - NapCat 那边「反向 WS」目标是否填了这台机器的局域网 IP？")
            print(f"   - 端口和路径是否与程序监听一致：{cfg.port}{cfg.path}？")
            print("   - Token 是否一致？")

    await rt.shutdown()
    print("=" * 60)


if __name__ == "__main__":
    main()
