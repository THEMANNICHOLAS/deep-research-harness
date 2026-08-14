"""Behavioral tests for harness.tools.fallback (the `fetch_raw` recovery tool, D2/R2/R5)."""

from langchain_core.tools import BaseTool

from harness.config import AgentSettings
from harness.sources import SourceRegistry
from harness.tools import fallback, fetch
from tests.conftest import _FakeMarkdown, _FakeResult


def _tool_call(urls: list[str], reason: str, call_id: str) -> dict:
    """Build a `ToolCall`-shaped dict for `fetch_raw.ainvoke`."""
    return {
        "name": "fetch_raw",
        "args": {"urls": urls, "reason": reason},
        "id": call_id,
        "type": "tool_call",
    }


async def test_fetch_raw_wraps_each_successful_page_in_the_undigested_marker(
    install_crawler, make_config
):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://a.test", markdown=_FakeMarkdown(raw_markdown="A body", fit_markdown="A body")
        )
    ]
    install_crawler(results)
    fetch_raw = fallback.build_fallback_tool(config, registry)

    message = await fetch_raw.ainvoke(
        _tool_call(["https://a.test"], "digestion timed out twice", "call-1")
    )

    assert '<undigested source="S1" reason="digestion timed out twice">' in message.content
    assert "</undigested>" in message.content
    assert "A body" in message.content


async def test_fetch_raw_still_writes_the_normal_capture_file(
    install_crawler, make_config, tmp_path
):
    config = make_config(agent=AgentSettings(workspace_dir=tmp_path))
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://a.test", markdown=_FakeMarkdown(raw_markdown="A body", fit_markdown="A body")
        )
    ]
    install_crawler(results)
    fetch_raw = fallback.build_fallback_tool(config, registry)

    message = await fetch_raw.ainvoke(_tool_call(["https://a.test"], "some reason", "call-1"))

    source_id = message.artifact[0].source_id
    source_path = fetch._sources_dir(config, registry) / f"{source_id}.md"
    assert source_path.exists()
    assert "A body" in source_path.read_text(encoding="utf-8")


async def test_fetch_raw_mints_ids_via_the_shared_registry_continuing_the_sequence(
    install_crawler, make_config
):
    config = make_config()
    registry = SourceRegistry()
    fetch_pages = fetch.build_fetch_tool(config, registry)

    install_crawler(
        [
            _FakeResult(
                "https://one.test", markdown=_FakeMarkdown(raw_markdown="one", fit_markdown="one")
            ),
            _FakeResult(
                "https://two.test", markdown=_FakeMarkdown(raw_markdown="two", fit_markdown="two")
            ),
        ]
    )
    await fetch_pages.ainvoke(
        {
            "name": "fetch_pages",
            "args": {"urls": ["https://one.test", "https://two.test"]},
            "id": "digest-1",
            "type": "tool_call",
        }
    )

    fetch_raw = fallback.build_fallback_tool(config, registry)
    install_crawler(
        [
            _FakeResult(
                "https://three.test",
                markdown=_FakeMarkdown(raw_markdown="three", fit_markdown="three"),
            )
        ]
    )
    message = await fetch_raw.ainvoke(
        _tool_call(["https://three.test"], "some reason", "call-fallback")
    )

    assert message.artifact[0].source_id == "S3"
    assert registry.get("S3") is not None


async def test_fetch_raw_marks_successful_pages_fallback_and_leaves_failures_unread(
    install_crawler, make_config
):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://ok.test", markdown=_FakeMarkdown(raw_markdown="ok", fit_markdown="ok")
        ),
        _FakeResult(
            "https://bad.test", status_code=500, error_message="server exploded", markdown=None
        ),
    ]
    install_crawler(results)
    fetch_raw = fallback.build_fallback_tool(config, registry)

    message = await fetch_raw.ainvoke(
        _tool_call(["https://ok.test", "https://bad.test"], "some reason", "call-mixed")
    )

    ok_page, bad_page = message.artifact
    assert registry.get(ok_page.source_id).read_mode == "fallback"
    assert registry.get(bad_page.source_id).read_mode == "unread"


async def test_a_call_over_the_url_limit_is_rejected_before_any_fetch(install_crawler, make_config):
    config = make_config()
    limit = config.fetch.max_urls_per_call
    registry = SourceRegistry()
    fake_cls = install_crawler([])
    fetch_raw = fallback.build_fallback_tool(config, registry)

    message = await fetch_raw.ainvoke(
        _tool_call(
            [f"https://over{n}.test" for n in range(1, limit + 2)], "some reason", "over-limit-1"
        )
    )

    assert message.status == "error"
    assert f"At most {limit} URLs" in message.content
    assert fake_cls.calls == []


async def test_fetch_raw_exposes_the_pinned_contract(make_config):
    config = make_config()
    registry = SourceRegistry()

    fetch_raw = fallback.build_fallback_tool(config, registry)

    assert isinstance(fetch_raw, BaseTool)
    assert fetch_raw.name == "fetch_raw"
    assert fetch_raw.response_format == "content_and_artifact"
    assert fetch_raw.description
    schema = fetch_raw.args_schema.model_json_schema()
    assert set(schema["properties"]) == {"urls", "reason"}
