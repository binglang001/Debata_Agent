"""跨平台路径管理 —— 所有运行时路径的唯一来源。

阶段 0 路径契约：
    <数据根>/
        instances/
            <实例名>/
                config.yaml         <- 主配置文件
                secrets.enc         <- AES 加密后的 API 密钥库
                memory/{name}/      <- 各人格的对话历史
                logs/               <- 结构化日志
                emoji/              <- 表情包资源

开发兼容模式仍保留旧布局：
    项目根/
        data/                       <- DATA_DIR 继续指向这里
        personas/{name}/            <- 仓库自带 + 用户自创人格
        providers/presets/          <- 提供商预设（git 追踪）

RSA 私钥优先保存在系统密钥环（keyring）；Linux 无可用 keyring 后端时，
退回 data/rsa_private.pem 本地私钥文件。
"""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_dir

DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT_ENV_VAR = "DEBATA_DATA_ROOT"
DEV_DATA_ROOT_MARKER = ".debata-dev-data-root"
APP_DATA_DIR_NAME = "Debata_Agent"


class AppPaths:
    """所有路径的中心化管理。

    通过依赖注入传递给需要路径的模块，便于测试时替换为临时目录。
    """

    KEYRING_SERVICE: str = "Debata_Agent"
    KEYRING_RSA_PRIVATE_KEY: str = "rsa_private_key"

    def __init__(
        self,
        project_root: Path | None = None,
        *,
        config_file: Path | None = None,
        data_root: Path | None = None,
        instance_name: str = "default",
    ) -> None:
        explicit_project_root = project_root is not None
        if project_root is None:
            # app_config/paths.py → app_config/ → 项目根
            project_root = DEFAULT_PROJECT_ROOT
        else:
            project_root = Path(project_root)
        self.PROJECT_ROOT: Path = project_root
        self.instance_name: str = instance_name

        root_data_dir, legacy_data_dir = self._resolve_data_root(
            project_root=project_root,
            explicit_project_root=explicit_project_root,
            explicit_data_root=data_root,
            config_file=config_file,
        )
        self.ROOT_DATA_DIR: Path = root_data_dir
        self.data_root: Path = self.ROOT_DATA_DIR
        self.INSTANCE_DIR: Path = self.ROOT_DATA_DIR / "instances" / self.instance_name

        # === 运行时数据目录（gitignore） ===
        # 兼容模式下继续使用旧 data/；新数据根模式下 DATA_DIR 指向实例目录。
        self.DATA_DIR: Path = project_root / "data" if legacy_data_dir else self.INSTANCE_DIR
        self.CONFIG_FILE: Path = config_file if config_file is not None else self.DATA_DIR / "config.yaml"
        self.SECRETS_FILE: Path = self.DATA_DIR / "secrets.enc"
        self.SECRETS_META_FILE: Path = self.DATA_DIR / "secrets.meta"
        self.RSA_PUBLIC_KEY_FILE: Path = self.DATA_DIR / "rsa_public.pem"
        self.RSA_PRIVATE_KEY_FILE: Path = self.DATA_DIR / "rsa_private.pem"

        self.MEMORY_DIR: Path = self.DATA_DIR / "memory"
        self.LOGS_DIR: Path = self.DATA_DIR / "logs"
        self.EMOJI_DIR: Path = self.DATA_DIR / "emoji"
        self.MODELS_DIR: Path = self.DATA_DIR / "models"
        # AI 可读写的工作目录（替代旧 uploads/）。
        # 用户发送的文件 / 图片 / 语音会自动落地到这里；
        # AI 可通过 read/write/edit/list/delete/upload_file/run_python 工具操作其中文件。
        self.WORKSPACE_DIR: Path = self.DATA_DIR / "workspace"

        # === 仓库内 personas/ —— 所有人格平级（仓库自带 + 用户自创共存）===
        # 仓库自带（如 debata/）入 git，其它由 .gitignore 排除
        self.PERSONAS_DIR: Path = project_root / "personas"

        # === 仓库内置资源（git 追踪） ===
        self.PROVIDER_PRESETS_DIR: Path = project_root / "providers" / "presets"

    @classmethod
    def _resolve_data_root(
        cls,
        *,
        project_root: Path,
        explicit_project_root: bool,
        explicit_data_root: Path | None,
        config_file: Path | None,
    ) -> tuple[Path, bool]:
        """返回数据根与是否启用旧 DATA_DIR 兼容模式。"""
        if explicit_data_root is not None:
            return Path(explicit_data_root).expanduser(), False

        env_data_root = os.environ.get(DATA_ROOT_ENV_VAR)
        if env_data_root:
            return Path(env_data_root).expanduser(), False

        marker = project_root / DEV_DATA_ROOT_MARKER
        if marker.exists():
            marker_value = marker.read_text(encoding="utf-8").strip()
            if not marker_value:
                return project_root / "data", True

            marker_path = Path(marker_value).expanduser()
            if not marker_path.is_absolute():
                marker_path = project_root / marker_path
            return marker_path, False

        if config_file is not None:
            return project_root / "data", True

        if explicit_project_root and not cls._is_default_project_root(project_root):
            return project_root / "data", True

        return Path(user_data_dir(APP_DATA_DIR_NAME, appauthor=False)), False

    @staticmethod
    def _is_default_project_root(project_root: Path) -> bool:
        try:
            return project_root.resolve() == DEFAULT_PROJECT_ROOT.resolve()
        except OSError:
            return project_root.absolute() == DEFAULT_PROJECT_ROOT.absolute()

    def ensure_data_dirs(self) -> None:
        """确保所有 data 子目录存在。幂等。"""
        for d in (
            self.ROOT_DATA_DIR,
            self.INSTANCE_DIR,
            self.DATA_DIR,
            self.MEMORY_DIR,
            self.LOGS_DIR,
            self.EMOJI_DIR,
            self.MODELS_DIR,
            self.WORKSPACE_DIR,
        ):
            d.mkdir(parents=True, exist_ok=True)
        # PERSONAS_DIR 在仓库根目录，必然存在（init 时已 ensure）
        self.PERSONAS_DIR.mkdir(parents=True, exist_ok=True)

    def memory_dir_for(self, persona: str) -> Path:
        """获取指定人格的记忆目录。"""
        return self.MEMORY_DIR / persona

    def persona_dir_for(self, persona: str) -> Path:
        """获取指定人格的资源目录。所有人格平级在 PERSONAS_DIR 下。"""
        return self.PERSONAS_DIR / persona
