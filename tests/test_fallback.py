"""Behavioral tests for harness.tools.fallback (the `fetch_raw` recovery tool, D2/R2/R5)."""

from pathlib import Path

from langchain_core.tools import BaseTool

from harness.blocklist import load_blocklist
from harness.config import AgentSettings, BlocklistSettings
from harness.runlog import RunLog
from harness.sources import SourceRegistry, sources_dir
from harness.tools import fallback, fetch
from tests.conftest import _FakeMarkdown, _FakeResult, approve_all

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "injection"


def _attack_markdown() -> str:
    return (FIXTURES_DIR / "attack_instruction_override_ignore.txt").read_text(encoding="utf-8")


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
    approve_all(registry, ["https://a.test"])
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
    approve_all(registry, ["https://a.test"])
    results = [
        _FakeResult(
            "https://a.test", markdown=_FakeMarkdown(raw_markdown="A body", fit_markdown="A body")
        )
    ]
    install_crawler(results)
    fetch_raw = fallback.build_fallback_tool(config, registry)

    message = await fetch_raw.ainvoke(_tool_call(["https://a.test"], "some reason", "call-1"))

    source_id = message.artifact[0].source_id
    source_path = sources_dir(config, registry) / f"{source_id}.md"
    assert source_path.exists()
    assert "A body" in source_path.read_text(encoding="utf-8")


async def test_fetch_raw_mints_ids_via_the_shared_registry_continuing_the_sequence(
    install_crawler, make_config
):
    config = make_config()
    registry = SourceRegistry()
    approve_all(registry, ["https://one.test", "https://two.test", "https://three.test"])
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


async def test_fetch_raw_marks_successful_pages_fallback_and_mints_nothing_for_failures(  # R5
    install_crawler, make_config
):
    config = make_config()
    registry = SourceRegistry()
    approve_all(registry, ["https://ok.test", "https://bad.test"])
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
    # A failure mints no id at all (R5) — there is no registry entry to leave "unread".
    assert bad_page.source_id is None


async def test_fetch_raw_fences_the_body_nested_inside_the_undigested_wrapper(  # R4
    install_crawler, make_config
):
    """The wrapper stays outermost; the fence's boundary lines sit inside it."""
    config = make_config()
    registry = SourceRegistry()
    approve_all(registry, ["https://a.test"])
    results = [
        _FakeResult(
            "https://a.test", markdown=_FakeMarkdown(raw_markdown="A body", fit_markdown="A body")
        )
    ]
    install_crawler(results)
    fetch_raw = fallback.build_fallback_tool(config, registry)

    message = await fetch_raw.ainvoke(_tool_call(["https://a.test"], "some reason", "call-1"))

    content = message.content
    wrapper_start = content.index('<undigested source="S1"')
    wrapper_end = content.index("</undigested>")
    fence_open = content.index("<<<UNTRUSTED")
    fence_close = content.index("<<<END UNTRUSTED")

    assert wrapper_start < fence_open < fence_close < wrapper_end
    assert wrapper_start < content.index("A body") < wrapper_end


async def test_fetch_raw_never_downgrades_a_digested_source(install_crawler, make_config):
    """A source an earlier delegation already digested keeps its "digested" mode even when the
    lead re-fetches it raw (e.g. for a second facet): `mark_read` is last-write-wins, so an
    unconditional mark here would report a genuinely digested source as a failed-digestion
    fallback. The `<undigested>` wrapper still applies — it describes THIS payload being raw.
    """
    config = make_config()
    registry = SourceRegistry()
    approve_all(registry, ["https://a.test"])
    source_id = registry.add("https://a.test")
    registry.mark_read(source_id, "digested")
    install_crawler(
        [
            _FakeResult(
                "https://a.test",
                markdown=_FakeMarkdown(raw_markdown="A body", fit_markdown="A body"),
            )
        ]
    )
    fetch_raw = fallback.build_fallback_tool(config, registry)

    message = await fetch_raw.ainvoke(
        _tool_call(["https://a.test"], "re-read for a second facet", "call-1")
    )

    assert "<undigested" in message.content
    assert registry.get(source_id).read_mode == "digested"


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


async def test_fetch_raw_renders_a_guard_blocked_page_the_same_as_fetch_pages(  # R1/D1
    install_crawler, make_config, tmp_path
):
    """Proves the shared `_fetch` covers both surfaces: a blocked page mints no Sn, writes
    no capture file, renders the opaque rejection block, and records one `guard_blocked`
    incident, through `fetch_raw` exactly as through `fetch_pages`."""
    config = make_config(agent=AgentSettings(workspace_dir=tmp_path))
    registry = SourceRegistry()
    approve_all(registry, ["https://evil.test"])
    run_log = RunLog()
    attack_markdown = _attack_markdown()
    results = [
        _FakeResult(
            "https://evil.test",
            markdown=_FakeMarkdown(raw_markdown=attack_markdown, fit_markdown=attack_markdown),
        )
    ]
    install_crawler(results)
    fetch_raw = fallback.build_fallback_tool(config, registry, run_log)

    message = await fetch_raw.ainvoke(
        _tool_call(["https://evil.test"], "some reason", "call-guard-raw-1")
    )

    assert message.artifact == []
    assert registry.all() == []
    assert message.content == fetch._rejection_block("https://evil.test")
    assert list(sources_dir(config, registry).glob("*.md")) == []

    incidents = [i for i in run_log.incidents() if i.kind == "guard_blocked"]
    assert len(incidents) == 1
    assert "https://evil.test" in incidents[0].detail
    assert "instruction_override" in incidents[0].detail


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


# --- Phase 4: strict URL provenance (R2) -------------------------------------------------


async def test_fetch_raw_rejects_an_unapproved_url_before_any_crawl(install_crawler, make_config):
    """Same rejection behavior as `fetch_pages`, proving the shared `_fetch` covers both."""
    config = make_config()
    registry = SourceRegistry()  # deliberately nothing approved
    run_log = RunLog()
    fake_cls = install_crawler([])
    fetch_raw = fallback.build_fallback_tool(config, registry, run_log)

    message = await fetch_raw.ainvoke(
        _tool_call(["https://never-approved.test"], "some reason", "call-provenance-1")
    )

    assert message.artifact == []
    assert fake_cls.calls == []
    assert registry.all() == []

    incidents = [i for i in run_log.incidents() if i.kind == "provenance_rejected"]
    assert len(incidents) == 1
    assert "https://never-approved.test" in incidents[0].detail


# --- Visible, sticky fetch failures (R1/D1/D2) -------------------------------------------
#
# The block's exact wording is pinned once, by test_fetch.py's golden test; everything here
# asserts against `fetch._rejection_block` so no stale copy of the policy line can survive.


async def test_fetch_raw_replays_the_stored_verdict_with_zero_crawler_calls(
    install_crawler, make_config
):
    config = make_config()
    registry = SourceRegistry()
    approve_all(registry, ["https://blocked.test"])
    results = [_FakeResult("https://blocked.test", status_code=403, markdown=None)]
    fake_cls = install_crawler(results)

    await fetch._fetch(["https://blocked.test"], config, registry, RunLog())
    # R6/D2: a 403 with no body reads as thin (word count only) and escalates once through
    # the browser -- two calls, same "blocked" outcome either way.
    assert len(fake_cls.calls) == 2

    fetch_raw = fallback.build_fallback_tool(config, registry)
    message = await fetch_raw.ainvoke(
        _tool_call(["https://blocked.test"], "retrying the failed one", "call-1")
    )

    # Replayed from the sticky verdict: no further crawler calls.
    assert len(fake_cls.calls) == 2
    assert message.artifact == []
    # A genuine failure replays its rendered outcome, not the opaque policy block.
    assert "## https://blocked.test" in message.content
    assert "blocked — status 403" in message.content


async def test_fetch_raw_renders_an_opaque_rejection_for_an_unapproved_url(
    install_crawler, make_config
):
    config = make_config()
    registry = SourceRegistry()  # deliberately nothing approved
    fake_cls = install_crawler([])
    fetch_raw = fallback.build_fallback_tool(config, registry)

    message = await fetch_raw.ainvoke(
        _tool_call(["https://never-approved.test"], "some reason", "call-1")
    )

    assert fake_cls.calls == []
    assert message.artifact == []
    assert message.content == fetch._rejection_block("https://never-approved.test")


# --- Phase 3: persistent domain blocklist (R3/R4) -----------------------------------------


async def test_fetch_raw_hits_the_pre_crawl_blocklist_backstop_with_zero_crawler_calls(
    install_crawler, make_config, tmp_path
):
    """`fetch_raw` shares `_fetch`, so a blocklisted hostname hits the same pre-crawl
    backstop through `fetch_raw` as through `fetch_pages`, with zero crawler calls."""
    blocklist_path = tmp_path / "blocked-domains.json"
    load_blocklist(blocklist_path).add("walled.test", "403")
    config = make_config(blocklist=BlocklistSettings(path=blocklist_path))
    registry = SourceRegistry()
    approve_all(registry, ["https://walled.test/page"])
    fake_cls = install_crawler([])
    fetch_raw = fallback.build_fallback_tool(config, registry)

    message = await fetch_raw.ainvoke(
        _tool_call(["https://walled.test/page"], "some reason", "call-1")
    )

    assert fake_cls.calls == []
    assert message.artifact == []
    assert message.content == fetch._rejection_block("https://walled.test/page")
