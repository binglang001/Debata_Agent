"""Diana_Agent 程序入口。

启动顺序：
    1. 解析命令行参数（--no-gui / --config 路径等）
    2. 配置 logging（按 config.app.log_level）
    3. 安装高性能事件循环（Linux/Mac 上用 uvloop）
    4. 检测旧配置存在 → 提示用 --migrate
    5. 检测配置是否就绪 → 否则启动配置向导
    6. 实例化 Runtime → 启动 → 等待 stop
    7. 如启用 GUI 则把 tray 跑在 qasync 桥接的事件循环里
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
        prog="diana", description="Diana_Agent —— 让虚拟角色活过来的通用框架"
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
        "--migrate",
        action="store_true",
        help="把旧的 .env + config.yaml 迁移到 V2 加密配置后退出",
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


async def run_headless(project_root: Path) -> None:
    """无 GUI 模式：直接跑 Runtime。"""
    from core import Runtime

    rt = Runtime(project_root=project_root)
    try:
        await rt.start()
        await rt.wait_until_stop()
    finally:
        await rt.shutdown()


def run_with_gui(project_root: Path) -> None:
    """GUI 模式（Phase 2 实装）。"""
    raise NotImplementedError(
        "GUI 模式将在 Phase 2 实现。请用 --no-gui 启动 headless 模式。"
    )


# ============================================================
# main
# ============================================================


def main() -> None:
    """同步入口。"""
    args = parse_args()
    project_root = Path(__file__).resolve().parent

    setup_logging("INFO")
    install_uvloop()

    from app_config import AppPaths, detect_legacy

    paths = AppPaths(project_root=project_root)
    paths.ensure_data_dirs()

    if args.migrate:
        _run_migration(paths)
        return

    if args.napcat:
        _run_napcat_setup(paths)
        return

    if args.test_adapter:
        asyncio.run(_test_adapter(project_root))
        return

    if args.list_secrets:
        _run_list_secrets(paths)
        return

    # 检测旧 V1 配置
    legacy = detect_legacy(paths)
    if any(legacy.values()) and not paths.CONFIG_FILE.exists():
        print("检测到旧版配置（.env / config.yaml）。")
        print("请运行 `python main.py --migrate` 一站式迁移到 V2 后再启动。")
        sys.exit(2)

    # 检测 V2 配置就绪
    if args.setup or not paths.CONFIG_FILE.exists():
        if args.no_gui:
            _run_cli_wizard(paths)
        else:
            _run_gui_wizard(paths, no_gui=False)
        return

    if args.no_gui:
        try:
            asyncio.run(run_headless(project_root))
        except KeyboardInterrupt:
            # Windows 上 SIGINT 不能通过 add_signal_handler 捕获，
            # 会一路抛到 asyncio.run 外面。清理已在 run_headless 的 finally 跑完。
            print("\n收到 Ctrl+C，已退出。")
    else:
        run_with_gui(project_root)


def _run_migration(paths) -> None:
    """运行迁移：旧 .env + config.yaml → V2 加密配置。"""
    from app_config import SecretsManager, run_full_migration

    secrets = SecretsManager(paths)
    secrets.initialize()
    report = run_full_migration(paths, secrets)

    print("=" * 60)
    print("迁移完成。")
    print(f"  配置文件: {report['config_path']}（创建: {report['config_created']}）")
    print(f"  迁移的密钥: {report['migrated_secrets']}")
    print(f"  人格目录（仓库内 / 用户自创共存）: {report['migrated_personas']}")
    print(f"  迁移的表情包数: {report['migrated_emoji_count']}")
    print("=" * 60)
    print("下一步：")
    print("  1) 检查 NapCat 配置:   python main.py --napcat   （只改 NapCat 段）")
    print("  2) 测试连接:           python main.py --test-adapter")
    print("  3) 真启动:             python main.py --no-gui")
    print("")
    print("提示：旧 .env / 旧 config.yaml 未删除，确认运行正常后再清理。")


def _run_gui_wizard(paths, no_gui: bool) -> None:
    """启动 GUI 配置向导（Phase 2 实装）。"""
    raise NotImplementedError(
        "GUI 配置向导将在 Phase 2 实现。请用 `python main.py --no-gui --setup` 走 CLI 向导。"
    )


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
    cur_persona = existing.persona.active if existing else "diana"

    print("=" * 60)
    if existing:
        print("Diana_Agent 配置向导 · amend 模式")
        print("（每项 Enter 保留当前值；只问需要的几项）")
    else:
        print("Diana_Agent 首次配置向导")
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
        print("  仓库自带：diana（开箱即用）")
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
                    model="deepseek-chat",
                    temperature=0.6,
                    max_tokens=16384,
                ),
                proactive=AgentConfig(
                    provider="deepseek_main",
                    model="deepseek-chat",
                    temperature=0.3,
                    max_tokens=64,
                ),
                summary=AgentConfig(
                    provider="deepseek_main",
                    model="deepseek-chat",
                    temperature=0.1,
                    max_tokens=8192,
                ),
            ),
            features=FeaturesConfig(
                long_term_memory=LongTermMemoryConfig(mode="file", keyword_force_save=True),
            ),
            persona=PersonaConfig(active=persona_name),
            behavior=BehaviorConfig(),
        )

    # 写入前预览，让用户确认（避免输错后又得手编 YAML）
    print("\n" + "-" * 60)
    print("即将写入以下配置：")
    print(f"  人格        : {persona_name}")
    print(f"  Provider    : DeepSeek（model=deepseek-chat）")
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


async def _test_adapter(project_root: Path) -> None:
    """启动 NapCat adapter 5 秒，报告连接情况。"""
    from core import Runtime

    print("=" * 60)
    print("测试 NapCat 适配器连接（5 秒）")
    print("=" * 60)

    rt = Runtime(project_root=project_root)
    try:
        await rt.start()
    except Exception as e:
        print(f"\n✗ Runtime 启动失败：{e}")
        return

    # 启动后立刻打印配置摘要，方便用户判断"程序在用什么配置"
    cfg = rt.config.adapters["default"]
    endpoint = f"ws://{cfg.host}:{cfg.port}{cfg.path}"
    print(f"\n程序配置：")
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
        cfg = rt.config.adapters["default"]
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
