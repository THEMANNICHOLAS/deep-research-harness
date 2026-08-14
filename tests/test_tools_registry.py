"""Behavioral tests for harness.tools.build_tools."""

from langchain_core.tools import BaseTool

from harness.sources import SourceRegistry
from harness.tools import build_tools


def test_build_tools_returns_the_frozen_tool_set(make_config):
    config = make_config()

    tools = build_tools(config, SourceRegistry())

    # Ordered, not a set: `harness/tools/__init__.py`'s builder list is part of the contract.
    assert [tool.name for tool in tools] == ["fetch_pages", "search_web", "ask_user"]


def test_every_tool_exposes_description_and_json_schema(make_config):
    config = make_config()

    tools = build_tools(config, SourceRegistry())

    by_name = {tool.name: tool for tool in tools}
    for tool in tools:
        assert isinstance(tool.description, str)
        assert tool.description
        assert tool.args_schema is not None
        schema = tool.args_schema.model_json_schema()
        assert isinstance(schema, dict)
        assert schema["properties"]

    assert "urls" in by_name["fetch_pages"].args_schema.model_json_schema()["properties"]
    search_props = by_name["search_web"].args_schema.model_json_schema()["properties"]
    assert "query" in search_props
    assert "max_results" in search_props
    assert "question" in by_name["ask_user"].args_schema.model_json_schema()["properties"]


async def test_build_tools_wires_the_callers_registry_into_the_fetch_tool(make_config, monkeypatch):
    """D8: the caller's per-run registry must reach the fetch tool, not a private one."""
    config = make_config()
    registry = SourceRegistry()
    seen = []

    async def _spy(urls, cfg, reg):
        seen.append(reg)
        return "", []

    monkeypatch.setattr("harness.tools.fetch._fetch", _spy)

    by_name = {tool.name: tool for tool in build_tools(config, registry)}
    await by_name["fetch_pages"].ainvoke({"urls": ["https://example.test/a"]})

    assert len(seen) == 1
    assert seen[0] is registry


def test_tools_are_langchain_base_tools_with_content_and_artifact(make_config):
    config = make_config()

    tools = build_tools(config, SourceRegistry())

    for tool in tools:
        assert isinstance(tool, BaseTool)
        assert tool.response_format == "content_and_artifact"
