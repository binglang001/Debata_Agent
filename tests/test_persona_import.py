"""人格导入安全与同名处理回归测试。"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from agents.persona_import import (
    PersonaImportError,
    copy_persona_dir,
    import_persona_zip,
)
from ui.wizard.context import WizardContext
from ui.wizard.window import WizardWindow


def _make_persona_dir(root: Path, name: str, prompt: str = "hi") -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "persona_prompt.py").write_text(
        f'PERSONA_PROMPT = "{prompt}"\nPERSONA_VARS = {{}}\n',
        encoding="utf-8",
    )
    return d


def test_copy_persona_dir_rejects_duplicate(tmp_path: Path):
    src_root = tmp_path / "src"
    dst_root = tmp_path / "personas"
    src = _make_persona_dir(src_root, "alice")
    _make_persona_dir(dst_root, "alice")

    with pytest.raises(PersonaImportError, match="已存在"):
        copy_persona_dir(src, dst_root)


def test_copy_persona_dir_requires_prompt(tmp_path: Path):
    src = tmp_path / "src" / "bad"
    src.mkdir(parents=True)

    with pytest.raises(PersonaImportError, match="persona_prompt.py"):
        copy_persona_dir(src, tmp_path / "personas")


def test_import_persona_zip_rejects_path_traversal(tmp_path: Path):
    zip_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("../evil.py", "boom")

    with pytest.raises(PersonaImportError, match="非法路径"):
        import_persona_zip(zip_path, tmp_path / "personas")


def test_import_persona_zip_imports_single_valid_persona(tmp_path: Path):
    zip_path = tmp_path / "alice.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("alice/persona_prompt.py", 'PERSONA_PROMPT = "hi"\n')
        zf.writestr("alice/avatar.txt", "avatar")

    name = import_persona_zip(zip_path, tmp_path / "personas")

    assert name == "alice"
    assert (tmp_path / "personas" / "alice" / "persona_prompt.py").exists()
    assert (tmp_path / "personas" / "alice" / "avatar.txt").exists()


def test_wizard_create_persona_rejects_duplicate(tmp_paths):
    _make_persona_dir(tmp_paths.PERSONAS_DIR, "alice")
    ctx = WizardContext()
    ctx.persona.source = "create"
    ctx.persona.active = "alice"
    ctx.persona.generated_xml = "<identity>Alice</identity>"

    dummy = type(
        "WizardStub",
        (),
        {
            "_context": ctx,
            "_paths": tmp_paths,
        },
    )()

    with pytest.raises(PersonaImportError, match="已存在"):
        WizardWindow._save_persona_if_needed(dummy)
