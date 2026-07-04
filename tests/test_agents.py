"""测试 agents 层：persona_loader。"""

from __future__ import annotations

import pytest

from agents.persona_loader import (
    Persona,
    find_persona_dir,
    list_available_personas,
    load_persona,
    validate_persona_name,
)

# ============================================================
# persona_loader
# ============================================================


def test_validate_persona_name_ok():
    validate_persona_name("yuexi")
    validate_persona_name("a_b-c123")
    # 中文与混合应通过（人格名常用中文）
    validate_persona_name("小明")
    validate_persona_name("寒月-01")
    validate_persona_name("小桃")


def test_validate_persona_name_rejects_bad():
    # 拒：空 / 长 / 路径敏感字符 / 空格 / 特殊符号
    for bad in ["", "..", "hi/x", "a\\b", "a:b", 'a"b', "a*b", "a?b", "a<b", "a|b", "a b", "a" * 65]:
        with pytest.raises(ValueError):
            validate_persona_name(bad)


def test_find_persona_dir_missing(tmp_paths):
    assert find_persona_dir(tmp_paths, "nonexistent") is None


def test_find_persona_dir_returns_path(tmp_paths):
    # 所有人格平级在 PERSONAS_DIR 下，找到就返回路径
    (tmp_paths.PERSONAS_DIR / "alice").mkdir(parents=True)
    (tmp_paths.PERSONAS_DIR / "alice" / "persona_prompt.py").write_text(
        "PERSONA_PROMPT = 'x'", encoding="utf-8"
    )
    found = find_persona_dir(tmp_paths, "alice")
    assert found == tmp_paths.PERSONAS_DIR / "alice"


def test_load_persona_basic(tmp_paths):
    (tmp_paths.PERSONAS_DIR / "alice").mkdir(parents=True)
    (tmp_paths.PERSONAS_DIR / "alice" / "persona_prompt.py").write_text(
        'PERSONA_PROMPT = "你是 Alice"\n'
        'PERSONA_VARS = {"name": "Alice", "admins": [{"qq": 1, "name": "Lily"}]}\n',
        encoding="utf-8",
    )
    p = load_persona(tmp_paths, "alice")
    assert p.name == "alice"
    assert p.prompt == "你是 Alice"
    assert p.vars["name"] == "Alice"
    assert p.get_admins() == [{"qq": 1, "name": "Lily"}]
    assert p.display_name() == "Alice"


@pytest.mark.parametrize(
    ("raw_age", "expected"),
    [
        (18, 18),
        (0, 0),
        (-1, None),
        ("18", 18),
        (" 18 ", 18),
        ("0018", 18),
        ("0", 0),
        ("", None),
        ("   ", None),
        ("18.5", None),
        ("abc", None),
        ("-1", None),
        (None, None),
    ],
)
def test_persona_get_age_parses_int_and_string_cases(raw_age, expected):
    persona = Persona(name="alice", prompt="你是 Alice", vars={"age": raw_age})

    assert persona.get_age() == expected


@pytest.mark.parametrize("raw_age", [True, False])
def test_persona_get_age_rejects_bool(raw_age):
    persona = Persona(name="alice", prompt="你是 Alice", vars={"age": raw_age})

    assert persona.get_age() is None


def test_load_persona_missing_prompt_raises(tmp_paths):
    (tmp_paths.PERSONAS_DIR / "bad").mkdir(parents=True)
    (tmp_paths.PERSONAS_DIR / "bad" / "persona_prompt.py").write_text(
        "X = 1", encoding="utf-8"
    )
    with pytest.raises(ValueError):
        load_persona(tmp_paths, "bad")


def test_load_persona_not_found(tmp_paths):
    with pytest.raises(FileNotFoundError):
        load_persona(tmp_paths, "ghost")


def test_list_available_personas(tmp_paths):
    # 所有人格平级。下划线开头的目录被忽略（保留作为系统目录）
    (tmp_paths.PERSONAS_DIR / "p1").mkdir(parents=True)
    (tmp_paths.PERSONAS_DIR / "p1" / "persona_prompt.py").write_text(
        'PERSONA_PROMPT = "1"', encoding="utf-8"
    )
    (tmp_paths.PERSONAS_DIR / "p2").mkdir(parents=True)
    (tmp_paths.PERSONAS_DIR / "p2" / "persona_prompt.py").write_text(
        'PERSONA_PROMPT = "2"', encoding="utf-8"
    )
    # 下划线开头的应被忽略
    (tmp_paths.PERSONAS_DIR / "_hidden").mkdir(parents=True)
    (tmp_paths.PERSONAS_DIR / "_hidden" / "persona_prompt.py").write_text(
        'PERSONA_PROMPT = "x"', encoding="utf-8"
    )

    found = list_available_personas(tmp_paths)
    assert "p1" in found
    assert "p2" in found
    assert "_hidden" not in found
