"""Shared helpers for generated persona files."""

from __future__ import annotations

from typing import Any

from agents.persona_gen_agent import PersonaBrief


def admin_entries(admin_qq: str, admin_name: str) -> list[dict[str, object]]:
    if not admin_qq:
        return []
    entry: dict[str, object] = {"qq": int(admin_qq), "role": "owner"}
    if admin_name:
        entry["name"] = admin_name
    return [entry]


def admin_entries_from_brief(brief: PersonaBrief | None) -> list[dict[str, object]]:
    if brief is None:
        return []
    entries: list[dict[str, object]] = []
    for item in brief.admins:
        qq = str(item.get("qq", "")).strip()
        name = str(item.get("name", "")).strip()
        relation = str(item.get("relation", "")).strip()
        if not qq:
            continue
        entry: dict[str, object] = {"qq": int(qq), "role": "owner"}
        if name:
            entry["name"] = name
        if relation:
            entry["relation"] = relation
        entries.append(entry)
    if entries:
        return entries
    return admin_entries(brief.admin_qq, brief.admin_name)


def render_minimal_persona(
    name: str,
    xml: str,
    admins: list[dict[str, object]] | None = None,
    gender: str = "",
) -> str:
    safe = xml.replace("'''", "\\'\\'\\'")
    persona_vars: dict[str, object] = {"name": name, "admins": admins or []}
    if gender:
        persona_vars["gender"] = gender
    vars_text = format_python_literal(persona_vars)
    return (
        '"""自动生成的人格档案。"""\n\n'
        "PERSONA_PROMPT = '''\n"
        f"{safe}\n"
        "'''\n\n"
        f"PERSONA_VARS = {vars_text}\n"
    )


def format_python_literal(value: Any) -> str:
    import pprint

    return pprint.pformat(value, width=96, sort_dicts=False)


__all__ = [
    "admin_entries",
    "admin_entries_from_brief",
    "format_python_literal",
    "render_minimal_persona",
]
