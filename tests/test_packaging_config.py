"""打包配置回归测试。"""

from __future__ import annotations

import tomllib
from pathlib import Path

from setuptools import find_packages


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    return tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_setuptools_find_packages_includes_runtime_subpackages():
    cfg = _pyproject()["tool"]["setuptools"]["packages"]["find"]
    packages = set(
        find_packages(
            where=str(PROJECT_ROOT),
            include=cfg["include"],
            exclude=cfg["exclude"],
        )
    )

    assert {
        "features.embedding",
        "plugins",
        "ui.widgets",
        "ui.wizard.step_views",
        "providers.protocols",
    } <= packages


def test_package_data_keeps_provider_presets_and_plugin_docs():
    package_data = _pyproject()["tool"]["setuptools"]["package-data"]

    assert "presets/*/preset.yaml" in package_data["providers"]
    assert "presets/*/tutorial/*.md" in package_data["providers"]
    assert "PLUGIN_SPEC.md" in package_data["plugins"]
    assert "voxcpm2/*.py" in package_data["plugins"]
    assert "embedding_bge_zh/*.py" in package_data["plugins"]
    assert "embedding_minilm/*.py" in package_data["plugins"]


def test_default_dependencies_do_not_include_heavy_local_model_deps():
    deps = set(_pyproject()["project"]["dependencies"])

    heavy_local = {
        "torch",
        "sentence-transformers",
        "voxcpm",
        "soundfile",
    }
    normalized = {dep.split(">=", 1)[0].split("==", 1)[0] for dep in deps}

    assert heavy_local.isdisjoint(normalized)


def test_default_dependencies_include_ddgs_not_legacy_duckduckgo_search():
    deps = set(_pyproject()["project"]["dependencies"])
    normalized = {dep.split(">=", 1)[0].split("==", 1)[0] for dep in deps}

    assert "ddgs" in normalized
    assert "duckduckgo-search" not in normalized
    assert "duckduckgo_search" not in normalized


def test_plugin_python_deps_are_optional_local_model_dependencies():
    from plugins import PluginManager

    optional = _pyproject()["project"]["optional-dependencies"]
    local_deps = {
        dep.split(">=", 1)[0].split("==", 1)[0]
        for group in ("asr-local", "tts-local", "embedding-local", "local-models")
        for dep in optional[group]
    }
    manager = PluginManager(PROJECT_ROOT / "plugins")
    manager.scan()

    plugin_deps = {
        dep.split(">=", 1)[0].split("==", 1)[0]
        for record in manager.list_all()
        for dep in record.meta.python_deps
    }

    assert plugin_deps <= local_deps
