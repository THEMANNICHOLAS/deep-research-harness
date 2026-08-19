"""Behavioral tests for harness.tools.build_tools."""

from langchain_core.tools import BaseTool

import harness.tools
from harness.runlog import RunLog
from harness.sources import SourceRegistry
from harness.tools import build_tools


def test_build_tools_returns_the_frozen_tool_set(make_config, monkeypatch):
    config = make_config()

    calls = []
    real_build_fetch_tool = harness.tools.build_fetch_tool

    def _spy(cfg, reg, log):
        calls.append((cfg, reg, log))
        return real_build_fetch_tool(cfg, reg, log)

    monkeypatch.setattr("harness.tools.build_fetch_tool", _spy)

    tool_sets = build_tools(config, SourceRegistry())

    # Ordered, not a set: `harness/tools/__init__.py`'s builder list is part of the contract.
    # Step 3: `search_web` and `fetch_raw` both moved off the lead onto the researcher — the
    # lead delegates through `task` instead of researching directly, and the digest-recovery
    # loop belongs to whoever dispatches readers. `fetch_pages` stays on the reader.
    # R4 regression (PLAN-prompt-injection-defense.md Phase 5): this exact three-tier split is
    # the containment floor the fencing/sanitizing work in this phase builds on top of.
    assert [tool.name for tool in tool_sets.lead] == ["ask_user"]
    assert [tool.name for tool in tool_sets.researcher] == ["search_web", "fetch_raw"]
    assert [tool.name for tool in tool_sets.reader] == ["fetch_pages"]

    # The fetch instance is built exactly once and routed to the reader, never duplicated.
    assert len(calls) == 1


def test_every_tool_exposes_description_and_json_schema(make_config):
    config = make_config()

    tool_sets = build_tools(config, SourceRegistry())
    tools = [*tool_sets.lead, *tool_sets.researcher, *tool_sets.reader]

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
    fetch_raw_props = by_name["fetch_raw"].args_schema.model_json_schema()["properties"]
    assert "urls" in fetch_raw_props
    assert "reason" in fetch_raw_props


async def test_build_tools_wires_the_callers_registry_into_the_fetch_tool(make_config, monkeypatch):
    """D8: the caller's per-run registry must reach the fetch tool, not a private one."""
    config = make_config()
    registry = SourceRegistry()
    seen = []

    async def _spy(urls, cfg, reg, log):
        seen.append(reg)
        return "", []

    monkeypatch.setattr("harness.tools.fetch._fetch", _spy)

    tool_sets = build_tools(config, registry)
    by_name = {
        tool.name: tool for tool in [*tool_sets.lead, *tool_sets.researcher, *tool_sets.reader]
    }
    await by_name["fetch_pages"].ainvoke({"urls": ["https://example.test/a"]})

    assert len(seen) == 1
    assert seen[0] is registry


def test_tools_are_langchain_base_tools_with_content_and_artifact(make_config):
    config = make_config()

    tool_sets = build_tools(config, SourceRegistry())

    for tool in [*tool_sets.lead, *tool_sets.researcher, *tool_sets.reader]:
        assert isinstance(tool, BaseTool)
        assert tool.response_format == "content_and_artifact"


def test_one_run_log_instance_reaches_every_tool_builder(make_config, monkeypatch):
    """The docstring's "ONE `run_log` is shared" claim, asserted rather than trusted.

    A second `RunLog()` built for any one builder would fragment the incidents the report
    and terminal disclose, and every existing test would still pass.
    """
    config = make_config()
    run_log = RunLog()
    seen: dict[str, object] = {}

    for name in ("build_search_tool", "build_fallback_tool", "build_fetch_tool"):
        real = getattr(harness.tools, name)

        def _spy(*args, _name=name, _real=real, **kwargs):
            seen[_name] = args[-1]
            return _real(*args, **kwargs)

        monkeypatch.setattr(f"harness.tools.{name}", _spy)

    build_tools(config, SourceRegistry(), run_log)

    assert set(seen) == {"build_search_tool", "build_fallback_tool", "build_fetch_tool"}
    assert all(log is run_log for log in seen.values())
