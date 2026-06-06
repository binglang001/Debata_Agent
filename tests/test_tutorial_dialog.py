from __future__ import annotations

from pathlib import Path

from ui.wizard.components import _localize_tutorial_markdown


def test_tutorial_markdown_localizes_repo_raw_images(tmp_path: Path):
    root = tmp_path / "project"
    image = root / "docs" / "images" / "guide.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    markdown = (
        "![图](https://raw.githubusercontent.com/"
        "binglang001/Debata_Agent/main/docs/images/guide.png)"
    )

    result = _localize_tutorial_markdown(
        markdown,
        base_dir=root / "docs" / "feature_guides",
        project_root=root,
    )

    assert image.as_uri() in result
    assert "raw.githubusercontent.com" not in result


def test_tutorial_markdown_localizes_relative_images(tmp_path: Path):
    guide_dir = tmp_path / "docs" / "feature_guides"
    image = guide_dir / "images" / "step.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    markdown = '![步骤](images/step.png)\n<img src="images/step.png">'

    result = _localize_tutorial_markdown(
        markdown,
        base_dir=guide_dir,
        project_root=tmp_path,
    )

    assert result.count(image.as_uri()) == 2
