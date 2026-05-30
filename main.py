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
from pathlib import Path

logger = logging.getLogger(__name__)


def setup_logging(level: str = "INFO") -> None:
    """配置标准 logging。

    格式：[YYYY-MM-DD HH:MM:SS] [LEVEL] [module] message
    """
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


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

    import qasync  # type: ignore
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from app_config import AppPaths, SecretsManager
    from ui.theme import build_qss, palette_for_theme
    from ui.dashboard.main_window import DashboardWindow
    from ui.tray import Tray
    from ui.wizard.window import WizardWindow
    from core import Runtime

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(build_qss(palette_for_theme("auto")))

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
                app.setStyleSheet(build_qss(palette_for_theme(cfg_check.app.theme)))
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

    setup_logging("INFO")
    install_uvloop()

    from app_config import AppPaths

    config_file = Path(args.config) if args.config else None
    paths = AppPaths(project_root=project_root, config_file=config_file)
    paths.ensure_data_dirs()

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


def _run_cli_wizard(paths) -> None:
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
        d_host = cur_napcat.host if cur_napcat else "127.0.0.1"
        d_port = cur_napcat.port if cur_napcat else 8080
        d_path = cur_napcat.path if (cur_napcat and cur_napcat.mode == "server") else "/onebot/v11/ws"
    else:
        mode = "client"
        d_host = cur_napcat.host if cur_napcat else "127.0.0.1"
        d_port = cur_napcat.port if cur_napcat else 3001
        d_path = cur_napcat.path if (cur_napcat and cur_napcat.mode == "client") else "/"

    host = input(f"  地址 [{d_host}]: ").strip() or d_host
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
                long_term_memory=LongTermMemoryConfig(mode="file", keyword_trigger_save=True),
            ),
            persona=PersonaConfig(active=persona_name),
            behavior=BehaviorConfig(),
        )

    # 写入前预览，让用户确认（避免输错后又得手编 YAML）
    print("\n" + "-" * 60)
    print("即将写入以下配置：")
    print(f"  人格        : {persona_name}")
    print(f"  Provider    : DeepSeek（model=deepseek-v4-flash）")
    print(f"  Adapter mode: {mode}")
    print(f"  WS endpoint : ws://{host}:{port}{path}")
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
        host = input(f"  程序监听地址 [{current.host}]: ").strip() or current.host
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
        print("  请确认 NapCat 那边「反向 WS」目标地址与此一致。")
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
    print(f"\n程序配置：")
    print(f"  adapter  = {adapter_name}")
    print(f"  mode     = {cfg.mode}")
    print(f"  endpoint = {endpoint}")
    print(f"  token_id = {cfg.access_token_id or '(无)'}")
    print(f"  NapCat 这边应该: "
          f"{'「正向 WS」监听 ' + endpoint if cfg.mode == 'client' else '「反向 WS」目标 = ' + endpoint}")

    print(f"\nAdapter 已启动。等 5 秒看连接情况...")
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
            print(f"   - NapCat 那边「反向 WS」目标是否填了 {endpoint}？")
            print("   - Token 是否一致？")

    await rt.shutdown()
    print("=" * 60)


if __name__ == "__main__":
    main()
