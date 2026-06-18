"""应用版本与 schema 版本登记中心。"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 仅兼容 Python 3.10 环境
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None  # type: ignore[assignment]


PACKAGE_NAME = "debata_agent"

# 阶段 0 只登记当前版本占位，不在这里实现后续阶段的具体迁移。
DEFAULT_SCHEMA_VERSIONS: dict[str, int] = {
    "config": 1,
    "root": 2,
    "instance": 1,
    "secrets": 1,
    "diana_db": 1,
    "persona_data": 1,
}

MigrationCallable = Callable[[Any], Any]


class VersioningError(RuntimeError):
    """版本登记中心的基础异常。"""


class VersionReadError(VersioningError):
    """无法读取应用版本。"""


class UnknownSchemaPlaneError(VersioningError):
    """访问了未登记的 schema 平面。"""


class DuplicateMigrationError(VersioningError):
    """重复登记同一个迁移步骤。"""


class MissingMigrationError(VersioningError):
    """无法规划完整迁移链。"""


class FutureSchemaVersionError(VersioningError):
    """数据版本高于当前应用支持的版本。"""


class InvalidMigrationError(VersioningError):
    """迁移步骤定义无效。"""


@dataclass(frozen=True, slots=True)
class MigrationStep:
    """单个 schema 迁移步骤。"""

    plane: str
    from_version: int
    to_version: int
    migrate: MigrationCallable


@dataclass(slots=True)
class VersionRegistry:
    """集中登记应用版本、各平面 schema 版本与迁移步骤。"""

    application_version: str
    schema_versions: dict[str, int] = field(default_factory=dict)
    migrations: dict[tuple[str, int, int], MigrationStep] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.schema_versions = dict(self.schema_versions)
        self.migrations = dict(self.migrations)
        for plane, version in self.schema_versions.items():
            self._validate_plane_name(plane)
            self._validate_version(version, "当前版本")
        for key, step in self.migrations.items():
            self._validate_migration_step(key, step)

    def current_version(self, plane: str) -> int:
        """返回指定平面的当前 schema 版本。"""

        try:
            return self.schema_versions[plane]
        except KeyError as exc:
            known = ", ".join(sorted(self.schema_versions)) or "无"
            raise UnknownSchemaPlaneError(
                f"未知 schema 平面：{plane!r}；已登记平面：{known}"
            ) from exc

    def ensure_supported_data_version(self, plane: str, data_version: int) -> None:
        """确认已存在数据没有使用未来版本。"""

        current = self.current_version(plane)
        self._validate_version(data_version, "数据版本")
        if data_version > current:
            raise FutureSchemaVersionError(
                f"{plane} 数据版本 v{data_version} 高于当前支持版本 v{current}，"
                "请升级应用后再打开。"
            )

    def register_migration(
        self,
        plane: str,
        from_version: int,
        to_version: int,
        migrate: MigrationCallable,
    ) -> MigrationStep:
        """登记一个 schema 迁移步骤。"""

        self.current_version(plane)
        self._validate_version(from_version, "起始版本")
        self._validate_version(to_version, "目标版本")
        if to_version <= from_version:
            raise InvalidMigrationError(
                f"{plane} 迁移步骤必须升序：v{from_version} -> v{to_version}"
            )
        if not callable(migrate):
            raise InvalidMigrationError(
                f"{plane} v{from_version} -> v{to_version} 的迁移处理器不可调用"
            )

        key = (plane, from_version, to_version)
        if key in self.migrations:
            raise DuplicateMigrationError(
                f"{plane} v{from_version} -> v{to_version} 迁移步骤已注册"
            )

        step = MigrationStep(
            plane=plane,
            from_version=from_version,
            to_version=to_version,
            migrate=migrate,
        )
        self.migrations[key] = step
        return step

    def lookup_migration(
        self,
        plane: str,
        from_version: int,
        to_version: int,
    ) -> MigrationStep:
        """查询已登记的单步迁移。"""

        self.current_version(plane)
        key = (plane, from_version, to_version)
        try:
            return self.migrations[key]
        except KeyError as exc:
            raise MissingMigrationError(
                f"缺少 {plane} v{from_version} -> v{to_version} 迁移步骤"
            ) from exc

    def plan_migration(
        self,
        plane: str,
        from_version: int,
        to_version: int | None = None,
    ) -> list[MigrationStep]:
        """规划从数据版本到目标版本的有序迁移链。"""

        current = self.current_version(plane)
        target = current if to_version is None else to_version
        self._validate_version(from_version, "起始版本")
        self._validate_version(target, "目标版本")
        if from_version > current:
            raise FutureSchemaVersionError(
                f"{plane} 数据版本 v{from_version} 高于当前支持版本 v{current}，"
                "请升级应用后再打开。"
            )
        if target > current:
            raise FutureSchemaVersionError(
                f"{plane} 目标版本 v{target} 高于当前支持版本 v{current}"
            )
        if from_version == target:
            return []
        if from_version > target:
            raise MissingMigrationError(
                f"{plane} 不支持从 v{from_version} 降级到 v{target}"
            )

        return self._find_migration_path(plane, from_version, target)

    def lookup(self, plane: str, from_version: int, to_version: int) -> MigrationStep:
        """lookup_migration 的简短别名。"""

        return self.lookup_migration(plane, from_version, to_version)

    def plan(
        self,
        plane: str,
        from_version: int,
        to_version: int | None = None,
    ) -> list[MigrationStep]:
        """plan_migration 的简短别名。"""

        return self.plan_migration(plane, from_version, to_version)

    def _find_migration_path(
        self,
        plane: str,
        from_version: int,
        target: int,
    ) -> list[MigrationStep]:
        queue: deque[tuple[int, list[MigrationStep]]] = deque([(from_version, [])])
        visited = {from_version}

        while queue:
            version, path = queue.popleft()
            for step in self._outgoing_steps(plane, version, target):
                next_path = [*path, step]
                if step.to_version == target:
                    return next_path
                if step.to_version not in visited:
                    visited.add(step.to_version)
                    queue.append((step.to_version, next_path))

        reachable = ", ".join(f"v{version}" for version in sorted(visited))
        raise MissingMigrationError(
            f"无法规划 {plane} 从 v{from_version} 到 v{target} 的迁移链；"
            f"缺少连续迁移步骤；已能到达：{reachable}"
        )

    def _outgoing_steps(
        self,
        plane: str,
        from_version: int,
        target: int,
    ) -> list[MigrationStep]:
        steps = [
            step
            for (step_plane, step_from, _), step in self.migrations.items()
            if step_plane == plane and step_from == from_version and step.to_version <= target
        ]
        return sorted(steps, key=lambda step: step.to_version)

    def _validate_migration_step(
        self,
        key: tuple[str, int, int],
        step: MigrationStep,
    ) -> None:
        expected = (step.plane, step.from_version, step.to_version)
        if key != expected:
            raise InvalidMigrationError(
                f"迁移注册表键 {key!r} 与迁移步骤 {expected!r} 不一致"
            )
        self.current_version(step.plane)
        self._validate_version(step.from_version, "起始版本")
        self._validate_version(step.to_version, "目标版本")
        if step.to_version <= step.from_version:
            raise InvalidMigrationError(
                f"{step.plane} 迁移步骤必须升序："
                f"v{step.from_version} -> v{step.to_version}"
            )
        if not callable(step.migrate):
            raise InvalidMigrationError(
                f"{step.plane} v{step.from_version} -> v{step.to_version} "
                "的迁移处理器不可调用"
            )

    @staticmethod
    def _validate_plane_name(plane: str) -> None:
        if not isinstance(plane, str) or not plane:
            raise UnknownSchemaPlaneError(f"schema 平面名称无效：{plane!r}")

    @staticmethod
    def _validate_version(version: int, label: str) -> None:
        if not isinstance(version, int) or version < 0:
            raise InvalidMigrationError(f"{label}必须是非负整数：{version!r}")


SchemaRegistry = VersionRegistry


def get_application_version(
    package_name: str = PACKAGE_NAME,
    project_root: Path | None = None,
) -> str:
    """读取运行时应用版本，源码树未安装时回退到 pyproject.toml。"""

    try:
        return importlib_metadata.version(package_name)
    except importlib_metadata.PackageNotFoundError:
        return read_pyproject_version(project_root)


def read_pyproject_version(project_root: Path | None = None) -> str:
    """读取源码树 pyproject.toml 中的 [project].version。"""

    root = _default_project_root() if project_root is None else Path(project_root)
    pyproject_path = root / "pyproject.toml"
    data = _read_toml(pyproject_path)
    try:
        version = data["project"]["version"]
    except (KeyError, TypeError) as exc:
        raise VersionReadError(
            f"{pyproject_path} 缺少 [project].version，无法确定应用版本"
        ) from exc
    if not isinstance(version, str) or not version:
        raise VersionReadError(
            f"{pyproject_path} 的 [project].version 必须是非空字符串"
        )
    return version


def create_default_registry(application_version: str | None = None) -> VersionRegistry:
    """创建阶段 0 默认版本登记中心。"""

    return VersionRegistry(
        application_version=application_version or get_application_version(),
        schema_versions=dict(DEFAULT_SCHEMA_VERSIONS),
    )


def _read_toml(path: Path) -> Mapping[str, Any]:
    if tomllib is None:
        raise VersionReadError("当前 Python 环境缺少 TOML 解析器，无法读取 pyproject.toml")
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except FileNotFoundError as exc:
        raise VersionReadError(f"未找到 {path}，无法确定应用版本") from exc
    except tomllib.TOMLDecodeError as exc:
        raise VersionReadError(f"{path} 不是有效 TOML，无法确定应用版本") from exc


def _default_project_root() -> Path:
    return Path(__file__).resolve().parent.parent

