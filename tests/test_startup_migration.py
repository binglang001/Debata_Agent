from __future__ import annotations

import pytest


def test_initialize_runtime_data_migrates_legacy_config_to_external_data_root(tmp_path, monkeypatch):
    import app_config.paths as paths_module
    from app_config.paths import AppPaths
    from app_config.startup import initialize_runtime_data

    monkeypatch.delenv("DEBATA_DATA_ROOT", raising=False)
    project_root = tmp_path / "project"
    data_root = tmp_path / "system-data"
    old_data = project_root / "data"
    old_data.mkdir(parents=True)
    (project_root / "pyproject.toml").write_text('[project]\nversion = "0.9.0"\n', encoding="utf-8")
    (old_data / "config.yaml").write_text("app:\n  name: old\n", encoding="utf-8")
    monkeypatch.setattr(paths_module, "DEFAULT_PROJECT_ROOT", project_root)
    monkeypatch.setattr(paths_module, "user_data_dir", lambda *_args, **_kwargs: str(data_root))

    paths = AppPaths()
    first = initialize_runtime_data(paths)
    second = initialize_runtime_data(paths)

    assert first["migrated"] is True
    assert second["migrated"] is False
    assert second["skipped"] is True
    assert paths.ROOT_DATA_DIR == data_root
    assert paths.CONFIG_FILE.read_text(encoding="utf-8") == "app:\n  name: old\n"
    assert first["backup_path"] is not None
    assert second["backup_path"] == first["backup_path"]
    assert list((paths.ROOT_DATA_DIR / "backups").iterdir()) == [first["backup_path"]]


def test_main_no_gui_initializes_data_before_config_check(tmp_path, monkeypatch):
    import main as app_main

    calls: list[str] = []
    project_root = tmp_path / "project"
    old_data = project_root / "data"
    old_data.mkdir(parents=True)
    (project_root / "pyproject.toml").write_text('[project]\nversion = "0.9.0"\n', encoding="utf-8")
    (old_data / "config.yaml").write_text("app:\n  name: old\n", encoding="utf-8")
    data_root = tmp_path / "system-data"

    monkeypatch.setenv("DEBATA_DATA_ROOT", str(data_root))
    monkeypatch.setattr(app_main, "parse_args", lambda: app_main.argparse.Namespace(
        no_gui=True,
        config="",
        setup=False,
        napcat=False,
        test_adapter=False,
        list_secrets=False,
    ))
    monkeypatch.setattr(app_main, "setup_logging", lambda *args, **kwargs: calls.append("setup_logging"))
    monkeypatch.setattr(app_main, "install_uvloop", lambda: calls.append("install_uvloop"))

    def fake_wizard(_paths):
        raise AssertionError("迁移后的配置已存在，不应进入 CLI 向导")

    async def fake_run_headless(_project_root, config_file=None):
        calls.append("run_headless")
        assert config_file is None

    def record_initialize(paths, project_root=None):
        calls.append("initialize")
        from app_config.startup import initialize_runtime_data

        return initialize_runtime_data(paths, project_root=project_root)

    monkeypatch.setattr(app_main, "__file__", str(project_root / "main.py"))
    monkeypatch.setattr(app_main, "_run_cli_wizard", fake_wizard)
    monkeypatch.setattr(app_main, "run_headless", fake_run_headless)
    monkeypatch.setattr("app_config.initialize_runtime_data", record_initialize)

    app_main.main()

    assert calls == ["initialize", "setup_logging", "install_uvloop", "run_headless"]
    assert (data_root / "instances" / "default" / "config.yaml").is_file()


@pytest.mark.asyncio
async def test_runtime_start_initializes_data_before_loading_config(tmp_path, monkeypatch):
    from core.runtime import Runtime

    calls: list[str] = []
    project_root = tmp_path / "project"

    def fake_initialize(paths, project_root=None):
        calls.append("initialize")
        assert paths.PROJECT_ROOT == project_root
        assert project_root == tmp_path / "project"
        assert paths.CONFIG_FILE == project_root / "data" / "config.yaml"
        return {"migrated": False, "skipped": True}

    def fake_load_config(_paths):
        calls.append("load_config")
        raise RuntimeError("stop after load_config")

    class FakeSecretsManager:
        def __init__(self, _paths):
            pass

        def initialize(self):
            calls.append("secrets")

    monkeypatch.setattr("app_config.initialize_runtime_data", fake_initialize)
    monkeypatch.setattr("app_config.SecretsManager", FakeSecretsManager)
    monkeypatch.setattr("app_config.load_config", fake_load_config)

    rt = Runtime(project_root=project_root)
    with pytest.raises(RuntimeError, match="stop after load_config"):
        await rt.start()

    assert calls == ["initialize", "secrets", "load_config"]
