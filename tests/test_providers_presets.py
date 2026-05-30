"""测试 provider 预设加载（含内置预设的完整性检查）。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from providers.presets_loader import (
    ModelInfo,
    ProviderPreset,
    load_all_presets,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRESETS_DIR = PROJECT_ROOT / "providers" / "presets"


# ============================================================
# ModelInfo / ProviderPreset 类型本身
# ============================================================


def test_model_info_from_dict():
    m = ModelInfo.from_dict(
        {
            "id": "deepseek-chat",
            "display_name": "DeepSeek V4",
            "capabilities": ["chat", "tool_call"],
            "context_length": 128000,
        }
    )
    assert m.id == "deepseek-chat"
    assert m.context_length == 128000
    assert "chat" in m.capabilities


def test_model_info_defaults():
    m = ModelInfo.from_dict({"id": "x"})
    assert m.display_name == "x"
    assert m.capabilities == []
    assert m.context_length == 0


# ============================================================
# YAML 加载
# ============================================================


def test_load_preset_from_yaml(tmp_path):
    yaml_content = """
id: testprov
display_name: Test Provider
protocol: openai_compat
base_url: https://api.test.com
registration_url: https://test.com/signup
reasoning_style: thinking_extra_body
models:
  - id: model-a
    display_name: Model A
    capabilities: [chat, tool_call]
    context_length: 32000
"""
    preset_dir = tmp_path / "testprov"
    preset_dir.mkdir()
    (preset_dir / "preset.yaml").write_text(yaml_content, encoding="utf-8")

    preset = ProviderPreset.from_yaml(preset_dir / "preset.yaml")
    assert preset.id == "testprov"
    assert preset.protocol == "openai_compat"
    assert len(preset.models) == 1
    assert preset.models[0].id == "model-a"


def test_load_preset_missing_id_raises(tmp_path):
    (tmp_path / "preset.yaml").write_text("display_name: X", encoding="utf-8")
    with pytest.raises(ValueError, match="缺 id"):
        ProviderPreset.from_yaml(tmp_path / "preset.yaml")


def test_load_preset_id_lowercased(tmp_path):
    (tmp_path / "preset.yaml").write_text(
        "id: UPPER\ndisplay_name: X\nprotocol: openai_compat\nbase_url: https://x",
        encoding="utf-8",
    )
    preset = ProviderPreset.from_yaml(tmp_path / "preset.yaml")
    assert preset.id == "upper"


def test_builtin_presets_include_current_flagship_models():
    """这些 ID 来自 2026-05-29 查询的官方模型文档。"""
    presets = load_all_presets(PRESETS_DIR)

    assert any(m.id == "gpt-5.5" for m in presets["openai"].models)
    assert any(m.id == "claude-opus-4-8" for m in presets["anthropic"].models)
    assert any(m.id == "qwen3.7-max" for m in presets["qwen"].models)
    assert any(m.id == "doubao-seed-2-0-lite-260428" for m in presets["volcengine"].models)


def test_load_preset_detects_tutorial_dir(tmp_path):
    (tmp_path / "preset.yaml").write_text(
        "id: p\ndisplay_name: P\nprotocol: openai_compat\nbase_url: https://x",
        encoding="utf-8",
    )
    (tmp_path / "tutorial").mkdir()
    preset = ProviderPreset.from_yaml(tmp_path / "preset.yaml")
    assert preset.tutorial_dir is not None
    assert preset.tutorial_dir.name == "tutorial"


def test_load_all_presets_smoke(tmp_path):
    """空目录应返回 {}。"""
    presets = load_all_presets(tmp_path, force_reload=True)
    assert presets == {}


def test_load_all_presets_uses_cache(tmp_path):
    (tmp_path / "good").mkdir()
    (tmp_path / "good" / "preset.yaml").write_text(
        "id: good\ndisplay_name: G\nprotocol: openai_compat\nbase_url: https://g",
        encoding="utf-8",
    )
    first = load_all_presets(tmp_path, force_reload=True)
    assert "good" in first

    (tmp_path / "good" / "preset.yaml").unlink()
    second = load_all_presets(tmp_path)
    assert second.keys() == first.keys()


def test_load_all_presets_skips_invalid(tmp_path):
    # 一个 valid + 一个 invalid + 一个不带 preset.yaml 的目录
    (tmp_path / "good").mkdir()
    (tmp_path / "good" / "preset.yaml").write_text(
        "id: good\ndisplay_name: G\nprotocol: openai_compat\nbase_url: https://g",
        encoding="utf-8",
    )
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "preset.yaml").write_text("invalid: yaml: content", encoding="utf-8")
    (tmp_path / "empty").mkdir()

    presets = load_all_presets(tmp_path)
    assert "good" in presets
    assert "bad" not in presets


def test_load_all_presets_skips_underscore_dirs(tmp_path):
    """以 _ 开头的目录被跳过。"""
    (tmp_path / "_private").mkdir()
    (tmp_path / "_private" / "preset.yaml").write_text(
        "id: x\ndisplay_name: X\nprotocol: openai_compat\nbase_url: https://x",
        encoding="utf-8",
    )
    presets = load_all_presets(tmp_path)
    assert "x" not in presets


# ============================================================
# 内置预设完整性检查
# ============================================================


def test_builtin_presets_load():
    presets = load_all_presets(PRESETS_DIR)
    assert len(presets) > 0, "至少应有一个内置预设"

    # 关键预设必须存在
    expected = {"deepseek", "glm", "openai", "anthropic", "moonshot", "qwen", "volcengine"}
    assert expected.issubset(presets.keys()), (
        f"缺少关键预设: {expected - set(presets.keys())}"
    )


def test_builtin_presets_have_required_fields():
    presets = load_all_presets(PRESETS_DIR)
    for pid, preset in presets.items():
        assert preset.display_name, f"{pid} 缺 display_name"
        assert preset.protocol in ("openai_compat", "anthropic", "gemini", "volcengine"), (
            f"{pid} 协议未识别: {preset.protocol}"
        )
        assert preset.base_url.startswith("http"), f"{pid} base_url 不合法: {preset.base_url}"
        assert preset.models, f"{pid} 没有 model 列表"
        for m in preset.models:
            assert m.id, f"{pid} 有空 model id"
            assert m.context_length >= 0


def test_builtin_presets_registration_urls():
    """所有预设都应有注册链接，方便引导用户。"""
    presets = load_all_presets(PRESETS_DIR)
    for pid, preset in presets.items():
        assert preset.registration_url, f"{pid} 没有 registration_url"


def test_builtin_presets_tutorial_dirs():
    """所有预设都应有 tutorial/ 目录占位（即使为空）。"""
    presets = load_all_presets(PRESETS_DIR)
    for pid, preset in presets.items():
        tutorial = PRESETS_DIR / pid / "tutorial"
        assert tutorial.is_dir(), f"{pid} 缺 tutorial/ 子目录"
