"""Tool schema integration pipeline tests."""

from __future__ import annotations

import json

from tests.integration_pipeline.helpers import _make_root_config
from tools import build_default_registry


def test_tool_registry_stub_schema_reduces_exposed_tool_schema_size():

    registry = build_default_registry(_make_root_config())

    for name in ("upload_file", "start_agent_task"):

        spec = registry.get_spec(name)

        assert spec is not None

        stub_text = json.dumps(spec.to_openai_schema(), ensure_ascii=False)

        full_text = json.dumps(

            {

                "type": "function",

                "function": {

                    "name": spec.name,

                    "description": spec.description,

                    "parameters": spec.full_parameters_schema(),

                },

            },

            ensure_ascii=False,

        )

        assert "_tool_search_required" in stub_text

        assert len(stub_text) < len(full_text)
