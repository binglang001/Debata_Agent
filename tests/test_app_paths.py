from __future__ import annotations

from app_config.paths import AppPaths


def test_explicit_project_root_keeps_legacy_data_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("DEBATA_DATA_ROOT", raising=False)

    paths = AppPaths(project_root=tmp_path)

    assert paths.ROOT_DATA_DIR == tmp_path / "data"
    assert paths.INSTANCE_DIR == tmp_path / "data" / "instances" / "default"
    assert paths.DATA_DIR == tmp_path / "data"
    assert paths.CONFIG_FILE == tmp_path / "data" / "config.yaml"
    assert paths.VECTOR_DIR == tmp_path / "data" / "vector"
    assert paths.vector_dir_for("test_bot") == tmp_path / "data" / "vector" / "test_bot"


def test_explicit_data_root_uses_default_instance_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("DEBATA_DATA_ROOT", raising=False)
    data_root = tmp_path / "external-data"

    paths = AppPaths(project_root=tmp_path, data_root=data_root)

    assert paths.ROOT_DATA_DIR == data_root
    assert paths.INSTANCE_DIR == data_root / "instances" / "default"
    assert paths.DATA_DIR == paths.INSTANCE_DIR
    assert paths.MEMORY_DIR == paths.INSTANCE_DIR / "memory"
    assert paths.VECTOR_DIR == paths.INSTANCE_DIR / "vector"
    assert paths.vector_dir_for("test_bot") == paths.INSTANCE_DIR / "vector" / "test_bot"


def test_env_data_root_overrides_project_root(tmp_path, monkeypatch):
    env_data_root = tmp_path / "env-data"
    monkeypatch.setenv("DEBATA_DATA_ROOT", str(env_data_root))

    paths = AppPaths(project_root=tmp_path)

    assert paths.ROOT_DATA_DIR == env_data_root
    assert paths.INSTANCE_DIR == env_data_root / "instances" / "default"
    assert paths.DATA_DIR == paths.INSTANCE_DIR


def test_empty_dev_marker_uses_project_data_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("DEBATA_DATA_ROOT", raising=False)
    (tmp_path / ".debata-dev-data-root").write_text("", encoding="utf-8")

    paths = AppPaths(project_root=tmp_path)

    assert paths.ROOT_DATA_DIR == tmp_path / "data"
    assert paths.INSTANCE_DIR == tmp_path / "data" / "instances" / "default"
    assert paths.DATA_DIR == tmp_path / "data"


def test_relative_dev_marker_resolves_from_project_root(tmp_path, monkeypatch):
    monkeypatch.delenv("DEBATA_DATA_ROOT", raising=False)
    (tmp_path / ".debata-dev-data-root").write_text("var/debata", encoding="utf-8")

    paths = AppPaths(project_root=tmp_path)

    data_root = tmp_path / "var" / "debata"
    assert paths.ROOT_DATA_DIR == data_root
    assert paths.INSTANCE_DIR == data_root / "instances" / "default"
    assert paths.DATA_DIR == paths.INSTANCE_DIR


def test_instance_name_changes_instance_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("DEBATA_DATA_ROOT", raising=False)
    data_root = tmp_path / "external-data"

    paths = AppPaths(project_root=tmp_path, data_root=data_root, instance_name="second")

    assert paths.ROOT_DATA_DIR == data_root
    assert paths.INSTANCE_DIR == data_root / "instances" / "second"
    assert paths.DATA_DIR == paths.INSTANCE_DIR
    assert paths.LOGS_DIR == paths.INSTANCE_DIR / "logs"


def test_ensure_data_dirs_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.delenv("DEBATA_DATA_ROOT", raising=False)
    paths = AppPaths(project_root=tmp_path, data_root=tmp_path / "external-data")

    paths.ensure_data_dirs()
    paths.ensure_data_dirs()

    assert paths.DATA_DIR.is_dir()
    assert paths.MEMORY_DIR.is_dir()
    assert paths.VECTOR_DIR.is_dir()
    assert paths.LOGS_DIR.is_dir()
    assert paths.EMOJI_DIR.is_dir()
    assert paths.MODELS_DIR.is_dir()
    assert paths.WORKSPACE_DIR.is_dir()
