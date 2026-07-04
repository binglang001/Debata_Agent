"""版本登记中心测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
import tomllib

from app_config.versioning import (
    DEFAULT_SCHEMA_VERSIONS,
    DuplicateMigrationError,
    FutureSchemaVersionError,
    MissingMigrationError,
    VersionRegistry,
    create_default_registry,
    get_application_version,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def _noop(data):
    return data


def test_application_version_falls_back_to_pyproject_for_uninstalled_package():
    version = get_application_version(
        package_name="debata-agent-uninstalled-distribution-for-test",
        project_root=PROJECT_ROOT,
    )

    assert version == _pyproject_version()


def test_default_registry_registers_phase_0_schema_planes():
    registry = create_default_registry(application_version="test-version")

    assert registry.application_version == "test-version"
    assert registry.schema_versions == DEFAULT_SCHEMA_VERSIONS
    assert {
        "config",
        "root",
        "instance",
        "secrets",
        "debata_db",
        "persona_data",
    } <= set(registry.schema_versions)
    assert all(isinstance(version, int) for version in registry.schema_versions.values())


def test_plan_migration_returns_ordered_chain_for_jump_versions():
    registry = VersionRegistry(
        application_version="test-version",
        schema_versions={"config": 3},
    )
    step_1_to_2 = registry.register_migration("config", 1, 2, _noop)
    step_2_to_3 = registry.register_migration("config", 2, 3, _noop)

    plan = registry.plan_migration("config", 1, 3)

    assert plan == [step_1_to_2, step_2_to_3]
    assert [(step.from_version, step.to_version) for step in plan] == [(1, 2), (2, 3)]


def test_missing_migration_step_raises_clear_error():
    registry = VersionRegistry(
        application_version="test-version",
        schema_versions={"config": 3},
    )
    registry.register_migration("config", 1, 2, _noop)

    with pytest.raises(MissingMigrationError, match="config.*v1.*v3.*缺少"):
        registry.plan_migration("config", 1, 3)


def test_data_version_higher_than_current_raises_clear_error():
    registry = VersionRegistry(
        application_version="test-version",
        schema_versions={"config": 2},
    )

    with pytest.raises(FutureSchemaVersionError, match="config.*v3.*高于当前支持版本.*v2"):
        registry.ensure_supported_data_version("config", 3)


def test_duplicate_migration_registration_raises_clear_error():
    registry = VersionRegistry(
        application_version="test-version",
        schema_versions={"config": 2},
    )
    registry.register_migration("config", 1, 2, _noop)

    with pytest.raises(DuplicateMigrationError, match="config.*v1.*v2.*已注册"):
        registry.register_migration("config", 1, 2, _noop)
