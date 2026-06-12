"""人格目录导入辅助。

这里放非 UI 的校验/复制逻辑，供首次向导和仪表盘角色页共用。
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path, PurePosixPath

from .persona_loader import validate_persona_name


class PersonaImportError(ValueError):
    """人格导入校验失败。"""


def validate_persona_source_dir(src: Path) -> Path:
    """校验 src 是可导入的人格目录，并返回绝对路径。"""
    src = src.resolve()
    if not src.is_dir():
        raise PersonaImportError(f"不是目录：{src}")
    validate_persona_name(src.name)
    if not (src / "persona_prompt.py").is_file():
        raise PersonaImportError(f"目录下没有 persona_prompt.py：{src}")
    return src


def copy_persona_dir(src: Path, personas_dir: Path) -> str:
    """复制人格目录到 personas_dir，拒绝同名覆盖。返回导入后名称。"""
    src = validate_persona_source_dir(src)
    personas_dir.mkdir(parents=True, exist_ok=True)
    dst = personas_dir / src.name
    if dst.exists():
        raise PersonaImportError(f"角色「{src.name}」已存在，请先重命名后再导入")
    shutil.copytree(src, dst)
    return src.name


def import_persona_zip(zip_path: Path, personas_dir: Path) -> str:
    """安全导入角色 zip。要求 zip 内只有一个根目录，且包含 persona_prompt.py。"""
    personas_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        entries = _validated_zip_entries(zf)
        roots = {parts[0] for _info, parts in entries}
        if len(roots) != 1:
            raise PersonaImportError("zip 必须只包含一个角色根目录")
        root = next(iter(roots))
        validate_persona_name(root)
        if not any(parts == (root, "persona_prompt.py") for _info, parts in entries):
            raise PersonaImportError("zip 内缺少 persona_prompt.py")

        dst = personas_dir / root
        if dst.exists():
            raise PersonaImportError(f"角色「{root}」已存在，请先重命名后再导入")

        base = personas_dir.resolve()
        for info, parts in entries:
            target = (personas_dir / Path(*parts)).resolve()
            try:
                target.relative_to(base)
            except ValueError as e:
                raise PersonaImportError(f"zip 含非法路径：{info.filename}") from e
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    return root


def _validated_zip_entries(
    zf: zipfile.ZipFile,
) -> list[tuple[zipfile.ZipInfo, tuple[str, ...]]]:
    entries: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
    for info in zf.infolist():
        name = info.filename
        if not name or name.endswith("/"):
            continue
        path = PurePosixPath(name)
        if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
            raise PersonaImportError(f"zip 含非法路径：{name}")
        if len(path.parts) < 2:
            raise PersonaImportError("zip 文件必须把角色文件放在一个根目录内")
        entries.append((info, tuple(path.parts)))
    if not entries:
        raise PersonaImportError("zip 为空")
    return entries


__all__ = [
    "PersonaImportError",
    "copy_persona_dir",
    "import_persona_zip",
    "validate_persona_source_dir",
]
