"""阶段 0 数据根初始化与旧 data 入根迁移。"""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MIGRATION_ID = "project_data_to_instance_dir.v0"


class DataMigrationError(RuntimeError):
    """数据迁移无法安全执行时抛出。"""


def ensure_data_root_initialized(
    paths: object,
    app_version: str,
    *,
    now: Callable[[], datetime | str] | None = None,
) -> dict[str, Any]:
    """初始化数据根清单，并在需要时把旧 data 内容复制进实例目录。"""

    timestamp = _format_timestamp(now() if now is not None else datetime.now(timezone.utc))
    project_root = _get_path_attr(paths, ("PROJECT_ROOT", "project_root"))
    target_instance_dir = _get_path_attr(paths, ("DATA_DIR", "data_dir", "target_instance_dir"))
    if target_instance_dir is None:
        raise DataMigrationError("paths 必须提供 DATA_DIR 或 data_dir")

    source_data_dir = project_root / "data" if project_root is not None else None
    data_root = _get_path_attr(paths, ("DATA_ROOT", "DATA_ROOT_DIR", "data_root", "data_root_dir"))
    if data_root is None:
        data_root = _infer_data_root(target_instance_dir, source_data_dir)

    data_root.mkdir(parents=True, exist_ok=True)
    target_instance_dir.mkdir(parents=True, exist_ok=True)

    root_json = data_root / "root.json"
    instance_json = target_instance_dir / "instance.json"
    root_manifest = _load_manifest(root_json)
    instance_manifest = _load_manifest(instance_json)
    root_changed = _ensure_root_manifest(root_manifest, app_version, timestamp, target_instance_dir)
    instance_changed = _ensure_instance_manifest(instance_manifest, app_version, timestamp, target_instance_dir)

    migration_record = _get_current_migration_record(root_manifest, source_data_dir, target_instance_dir)
    if migration_record is not None:
        instance_migrations = instance_manifest.setdefault("migrations", {})
        if isinstance(instance_migrations, dict) and MIGRATION_ID not in instance_migrations:
            instance_migrations[MIGRATION_ID] = migration_record
            instance_changed = True
        if root_changed:
            _atomic_write_json(root_json, root_manifest)
        if instance_changed:
            _atomic_write_json(instance_json, instance_manifest)
        return _report(
            migrated=False,
            skipped=True,
            backup_path=_path_or_none(migration_record.get("backup_path")),
            source_data_dir=source_data_dir,
            target_instance_dir=target_instance_dir,
            message="旧 data 已完成迁移，本次跳过。",
        )

    should_migrate, skip_message = _should_migrate(source_data_dir, target_instance_dir)
    if not should_migrate:
        if root_changed:
            _atomic_write_json(root_json, root_manifest)
        if instance_changed:
            _atomic_write_json(instance_json, instance_manifest)
        return _report(
            migrated=False,
            skipped=True,
            backup_path=None,
            source_data_dir=source_data_dir,
            target_instance_dir=target_instance_dir,
            message=skip_message,
        )

    backup_path, backup_skipped_paths = _create_backup(source_data_dir, data_root, timestamp)
    copy_skipped_paths = _copy_tree_contents_without_overwrite(source_data_dir, target_instance_dir)
    skipped_paths = _merge_skipped_paths(backup_skipped_paths, copy_skipped_paths)
    _record_migration(
        root_manifest,
        instance_manifest,
        app_version=app_version,
        timestamp=timestamp,
        source_data_dir=source_data_dir,
        target_instance_dir=target_instance_dir,
        backup_path=backup_path,
        skipped_paths=skipped_paths,
    )
    _atomic_write_json(root_json, root_manifest)
    _atomic_write_json(instance_json, instance_manifest)

    message = "旧 data 已复制到目标实例目录。"
    if skipped_paths:
        message = f"{message} 有 {len(skipped_paths)} 个路径已跳过。"
    return _report(
        migrated=True,
        skipped=False,
        backup_path=backup_path,
        source_data_dir=source_data_dir,
        target_instance_dir=target_instance_dir,
        message=message,
        skipped_paths=skipped_paths,
    )


def _get_path_attr(paths: object, names: tuple[str, ...]) -> Path | None:
    for name in names:
        value = getattr(paths, name, None)
        if value is not None:
            return Path(value)
    return None


def _infer_data_root(target_instance_dir: Path, source_data_dir: Path | None) -> Path:
    if source_data_dir is not None and _same_path(target_instance_dir, source_data_dir):
        return target_instance_dir
    if target_instance_dir.parent.name == "instances":
        return target_instance_dir.parent.parent
    return target_instance_dir.parent


def _format_timestamp(value: datetime | str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        return value.strftime("%Y%m%dT%H%M%SZ")
    return "".join(ch if ch.isalnum() else "-" for ch in value).strip("-") or "unknown-time"


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DataMigrationError(f"无法读取 JSON 清单：{path}") from exc
    if not isinstance(data, dict):
        raise DataMigrationError(f"JSON 清单必须是对象：{path}")
    return data


def _ensure_root_manifest(
    manifest: dict[str, Any],
    app_version: str,
    timestamp: str,
    target_instance_dir: Path,
) -> bool:
    changed = False
    defaults: dict[str, Any] = {
        "schema": "debata.data_root.v0",
        "app_version": app_version,
        "created_at": timestamp,
    }
    for key, value in defaults.items():
        if key not in manifest:
            manifest[key] = value
            changed = True
    if _upsert_instance_entry(manifest, target_instance_dir):
        changed = True
    if not isinstance(manifest.get("migrations"), dict):
        manifest["migrations"] = {}
        changed = True
    return changed


def _ensure_instance_manifest(
    manifest: dict[str, Any],
    app_version: str,
    timestamp: str,
    target_instance_dir: Path,
) -> bool:
    changed = False
    defaults: dict[str, Any] = {
        "schema": "debata.data_instance.v0",
        "app_version": app_version,
        "created_at": timestamp,
        "data_dir": str(target_instance_dir),
    }
    for key, value in defaults.items():
        if key not in manifest:
            manifest[key] = value
            changed = True
    if not isinstance(manifest.get("migrations"), dict):
        manifest["migrations"] = {}
        changed = True
    return changed


def _upsert_instance_entry(manifest: dict[str, Any], target_instance_dir: Path) -> bool:
    instance_name = target_instance_dir.name
    instance_path = str(target_instance_dir)
    instances = manifest.get("instances")
    if not isinstance(instances, list):
        manifest["instances"] = [{"name": instance_name, "path": instance_path}]
        return True

    for item in instances:
        if isinstance(item, dict) and item.get("name") == instance_name:
            if item.get("path") == instance_path:
                return False
            item["path"] = instance_path
            return True

    instances.append({"name": instance_name, "path": instance_path})
    return True


def _get_current_migration_record(
    root_manifest: dict[str, Any],
    source_data_dir: Path | None,
    target_instance_dir: Path,
) -> dict[str, Any] | None:
    migrations = root_manifest.get("migrations")
    if not isinstance(migrations, dict):
        return None
    migration_entry = migrations.get(MIGRATION_ID)
    if not isinstance(migration_entry, dict) or source_data_dir is None:
        return None
    records = _iter_migration_records(migration_entry, target_instance_dir.name)
    for record in records:
        if record.get("source_data_dir") != str(source_data_dir):
            continue
        if record.get("target_instance_dir") != str(target_instance_dir):
            continue
        return record
    return None


def _iter_migration_records(migration_entry: dict[str, Any], instance_name: str) -> tuple[dict[str, Any], ...]:
    if _is_migration_record(migration_entry):
        return (migration_entry,)

    records: list[dict[str, Any]] = []
    preferred_record = migration_entry.get(instance_name)
    if isinstance(preferred_record, dict) and _is_migration_record(preferred_record):
        records.append(preferred_record)
    for key, value in migration_entry.items():
        if key == instance_name:
            continue
        if isinstance(value, dict) and _is_migration_record(value):
            records.append(value)
    return tuple(records)


def _is_migration_record(value: dict[str, Any]) -> bool:
    return "source_data_dir" in value and "target_instance_dir" in value


def _should_migrate(source_data_dir: Path | None, target_instance_dir: Path) -> tuple[bool, str]:
    if source_data_dir is None:
        return False, "未提供项目根目录，只初始化清单。"
    if not source_data_dir.exists():
        return False, "旧 data 不存在，只初始化清单。"
    if not source_data_dir.is_dir():
        return False, "旧 data 不是目录，只初始化清单。"
    if _same_path(source_data_dir, target_instance_dir):
        return False, "DATA_DIR 仍指向旧 project_root/data，只初始化清单。"
    if _is_relative_to(target_instance_dir, source_data_dir):
        return False, "目标实例目录位于旧 data 内，为避免递归复制而跳过迁移。"
    return True, ""


def _create_backup(source_data_dir: Path, data_root: Path, timestamp: str) -> tuple[Path, tuple[str, ...]]:
    backups_dir = data_root / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backups_dir / f"data-{timestamp}"
    suffix = 1
    while backup_path.exists():
        suffix += 1
        backup_path = backups_dir / f"data-{timestamp}-{suffix}"
    backup_path.mkdir(parents=True)
    try:
        skipped_paths = _copy_tree_contents_without_overwrite(source_data_dir, backup_path)
    except Exception:
        if backup_path.exists() and not _is_symlink_or_reparse_point(backup_path):
            shutil.rmtree(backup_path)
        raise
    return backup_path, skipped_paths


def _copy_tree_contents_without_overwrite(source_data_dir: Path, target_instance_dir: Path) -> tuple[str, ...]:
    skipped: list[str] = []
    for source_path, relative_path, skip_entry in _iter_source_tree(source_data_dir):
        relative_path_text = relative_path.as_posix()
        if skip_entry:
            skipped.append(relative_path_text)
            continue
        target_path = target_instance_dir / relative_path
        if source_path.is_dir():
            if _is_symlink_or_reparse_point(target_path) or _has_blocking_or_unsafe_parent(
                target_path, target_instance_dir
            ):
                skipped.append(relative_path_text)
                continue
            if target_path.exists() and not target_path.is_dir():
                skipped.append(relative_path_text)
                continue
            target_path.mkdir(parents=True, exist_ok=True)
            continue
        if _path_exists_or_link(target_path):
            skipped.append(relative_path_text)
            continue
        if _has_blocking_or_unsafe_parent(target_path, target_instance_dir):
            skipped.append(relative_path_text)
            continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if _has_blocking_or_unsafe_parent(target_path, target_instance_dir):
            skipped.append(relative_path_text)
            continue
        copied = _copy_file_without_overwrite(source_path, target_path)
        if not copied:
            skipped.append(relative_path_text)
    return tuple(skipped)


def _iter_source_tree(source_data_dir: Path) -> tuple[tuple[Path, Path, bool], ...]:
    entries: list[tuple[Path, Path, bool]] = []
    stack = [source_data_dir]
    while stack:
        current_dir = stack.pop()
        try:
            children = sorted(current_dir.iterdir(), key=lambda item: item.name)
        except OSError:
            if current_dir != source_data_dir:
                entries.append((current_dir, current_dir.relative_to(source_data_dir), True))
            continue
        for child in children:
            relative_path = child.relative_to(source_data_dir)
            if _is_symlink_or_reparse_point(child):
                entries.append((child, relative_path, True))
                continue
            if child.is_dir():
                entries.append((child, relative_path, False))
                stack.append(child)
                continue
            entries.append((child, relative_path, False))
    return tuple(entries)


def _copy_file_without_overwrite(source_path: Path, target_path: Path) -> bool:
    if _is_symlink_or_reparse_point(source_path):
        return False

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY

    fd: int | None = None
    created_target = False
    try:
        fd = os.open(target_path, flags, 0o666)
        created_target = True
        with source_path.open("rb") as source_file:
            with os.fdopen(fd, "wb") as target_file:
                fd = None
                shutil.copyfileobj(source_file, target_file)
        shutil.copystat(source_path, target_path, follow_symlinks=False)
        return True
    except FileExistsError:
        return False
    except Exception:
        if fd is not None:
            os.close(fd)
        if created_target:
            try:
                target_path.unlink()
            except FileNotFoundError:
                pass
        raise


def _merge_skipped_paths(*groups: tuple[str, ...]) -> tuple[str, ...]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for path in group:
            if path in seen:
                continue
            seen.add(path)
            merged.append(path)
    return tuple(merged)


def _record_migration(
    root_manifest: dict[str, Any],
    instance_manifest: dict[str, Any],
    *,
    app_version: str,
    timestamp: str,
    source_data_dir: Path,
    target_instance_dir: Path,
    backup_path: Path,
    skipped_paths: tuple[str, ...],
) -> None:
    record: dict[str, Any] = {
        "app_version": app_version,
        "completed_at": timestamp,
        "source_data_dir": str(source_data_dir),
        "target_instance_dir": str(target_instance_dir),
        "backup_path": str(backup_path),
        "skipped_paths": list(skipped_paths),
    }
    root_manifest["updated_at"] = timestamp
    _record_root_migration(root_manifest, target_instance_dir.name, record)
    instance_manifest["updated_at"] = timestamp
    instance_manifest.setdefault("migrations", {})[MIGRATION_ID] = record


def _record_root_migration(root_manifest: dict[str, Any], instance_name: str, record: dict[str, Any]) -> None:
    migrations = root_manifest.setdefault("migrations", {})
    if not isinstance(migrations, dict):
        migrations = {}
        root_manifest["migrations"] = migrations
    migration_entry = migrations.get(MIGRATION_ID)
    if isinstance(migration_entry, dict) and _is_migration_record(migration_entry):
        legacy_record = migration_entry
        legacy_instance_name = _instance_name_from_record(legacy_record)
        migration_entry = {legacy_instance_name: legacy_record}
    elif not isinstance(migration_entry, dict):
        migration_entry = {}
    migrations[MIGRATION_ID] = migration_entry
    migration_entry[instance_name] = record


def _instance_name_from_record(record: dict[str, Any]) -> str:
    target_instance_dir = record.get("target_instance_dir")
    if isinstance(target_instance_dir, str) and target_instance_dir:
        return Path(target_instance_dir).name
    return "default"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _same_path(left: Path, right: Path) -> bool:
    return left.resolve(strict=False) == right.resolve(strict=False)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except ValueError:
        return False
    return True


def _has_blocking_or_unsafe_parent(path: Path, root: Path) -> bool:
    for parent in path.parents:
        if _same_path(parent, root):
            return False
        if _is_symlink_or_reparse_point(parent):
            return True
        if parent.exists() and not parent.is_dir():
            return True
    return False


def _path_exists_or_link(path: Path) -> bool:
    return path.exists() or path.is_symlink() or _is_symlink_or_reparse_point(path)


def _is_symlink_or_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    try:
        file_attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(file_attributes & reparse_point)


def _path_or_none(value: object) -> Path | None:
    if value is None:
        return None
    return Path(value)


def _report(
    *,
    migrated: bool,
    skipped: bool,
    backup_path: Path | None,
    source_data_dir: Path | None,
    target_instance_dir: Path,
    message: str,
    skipped_paths: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "migrated": migrated,
        "skipped": skipped,
        "backup_path": backup_path,
        "source_data_dir": source_data_dir,
        "target_instance_dir": target_instance_dir,
        "message": message,
        "skipped_paths": skipped_paths,
    }
