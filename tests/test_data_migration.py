from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import app_config.data_migration as data_migration
from app_config.data_migration import ensure_data_root_initialized


def fixed_now() -> datetime:
    return datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def make_paths(project_root: Path, data_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(PROJECT_ROOT=project_root, DATA_DIR=data_dir)


def test_migrates_old_data_to_target_instance_and_creates_backup(tmp_path):
    project_root = tmp_path / "project"
    old_data = project_root / "data"
    target_instance = tmp_path / "data-root" / "instances" / "default"
    (old_data / "memory" / "debata").mkdir(parents=True)
    (old_data / "memory" / "debata" / "history.jsonl").write_text("old history", encoding="utf-8")
    (old_data / "config.yaml").write_text("name: old\n", encoding="utf-8")

    report = ensure_data_root_initialized(make_paths(project_root, target_instance), "0.9.0", now=fixed_now)

    assert report["migrated"] is True
    assert report["skipped"] is False
    assert report["source_data_dir"] == old_data
    assert report["target_instance_dir"] == target_instance
    assert (tmp_path / "data-root" / "root.json").is_file()
    assert (target_instance / "instance.json").is_file()
    assert (target_instance / "memory" / "debata" / "history.jsonl").read_text(encoding="utf-8") == "old history"
    assert (target_instance / "config.yaml").read_text(encoding="utf-8") == "name: old\n"
    assert old_data.is_dir()
    assert (old_data / "config.yaml").is_file()

    backup_path = report["backup_path"]
    assert backup_path == tmp_path / "data-root" / "backups" / "data-20260102T030405Z"
    assert backup_path.is_dir()
    assert (backup_path / "memory" / "debata" / "history.jsonl").read_text(encoding="utf-8") == "old history"


def test_second_run_is_idempotent(tmp_path):
    project_root = tmp_path / "project"
    old_data = project_root / "data"
    target_instance = tmp_path / "data-root" / "instances" / "default"
    old_data.mkdir(parents=True)
    (old_data / "config.yaml").write_text("name: old\n", encoding="utf-8")
    paths = make_paths(project_root, target_instance)

    first = ensure_data_root_initialized(paths, "0.9.0", now=fixed_now)
    second = ensure_data_root_initialized(paths, "0.9.0", now=fixed_now)

    assert first["migrated"] is True
    assert second["migrated"] is False
    assert second["skipped"] is True
    assert second["backup_path"] == first["backup_path"]
    backups = list((tmp_path / "data-root" / "backups").iterdir())
    assert backups == [first["backup_path"]]
    assert (target_instance / "config.yaml").read_text(encoding="utf-8") == "name: old\n"


def test_existing_target_file_is_not_overwritten(tmp_path):
    project_root = tmp_path / "project"
    old_data = project_root / "data"
    target_instance = tmp_path / "data-root" / "instances" / "default"
    old_data.mkdir(parents=True)
    target_instance.mkdir(parents=True)
    (old_data / "config.yaml").write_text("name: old\n", encoding="utf-8")
    (old_data / "secrets.meta").write_text("old secret", encoding="utf-8")
    (target_instance / "config.yaml").write_text("name: target\n", encoding="utf-8")

    report = ensure_data_root_initialized(make_paths(project_root, target_instance), "0.9.0", now=fixed_now)

    assert report["migrated"] is True
    assert report["skipped_paths"] == ("config.yaml",)
    assert (target_instance / "config.yaml").read_text(encoding="utf-8") == "name: target\n"
    assert (target_instance / "secrets.meta").read_text(encoding="utf-8") == "old secret"


def test_old_data_dir_only_initializes_manifests_when_data_dir_is_unchanged(tmp_path):
    project_root = tmp_path / "project"
    old_data = project_root / "data"
    old_data.mkdir(parents=True)
    (old_data / "config.yaml").write_text("name: old\n", encoding="utf-8")

    report = ensure_data_root_initialized(make_paths(project_root, old_data), "0.9.0", now=fixed_now)

    assert report["migrated"] is False
    assert report["skipped"] is True
    assert report["backup_path"] is None
    assert (old_data / "root.json").is_file()
    assert (old_data / "instance.json").is_file()
    assert not (old_data / "backups").exists()
    assert (old_data / "config.yaml").read_text(encoding="utf-8") == "name: old\n"


def test_second_instance_upserts_root_instances_and_keeps_default_migration(tmp_path):
    project_root = tmp_path / "project"
    old_data = project_root / "data"
    default_instance = tmp_path / "data-root" / "instances" / "default"
    second_instance = tmp_path / "data-root" / "instances" / "second"
    old_data.mkdir(parents=True)
    (old_data / "config.yaml").write_text("name: old\n", encoding="utf-8")

    ensure_data_root_initialized(make_paths(project_root, default_instance), "0.9.0", now=fixed_now)
    ensure_data_root_initialized(make_paths(project_root, second_instance), "0.9.0", now=fixed_now)

    root_manifest = json.loads((tmp_path / "data-root" / "root.json").read_text(encoding="utf-8"))
    instances = {item["name"]: item["path"] for item in root_manifest["instances"]}
    assert instances == {
        "default": str(default_instance),
        "second": str(second_instance),
    }

    migration_entry = root_manifest["migrations"][data_migration.MIGRATION_ID]
    assert set(migration_entry) == {"default", "second"}
    assert migration_entry["default"]["target_instance_dir"] == str(default_instance)
    assert migration_entry["second"]["target_instance_dir"] == str(second_instance)


def test_legacy_single_root_migration_record_is_compatible(tmp_path):
    project_root = tmp_path / "project"
    old_data = project_root / "data"
    default_instance = tmp_path / "data-root" / "instances" / "default"
    second_instance = tmp_path / "data-root" / "instances" / "second"
    old_data.mkdir(parents=True)
    (old_data / "config.yaml").write_text("name: old\n", encoding="utf-8")
    root_json = tmp_path / "data-root" / "root.json"
    default_record = {
        "app_version": "0.9.0",
        "completed_at": "20260102T030405Z",
        "source_data_dir": str(old_data),
        "target_instance_dir": str(default_instance),
        "backup_path": str(tmp_path / "data-root" / "backups" / "data-20260102T030405Z"),
        "skipped_paths": [],
    }
    root_json.parent.mkdir(parents=True)
    root_json.write_text(
        json.dumps(
            {
                "schema": "debata.data_root.v0",
                "app_version": "0.9.0",
                "created_at": "20260102T030405Z",
                "instances": [{"name": "default", "path": str(default_instance)}],
                "migrations": {data_migration.MIGRATION_ID: default_record},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    skipped = ensure_data_root_initialized(make_paths(project_root, default_instance), "0.9.0", now=fixed_now)
    migrated = ensure_data_root_initialized(make_paths(project_root, second_instance), "0.9.0", now=fixed_now)

    assert skipped["migrated"] is False
    assert migrated["migrated"] is True
    root_manifest = json.loads(root_json.read_text(encoding="utf-8"))
    migration_entry = root_manifest["migrations"][data_migration.MIGRATION_ID]
    assert set(migration_entry) == {"default", "second"}
    assert migration_entry["default"]["target_instance_dir"] == str(default_instance)
    assert migration_entry["second"]["target_instance_dir"] == str(second_instance)


def test_copy_uses_no_clobber_when_target_appears_during_copy(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    old_data = project_root / "data"
    target_instance = tmp_path / "data-root" / "instances" / "default"
    old_data.mkdir(parents=True)
    (old_data / "race.txt").write_text("source payload", encoding="utf-8")
    race_target = target_instance / "race.txt"
    real_open = os.open

    def create_target_before_exclusive_open(path, flags, mode=0o777, *, dir_fd=None):
        path_obj = Path(path)
        if path_obj == race_target and flags & os.O_EXCL:
            path_obj.parent.mkdir(parents=True, exist_ok=True)
            path_obj.write_text("concurrent winner", encoding="utf-8")
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(data_migration.os, "open", create_target_before_exclusive_open)

    report = ensure_data_root_initialized(make_paths(project_root, target_instance), "0.9.0", now=fixed_now)

    assert report["migrated"] is True
    assert report["skipped_paths"] == ("race.txt",)
    assert race_target.read_text(encoding="utf-8") == "concurrent winner"
    assert (report["backup_path"] / "race.txt").read_text(encoding="utf-8") == "source payload"


def test_symlink_in_old_data_is_skipped_without_copying_external_content(tmp_path):
    project_root = tmp_path / "project"
    old_data = project_root / "data"
    target_instance = tmp_path / "data-root" / "instances" / "default"
    outside_dir = tmp_path / "outside"
    old_data.mkdir(parents=True)
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("external secret", encoding="utf-8")
    link_path = old_data / "linked-dir"
    try:
        link_path.symlink_to(outside_dir, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"当前环境无法创建 symlink：{exc}")

    report = ensure_data_root_initialized(make_paths(project_root, target_instance), "0.9.0", now=fixed_now)

    assert report["migrated"] is True
    assert report["skipped_paths"] == ("linked-dir",)
    assert not (target_instance / "linked-dir").exists()
    assert not (report["backup_path"] / "linked-dir").exists()
    assert not (target_instance / "linked-dir" / "secret.txt").exists()
