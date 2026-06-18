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

import main_cli as _main_cli

logger = logging.getLogger(__name__)


def setup_logging(
    level: str = "INFO",
    *,
    project_root: Path | None = None,
    logs_dir: Path | None = None,
) -> None:
    """配置标准 logging。

    格式：[YYYY-MM-DD HH:MM:SS] [LEVEL] [module] message
    """
    root = project_root or Path(__file__).resolve().parent
    logs_dir = logs_dir or root / "data" / "logs"
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

    from app_config import AppPaths, SecretsManager, initialize_runtime_data
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
    initialize_runtime_data(paths, project_root=project_root)

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

    from app_config import AppPaths, initialize_runtime_data

    config_file = Path(args.config) if args.config else None
    paths = AppPaths(project_root=project_root, config_file=config_file)
    paths.ensure_data_dirs()
    initialize_runtime_data(paths, project_root=project_root)
    setup_logging("INFO", project_root=project_root, logs_dir=paths.LOGS_DIR)
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


def _sync_cli_io() -> None:
    _main_cli.getpass = getpass


def _cli_text(prompt: str, default: str | None = None) -> str:
    return _main_cli._cli_text(prompt, default)


def _cli_int(prompt: str, default: int) -> int:
    return _main_cli._cli_int(prompt, default)


def _cli_yes_no(prompt: str, default: bool = False) -> bool:
    return _main_cli._cli_yes_no(prompt, default)


def _cli_secret(prompt: str, *, has_existing: bool = False) -> str:
    _sync_cli_io()
    return _main_cli._cli_secret(prompt, has_existing=has_existing)


def _cli_choose(
    prompt: str,
    choices: list[tuple[str, str]],
    *,
    default: str,
) -> str:
    return _main_cli._cli_choose(prompt, choices, default=default)


def _cli_load_presets(paths) -> dict[str, Any]:
    return _main_cli._cli_load_presets(paths)


def _cli_default_model(
    presets: dict[str, Any],
    preset_id: str,
    fallback: str = "",
) -> str:
    return _main_cli._cli_default_model(presets, preset_id, fallback)


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
    _sync_cli_io()
    return _main_cli._cli_provider_config(
        paths=paths,
        secrets=secrets,
        cfg=cfg,
        provider_id=provider_id,
        current_provider=current_provider,
        current_model=current_model,
        title=title,
        default_preset=default_preset,
    )


def _cli_agent_config(
    provider_id: str,
    model: str,
    *,
    temperature: float,
    max_tokens: int,
):
    return _main_cli._cli_agent_config(
        provider_id,
        model,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def _cli_configure_napcat(secrets, current):
    _sync_cli_io()
    return _main_cli._cli_configure_napcat(secrets, current)


def _cli_configure_features(paths, secrets, cfg, main_provider_id: str) -> None:
    _sync_cli_io()
    _main_cli._cli_configure_features(paths, secrets, cfg, main_provider_id)


def _run_cli_wizard_legacy(paths) -> None:
    _sync_cli_io()
    _main_cli._run_cli_wizard_legacy(paths)


def _run_cli_wizard(paths) -> None:
    _sync_cli_io()
    _main_cli._run_cli_wizard(paths)


def _run_list_secrets(paths) -> None:
    _main_cli._run_list_secrets(paths)


def _run_napcat_setup(paths) -> None:
    _sync_cli_io()
    _main_cli._run_napcat_setup(paths)


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
