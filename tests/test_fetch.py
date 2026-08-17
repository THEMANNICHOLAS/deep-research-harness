"""Behavioral tests for harness.tools.fetch."""

from pathlib import Path

import pytest
from crawl4ai import DefaultMarkdownGenerator, PruningContentFilter  # type: ignore[import-untyped]
from langchain_core.tools import BaseTool

from harness.config import AgentSettings, GuardSettings
from harness.runlog import RunLog
from harness.sources import SourceRegistry, sources_dir
from harness.tools import fetch
from tests.conftest import _FakeMarkdown, _FakeResult

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "injection"


def _attack_markdown() -> str:
    return (FIXTURES_DIR / "attack_instruction_override_ignore.txt").read_text(encoding="utf-8")


def _rendered(markdown: str, cap: int) -> str:
    """Render one fetched page's block through `_render`, for truncation assertions."""
    page = fetch.FetchedPage(
        source_id="S1",
        url="https://a.test",
        outcome="fetched",
        status_code=200,
        title=None,
        markdown=markdown,
        error=None,
    )
    return fetch._render(page, cap)


async def test_empty_url_list_returns_empty_content_and_artifact(install_crawler, make_config):
    config = make_config()
    registry = SourceRegistry()
    fake_cls = install_crawler([])

    content, pages = await fetch._fetch([], config, registry, RunLog())

    assert (content, pages) == ("", [])
    assert fake_cls.constructed_with == []


async def test_mixed_batch_returns_one_entry_per_url_with_successes_intact(
    install_crawler, make_config
):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://a.test",
            markdown=_FakeMarkdown(raw_markdown="A content", fit_markdown="A content"),
        ),
        _FakeResult(
            "https://b.test",
            error_message="boom",
            status_code=500,
            markdown=None,
        ),
        _FakeResult(
            "https://c.test",
            markdown=_FakeMarkdown(raw_markdown="C content", fit_markdown="C content"),
        ),
    ]
    install_crawler(results)

    content, pages = await fetch._fetch(
        ["https://a.test", "https://b.test", "https://c.test"], config, registry, RunLog()
    )

    assert len(pages) == 3
    assert [p.url for p in pages] == ["https://a.test", "https://b.test", "https://c.test"]
    assert pages[0].outcome == "fetched"
    assert pages[0].markdown == "A content"
    assert pages[1].outcome == "error"
    assert pages[2].outcome == "fetched"
    assert pages[2].markdown == "C content"


@pytest.mark.parametrize(
    ("status_code", "error_message", "content_type", "markdown", "expected"),
    [
        (403, None, "text/html", "content", "blocked"),
        (429, None, "text/html", "content", "blocked"),
        (503, None, "text/html", "content", "blocked"),
        (200, "Timeout after 15000ms", "text/html", "", "timeout"),
        (200, None, "application/pdf", "", "pdf"),
        (200, None, "text/html", "", "non_html"),
        (500, "internal server error", "text/html", "", "error"),
        (200, None, "text/html", "# Title\ncontent", "fetched"),
    ],
)
def test_classification_rules(status_code, error_message, content_type, markdown, expected):
    assert fetch.classify(status_code, error_message, content_type, markdown) == expected


async def test_equivalent_url_spellings_are_fetched_once_with_one_source_id(
    install_crawler, make_config
):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://dup.test/a",
            markdown=_FakeMarkdown(raw_markdown="one", fit_markdown="one"),
        ),
    ]
    fake_cls = install_crawler(results)

    content, pages = await fetch._fetch(
        ["https://dup.test/a", "https://dup.test/a/", "https://dup.test/a#frag"],
        config,
        registry,
        RunLog(),
    )

    # One crawl, one page, one heading: never two [Sn] blocks over one identity.
    assert fake_cls.calls[0].urls == ["https://dup.test/a"]
    assert len(pages) == 1
    assert registry.get(pages[0].source_id) is not None
    assert len(registry.all()) == 1
    assert content.count(f"## [{pages[0].source_id}]") == 1


async def test_merging_a_differently_shaped_url_records_a_disclosed_incident(
    install_crawler, make_config
):
    """A dropped spelling that names a different document form must not vanish silently.

    arxiv's abs and pdf paths canonicalize to one key, so asking for both fetches only the
    first — the abstract when it is listed first. That is a real coverage loss and the
    best-effort-plus-disclose invariant requires it reach the report, not just the log.
    """
    config = make_config()
    registry = SourceRegistry()
    run_log = RunLog()
    results = [
        _FakeResult(
            "https://arxiv.org/abs/2405.11111",
            markdown=_FakeMarkdown(raw_markdown="abstract", fit_markdown="abstract"),
        ),
    ]
    fake_cls = install_crawler(results)

    _, pages = await fetch._fetch(
        ["https://arxiv.org/abs/2405.11111", "https://arxiv.org/pdf/2405.11111.pdf"],
        config,
        registry,
        run_log,
    )

    assert fake_cls.calls[0].urls == ["https://arxiv.org/abs/2405.11111"]
    assert len(pages) == 1
    merged = [incident for incident in run_log.incidents() if incident.kind == "urls_merged"]
    assert len(merged) == 1
    assert "https://arxiv.org/pdf/2405.11111.pdf" in merged[0].detail
    assert "https://arxiv.org/abs/2405.11111" in merged[0].detail


async def test_merging_an_equivalent_spelling_records_no_incident(install_crawler, make_config):
    """Trailing slash, fragment and tracking params name the same document — nothing is lost.

    Recording those would bury the arxiv case above in noise on every ordinary run.
    """
    config = make_config()
    run_log = RunLog()
    results = [
        _FakeResult(
            "https://dup.test/a",
            markdown=_FakeMarkdown(raw_markdown="one", fit_markdown="one"),
        ),
    ]
    install_crawler(results)

    await fetch._fetch(
        ["https://dup.test/a", "https://dup.test/a/#frag", "https://dup.test/a?utm_source=x"],
        config,
        SourceRegistry(),
        run_log,
    )

    assert [incident for incident in run_log.incidents() if incident.kind == "urls_merged"] == []


async def test_content_is_truncated_at_the_cap_but_artifact_keeps_full_text(
    install_crawler, make_config
):
    cap = 50
    config = make_config(per_page_char_cap=cap)
    registry = SourceRegistry()
    long_markdown = "x" * 500
    results = [
        _FakeResult(
            "https://long.test",
            markdown=_FakeMarkdown(raw_markdown=long_markdown, fit_markdown=long_markdown),
        )
    ]
    install_crawler(results)

    content, pages = await fetch._fetch(["https://long.test"], config, registry, RunLog())

    assert len(content) < len(long_markdown)
    assert str(cap) in content
    assert pages[0].markdown == long_markdown


def test_text_at_or_under_the_cap_is_returned_unchanged():
    cap = 100

    at_cap = _rendered("A" * 100, cap)
    under_cap = _rendered("A" * 40, cap)

    assert "A" * 100 in at_cap
    assert "truncated" not in at_cap
    assert "A" * 40 in under_cap
    assert "truncated" not in under_cap


def test_over_cap_text_ends_at_the_latest_paragraph_break():
    cap = 100
    text = "A" * 40 + "\n\n" + "B" * 40 + "\n\n" + "C" * 100

    rendered = _rendered(text, cap)

    assert "C" not in rendered
    assert ("A" * 40 + "\n\n" + "B" * 40) in rendered
    assert str(cap) in rendered


def test_a_heading_start_is_a_valid_truncation_boundary():
    cap = 100
    text = "A" * 70 + "\n# Later heading\n" + "B" * 100

    rendered = _rendered(text, cap)

    assert "# Later heading" not in rendered
    assert "A" * 70 in rendered


def test_text_with_no_boundary_before_the_cap_falls_back_to_a_hard_cut():
    cap = 100
    text = "A" * 500

    rendered = _rendered(text, cap)

    assert "A" * 100 in rendered
    assert "A" * 101 not in rendered
    assert "truncated" in rendered
    assert str(cap) in rendered


def test_an_early_boundary_is_taken_even_though_it_discards_most_of_the_allowance():
    # There is no minimum-boundary floor: the latest boundary always wins.
    cap = 100
    text = "A" * 10 + "\n\n" + "B" * 200

    rendered = _rendered(text, cap)

    assert "A" * 10 in rendered
    assert "B" not in rendered
    assert "truncated" in rendered


def test_a_heading_at_the_very_start_does_not_empty_the_block():
    cap = 100
    text = "# Title\n" + "A" * 200

    rendered = _rendered(text, cap)

    assert "# Title" in rendered
    assert "A" * 92 in rendered
    assert "truncated" in rendered


async def test_content_has_a_heading_for_every_url_including_failures(install_crawler, make_config):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://ok.test",
            markdown=_FakeMarkdown(raw_markdown="fine", fit_markdown="fine"),
        ),
        _FakeResult(
            "https://fail.test",
            error_message="server exploded",
            status_code=500,
            markdown=None,
        ),
    ]
    install_crawler(results)

    content, pages = await fetch._fetch(
        ["https://ok.test", "https://fail.test"], config, registry, RunLog()
    )

    for page in pages:
        # A failure has no `source_id` (R5) and renders by URL alone.
        heading = f"## [{page.source_id}] {page.url}" if page.source_id else f"## {page.url}"
        assert heading in content
        assert page.outcome in content


async def test_config_limits_reach_the_crawl4ai_call(install_crawler, make_config):
    config = make_config(page_timeout_ms=1234, max_concurrency=3)
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://a.test",
            markdown=_FakeMarkdown(raw_markdown="a", fit_markdown="a"),
        )
    ]
    fake_cls = install_crawler(results)

    await fetch._fetch(["https://a.test"], config, registry, RunLog())

    assert len(fake_cls.calls) == 1
    recorded = fake_cls.calls[0]
    assert recorded.config.page_timeout == 1234
    assert recorded.dispatcher.max_session_permit == 3


async def test_dispatcher_is_memory_bounded_and_rate_limited(install_crawler, make_config):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult("https://a.test", markdown=_FakeMarkdown(raw_markdown="a", fit_markdown="a"))
    ]
    fake_cls = install_crawler(results)

    await fetch._fetch(["https://a.test"], config, registry, RunLog())

    dispatcher = fake_cls.calls[0].dispatcher
    # 75%, not crawl4ai's 90% default: each permit is a real browser page.
    assert dispatcher.memory_threshold_percent == 75.0
    # Not a retry count: 0.9.2 re-fetches nothing on a 429/503. It caps how many times a domain's
    # backoff delay doubles, and that sleep holds a concurrency permit.
    assert dispatcher.rate_limiter is not None
    assert dispatcher.rate_limiter.max_retries == 1


async def test_crawl4ai_logging_is_silenced_on_both_configs(install_crawler, make_config):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult("https://a.test", markdown=_FakeMarkdown(raw_markdown="a", fit_markdown="a"))
    ]
    fake_cls = install_crawler(results)

    await fetch._fetch(["https://a.test"], config, registry, RunLog())

    # crawl4ai defaults `verbose` to True on both configs and prints into our process.
    assert fake_cls.calls[0].config.verbose is False
    assert fake_cls.constructed_with[0].verbose is False


async def test_boilerplate_stripping_config_reaches_the_crawl4ai_call(install_crawler, make_config):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult("https://a.test", markdown=_FakeMarkdown(raw_markdown="a", fit_markdown="a"))
    ]
    fake_cls = install_crawler(results)

    await fetch._fetch(["https://a.test"], config, registry, RunLog())

    recorded = fake_cls.calls[0].config
    assert set(recorded.excluded_tags) >= {"nav", "header", "footer", "aside", "script"}
    assert isinstance(recorded.markdown_generator, DefaultMarkdownGenerator)
    assert isinstance(recorded.markdown_generator.content_filter, PruningContentFilter)


async def test_pdf_batch_gets_the_same_boilerplate_stripping_as_the_html_batch(
    install_crawler, make_config
):
    """Without an explicit generator crawl4ai falls back to an unfiltered default.

    That left running headers, footers and page numbers in every PDF source, against this
    module's own "boilerplate-stripped markdown" contract.
    """
    config = make_config()
    results = [
        _FakeResult(
            "https://a.test/paper.pdf",
            markdown=_FakeMarkdown(raw_markdown="text", fit_markdown="text"),
        )
    ]
    fake_cls = install_crawler(results)

    await fetch._fetch(["https://a.test/paper.pdf"], config, SourceRegistry(), RunLog())

    recorded = fake_cls.calls[0].config
    assert isinstance(recorded.markdown_generator, DefaultMarkdownGenerator)
    assert isinstance(recorded.markdown_generator.content_filter, PruningContentFilter)


async def test_built_tool_exposes_the_pinned_contract_and_returns_content_and_artifact(
    install_crawler, make_config
):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://tool.test",
            metadata={"title": "Tool Page"},
            markdown=_FakeMarkdown(raw_markdown="raw body", fit_markdown="clean body"),
        )
    ]
    install_crawler(results)

    fetch_pages = fetch.build_fetch_tool(config, registry)

    assert isinstance(fetch_pages, BaseTool)
    assert fetch_pages.name == "fetch_pages"
    assert fetch_pages.response_format == "content_and_artifact"
    assert fetch_pages.description
    schema = fetch_pages.args_schema.model_json_schema()
    assert set(schema["properties"]) == {"urls"}
    assert schema["properties"]["urls"]["type"] == "array"

    # D1: tools are driven with `ainvoke`; the ToolCall form is what surfaces the artifact.
    message = await fetch_pages.ainvoke(
        {
            "name": "fetch_pages",
            "args": {"urls": ["https://tool.test"]},
            "id": "live-check-1",
            "type": "tool_call",
        }
    )

    assert "## [S1] https://tool.test" in message.content
    assert [page.url for page in message.artifact] == ["https://tool.test"]
    assert message.artifact[0].markdown == "clean body"
    assert message.artifact[0].title == "Tool Page"


async def test_fit_markdown_is_preferred_over_raw_markdown(install_crawler, make_config):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://pruned.test",
            markdown=_FakeMarkdown(
                raw_markdown="Home About Contact The real article body. Footer links",
                fit_markdown="The real article body.",
            ),
        )
    ]
    install_crawler(results)

    content, pages = await fetch._fetch(["https://pruned.test"], config, registry, RunLog())

    assert pages[0].markdown == "The real article body."
    assert "Footer links" not in content


async def test_raw_markdown_is_used_when_fit_markdown_is_empty(install_crawler, make_config):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://unpruned.test",
            markdown=_FakeMarkdown(raw_markdown="Only raw survived.", fit_markdown=""),
        )
    ]
    install_crawler(results)

    _, pages = await fetch._fetch(["https://unpruned.test"], config, registry, RunLog())

    assert pages[0].markdown == "Only raw survived."
    assert pages[0].outcome == "fetched"


async def test_title_from_result_metadata_reaches_the_page_and_the_registry(
    install_crawler, make_config
):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://titled.test",
            metadata={"title": "An Article Title"},
            markdown=_FakeMarkdown(raw_markdown="body", fit_markdown="body"),
        )
    ]
    install_crawler(results)

    _, pages = await fetch._fetch(["https://titled.test"], config, registry, RunLog())

    assert pages[0].title == "An Article Title"
    source = registry.get(pages[0].source_id)
    assert source is not None
    assert source.title == "An Article Title"


async def test_content_type_header_on_the_result_reroutes_through_the_pdf_batch(
    install_crawler, make_config
):
    # R2: an extensionless PDF URL used to degrade silently to `non_html`. Now the
    # Playwright result's `application/pdf` header (exercised via `_content_type`'s
    # case-insensitive lookup, which the `classify()` unit tests bypass with a plain string)
    # reroutes it through the PDF batch instead, landing a real fetched capture.
    config = make_config()
    registry = SourceRegistry()
    playwright_results = [
        _FakeResult(
            "https://pdf.test/doc",
            response_headers={"Content-Type": "application/pdf"},
            markdown=_FakeMarkdown(raw_markdown="%PDF junk", fit_markdown="%PDF junk"),
        )
    ]
    pdf_results = [
        _FakeResult(
            "https://pdf.test/doc",
            markdown=_FakeMarkdown(raw_markdown="Extracted text", fit_markdown="Extracted text"),
        )
    ]
    fake_cls = install_crawler(playwright_results, pdf_results=pdf_results)

    _, pages = await fetch._fetch(["https://pdf.test/doc"], config, registry, RunLog())

    assert [call.is_pdf for call in fake_cls.calls] == [False, True]
    assert pages[0].outcome == "fetched"
    assert pages[0].markdown == "Extracted text"


async def test_input_url_with_no_result_reports_a_single_error_outcome(
    install_crawler, make_config
):
    config = make_config()
    registry = SourceRegistry()
    install_crawler([])

    _, pages = await fetch._fetch(["https://a.test"], config, registry, RunLog())

    # The `None`-pairing branch: it must survive the removal of the positional fallback.
    assert len(pages) == 1
    assert pages[0].url == "https://a.test"
    assert pages[0].outcome == "error"
    assert pages[0].markdown == ""
    assert pages[0].error == "no result returned for this URL"


async def test_the_first_of_two_results_for_one_url_is_the_one_reported(
    install_crawler, make_config
):
    # Pins `_pair`'s `bucket.pop(0)`: under memory pressure the dispatcher can return a
    # "Requeued" placeholder AND re-queue the crawl, and the first result wins.
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult("https://a.test", error_message="Requeued", status_code=None),
        _FakeResult(
            "https://a.test",
            markdown=_FakeMarkdown(raw_markdown="retry body", fit_markdown="retry body"),
        ),
    ]
    install_crawler(results)

    content, pages = await fetch._fetch(["https://a.test"], config, registry, RunLog())

    assert len(pages) == 1
    assert pages[0].outcome == "error"
    assert pages[0].error == "Requeued"
    assert "retry body" not in content


async def test_result_matching_no_input_url_never_supplies_another_urls_body(
    install_crawler, make_config
):
    # A visible `error` is safer than pairing an unrelated result to an input URL positionally,
    # which would produce a plausible wrong citation.
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://redirected.test/final",
            markdown=_FakeMarkdown(
                raw_markdown="redirected content", fit_markdown="redirected content"
            ),
        )
    ]
    install_crawler(results)

    content, pages = await fetch._fetch(["https://original.test/start"], config, registry, RunLog())

    assert len(pages) == 1  # R6 — exactly one outcome per input URL
    assert pages[0].url == "https://original.test/start"
    assert pages[0].outcome == "error"
    assert pages[0].markdown == ""
    assert "redirected content" not in content


def _tool_call(urls: list[str], call_id: str) -> dict:
    """Build a `ToolCall`-shaped dict for `fetch_pages.ainvoke`."""
    return {"name": "fetch_pages", "args": {"urls": urls}, "id": call_id, "type": "tool_call"}


async def test_a_call_over_the_url_limit_is_rejected_before_any_fetch(install_crawler, make_config):
    # R7/D2: the schema rejects the call before any fetch, and the rejection returns as a
    # recoverable tool message rather than an exception, so the caller can resend fewer URLs.
    config = make_config()
    limit = config.fetch.max_urls_per_call
    registry = SourceRegistry()
    fake_cls = install_crawler([])
    fetch_pages = fetch.build_fetch_tool(config, registry)

    message = await fetch_pages.ainvoke(
        _tool_call([f"https://over{n}.test" for n in range(1, limit + 2)], "over-limit-1")
    )

    assert message.status == "error"
    # The cap is what rejected the call, not some unrelated validation failure.
    assert f"at most {limit} items" in message.content
    assert f"At most {limit} URLs" in message.content
    assert fake_cls.calls == []


async def test_duplicate_urls_still_count_toward_the_limit(install_crawler, make_config):
    # The limit counts URLs as submitted, not after deduplication: the cap lives in the schema,
    # which runs before `_fetch` dedups, so six URLs of which two are duplicates is rejected.
    config = make_config(max_urls_per_call=5)
    registry = SourceRegistry()
    fake_cls = install_crawler([])
    fetch_pages = fetch.build_fetch_tool(config, registry)

    message = await fetch_pages.ainvoke(
        _tool_call(
            [
                "https://dup.test/a",
                "https://dup.test/b",
                "https://dup.test/c",
                "https://dup.test/d",
                "https://dup.test/a/",  # same page as /a once normalized
                "https://dup.test/b#frag",  # same page as /b once normalized
            ],
            "over-limit-dupes",
        )
    )

    assert message.status == "error"
    assert fake_cls.calls == []


async def test_a_malformed_call_is_reported_as_itself_not_as_an_over_limit_call(
    install_crawler, make_config
):
    # The `handle_validation_error` hook swallows EVERY validation failure for this tool, so a
    # fixed over-limit string would make a wrong type look like a too-long list.
    config = make_config()
    registry = SourceRegistry()
    fake_cls = install_crawler([])
    fetch_pages = fetch.build_fetch_tool(config, registry)

    message = await fetch_pages.ainvoke(
        {
            "name": "fetch_pages",
            "args": {"urls": "https://not-a-list.test"},
            "id": "malformed-1",
            "type": "tool_call",
        }
    )

    assert message.status == "error"
    assert "valid list" in message.content
    assert fake_cls.calls == []


def test_both_prose_surfaces_state_the_url_limit(make_config):
    config = make_config()
    registry = SourceRegistry()
    fetch_pages = fetch.build_fetch_tool(config, registry)

    expected = str(config.fetch.max_urls_per_call)

    assert expected in fetch_pages.description
    schema = fetch_pages.args_schema.model_json_schema()
    assert expected in schema["properties"]["urls"]["description"]


def test_the_schema_url_limit_follows_the_configured_value(make_config):
    registry = SourceRegistry()
    low_config = make_config(max_urls_per_call=2)
    high_config = make_config(max_urls_per_call=7)

    low_tool = fetch.build_fetch_tool(low_config, registry)
    high_tool = fetch.build_fetch_tool(high_config, registry)

    low_schema = low_tool.args_schema.model_json_schema()
    high_schema = high_tool.args_schema.model_json_schema()

    assert low_schema["properties"]["urls"]["maxItems"] == 2
    assert high_schema["properties"]["urls"]["maxItems"] == 7


async def test_a_call_at_exactly_the_limit_fetches_every_url(install_crawler, make_config):
    # Survival guard: proves the cap does not break the at-limit case.
    config = make_config(max_urls_per_call=2)
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://limit1.test",
            markdown=_FakeMarkdown(raw_markdown="one", fit_markdown="one"),
        ),
        _FakeResult(
            "https://limit2.test",
            markdown=_FakeMarkdown(raw_markdown="two", fit_markdown="two"),
        ),
    ]
    install_crawler(results)
    fetch_pages = fetch.build_fetch_tool(config, registry)

    message = await fetch_pages.ainvoke(
        _tool_call(["https://limit1.test", "https://limit2.test"], "at-limit-1")
    )

    assert [page.url for page in message.artifact] == [
        "https://limit1.test",
        "https://limit2.test",
    ]
    assert "## [S1] https://limit1.test" in message.content
    assert "## [S2] https://limit2.test" in message.content


async def test_each_fetched_page_writes_its_source_file(install_crawler, make_config, tmp_path):
    config = make_config(agent=AgentSettings(workspace_dir=tmp_path))
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://article.test",
            metadata={"title": "An Article"},
            markdown=_FakeMarkdown(raw_markdown="full body text", fit_markdown="full body text"),
        )
    ]
    install_crawler(results)
    fetch_pages = fetch.build_fetch_tool(config, registry)

    message = await fetch_pages.ainvoke(_tool_call(["https://article.test"], "call-source-1"))

    source_id = message.artifact[0].source_id
    source_path = sources_dir(config, registry) / f"{source_id}.md"
    assert source_path.exists()
    text = source_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == f"# {source_id}: An Article"
    assert "https://article.test" in text
    assert "full body text" in text


async def test_source_file_text_is_untruncated_even_when_the_render_is_capped(
    install_crawler, make_config, tmp_path
):
    cap = 20
    long_markdown = "y" * 500
    config = make_config(per_page_char_cap=cap, agent=AgentSettings(workspace_dir=tmp_path))
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://long.test",
            markdown=_FakeMarkdown(raw_markdown=long_markdown, fit_markdown=long_markdown),
        )
    ]
    install_crawler(results)
    fetch_pages = fetch.build_fetch_tool(config, registry)

    message = await fetch_pages.ainvoke(_tool_call(["https://long.test"], "call-source-2"))

    assert "truncated" in message.content
    assert long_markdown not in message.content

    source_id = message.artifact[0].source_id
    text = (sources_dir(config, registry) / f"{source_id}.md").read_text(encoding="utf-8")
    assert long_markdown in text


async def test_a_mixed_batch_writes_content_for_successes_and_nothing_for_failures(
    install_crawler, make_config, tmp_path
):
    config = make_config(agent=AgentSettings(workspace_dir=tmp_path))
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://ok.test",
            markdown=_FakeMarkdown(raw_markdown="ok body", fit_markdown="ok body"),
        ),
        _FakeResult(
            "https://bad.test",
            status_code=500,
            error_message="server exploded",
            markdown=None,
        ),
    ]
    install_crawler(results)
    fetch_pages = fetch.build_fetch_tool(config, registry)

    message = await fetch_pages.ainvoke(
        _tool_call(["https://ok.test", "https://bad.test"], "call-mixed")
    )

    ok_page, bad_page = message.artifact
    captures_dir = sources_dir(config, registry)
    ok_text = (captures_dir / f"{ok_page.source_id}.md").read_text(encoding="utf-8")

    assert ok_text.splitlines()[0] == f"# {ok_page.source_id}: https://ok.test"
    assert "ok body" in ok_text
    assert bad_page.source_id is None
    assert list(captures_dir.glob("*.md")) == [captures_dir / f"{ok_page.source_id}.md"]


async def test_one_source_write_failure_does_not_poison_the_batch_or_go_silent(
    install_crawler, make_config, tmp_path, monkeypatch
):
    config = make_config(agent=AgentSettings(workspace_dir=tmp_path))
    registry = SourceRegistry()
    run_log = RunLog()
    results = [
        _FakeResult(
            "https://a.test",
            markdown=_FakeMarkdown(raw_markdown="a body", fit_markdown="a body"),
        ),
        _FakeResult(
            "https://b.test",
            markdown=_FakeMarkdown(raw_markdown="b body", fit_markdown="b body"),
        ),
    ]
    install_crawler(results)
    fetch_pages = fetch.build_fetch_tool(config, registry, run_log)

    # A fresh registry mints IDs in input order, so the first URL is S1 and the second S2 —
    # predictable before the call, which is what lets exactly one write be targeted.
    failing_path = sources_dir(config, registry) / "S1.md"
    ok_path = sources_dir(config, registry) / "S2.md"
    real_write_text = Path.write_text

    def raising_write_text(self: Path, *args: object, **kwargs: object) -> int:
        if self == failing_path:
            raise OSError("disk full")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", raising_write_text)

    message = await fetch_pages.ainvoke(
        _tool_call(["https://a.test", "https://b.test"], "call-write-fail")
    )

    # The batch was not failed: both pages still come back.
    assert [page.url for page in message.artifact] == ["https://a.test", "https://b.test"]
    # And one failure did not poison the rest: the second file was still written.
    assert not failing_path.exists()
    assert ok_path.exists()

    # Disclosed, not just printed: the incident carries the failed source and URL, so the
    # report's gaps section and the terminal can both name what was lost.
    incidents = run_log.incidents()
    assert [incident.kind for incident in incidents] == ["capture_write_failed"]
    assert "S1" in incidents[0].detail
    assert "https://a.test" in incidents[0].detail


async def test_refetching_the_same_url_overwrites_the_same_source_file(
    install_crawler, make_config, tmp_path
):
    config = make_config(agent=AgentSettings(workspace_dir=tmp_path))
    registry = SourceRegistry()
    fetch_pages = fetch.build_fetch_tool(config, registry)

    install_crawler(
        [
            _FakeResult(
                "https://re.test",
                markdown=_FakeMarkdown(raw_markdown="first", fit_markdown="first"),
            )
        ]
    )
    first_message = await fetch_pages.ainvoke(_tool_call(["https://re.test"], "call-refetch-1"))
    first_id = first_message.artifact[0].source_id

    install_crawler(
        [
            _FakeResult(
                "https://re.test",
                markdown=_FakeMarkdown(raw_markdown="second", fit_markdown="second"),
            )
        ]
    )
    second_message = await fetch_pages.ainvoke(_tool_call(["https://re.test"], "call-refetch-2"))
    second_id = second_message.artifact[0].source_id

    assert first_id == second_id
    captures_dir = sources_dir(config, registry)
    assert list(captures_dir.glob(f"{first_id}*.md")) == [captures_dir / f"{first_id}.md"]
    text = (captures_dir / f"{first_id}.md").read_text(encoding="utf-8")
    assert "second" in text
    assert "first" not in text


async def test_a_failed_refetch_does_not_overwrite_a_successful_capture(  # R5
    install_crawler, make_config, tmp_path, monkeypatch
):
    """A transient failure on a URL already captured must not destroy the evidence. Under the
    new convention a failed fetch writes no file at all, so the guard is now that no write is
    even attempted against the existing capture — not merely that its content survives.
    """
    config = make_config(agent=AgentSettings(workspace_dir=tmp_path))
    registry = SourceRegistry()
    fetch_pages = fetch.build_fetch_tool(config, registry)

    install_crawler(
        [
            _FakeResult(
                "https://flaky.test",
                markdown=_FakeMarkdown(raw_markdown="real body", fit_markdown="real body"),
            )
        ]
    )
    first = await fetch_pages.ainvoke(_tool_call(["https://flaky.test"], "call-good"))
    source_id = first.artifact[0].source_id
    captures_dir = sources_dir(config, registry)
    captured_path = captures_dir / f"{source_id}.md"

    real_write_text = Path.write_text

    def guarded_write_text(self: Path, *args: object, **kwargs: object) -> int:
        assert self != captured_path, "a failed refetch must never write the existing capture"
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", guarded_write_text)

    install_crawler([_FakeResult("https://flaky.test", status_code=429, markdown=None)])
    second = await fetch_pages.ainvoke(_tool_call(["https://flaky.test"], "call-blocked"))

    # The model still learns the refetch was blocked: only the captured file is spared.
    assert second.artifact[0].outcome == "blocked"
    assert second.artifact[0].source_id is None
    assert "real body" in captured_path.read_text(encoding="utf-8")


async def test_a_successful_refetch_after_an_earlier_failure_writes_a_normal_capture(  # R5
    install_crawler, make_config, tmp_path
):
    """A failure leaves nothing to replace: the first (failed) call wrote no file at all, so
    the later success just mints a fresh id and writes a normal capture.
    """
    config = make_config(agent=AgentSettings(workspace_dir=tmp_path))
    registry = SourceRegistry()
    fetch_pages = fetch.build_fetch_tool(config, registry)

    install_crawler([_FakeResult("https://flaky.test", status_code=429, markdown=None)])
    first = await fetch_pages.ainvoke(_tool_call(["https://flaky.test"], "call-blocked"))
    assert first.artifact[0].source_id is None

    install_crawler(
        [
            _FakeResult(
                "https://flaky.test",
                markdown=_FakeMarkdown(raw_markdown="real body", fit_markdown="real body"),
            )
        ]
    )
    second = await fetch_pages.ainvoke(_tool_call(["https://flaky.test"], "call-good"))
    source_id = second.artifact[0].source_id

    assert source_id is not None
    captures_dir = sources_dir(config, registry)
    text = (captures_dir / f"{source_id}.md").read_text(encoding="utf-8")
    assert "real body" in text
    assert list(captures_dir.glob("*.md")) == [captures_dir / f"{source_id}.md"]


# --- R5: identity-model migration — a failed fetch mints no id and writes no file ------


@pytest.mark.parametrize(
    ("outcome", "result_kwargs", "no_result"),
    [
        ("blocked", {"status_code": 403}, False),
        ("timeout", {"error_message": "Timeout after 15000ms", "status_code": 200}, False),
        ("non_html", {"response_headers": {"Content-Type": "application/octet-stream"}}, False),
        ("error", {"status_code": 500, "error_message": "internal server error"}, False),
        ("error", {}, True),
    ],
)
async def test_a_failed_fetch_mints_no_source_id_writes_no_file_and_renders_by_url(  # R5
    install_crawler, make_config, tmp_path, outcome, result_kwargs, no_result
):
    """A failed fetch (blocked/timeout/non_html/error, or a missing result) is never evidence:
    it gets no `[Sn]` id, no capture file, and renders by URL alone — the report/verify surfaces
    must be able to say "content is real" from "a capture file exists" with no exceptions.
    """
    config = make_config(agent=AgentSettings(workspace_dir=tmp_path))
    registry = SourceRegistry()
    run_log = RunLog()
    url = "https://fail.test"
    if no_result:
        install_crawler([])
    else:
        page_text = "should never reach a capture file"
        install_crawler(
            [
                _FakeResult(
                    url,
                    markdown=_FakeMarkdown(raw_markdown=page_text, fit_markdown=page_text),
                    **result_kwargs,
                )
            ]
        )
    fetch_pages = fetch.build_fetch_tool(config, registry, run_log)

    message = await fetch_pages.ainvoke(_tool_call([url], f"call-nomint-{outcome}-{no_result}"))

    page = message.artifact[0]
    assert page.outcome == outcome
    assert page.source_id is None
    assert registry.all() == []
    captures_dir = sources_dir(config, registry)
    assert list(captures_dir.glob("*.md")) == []
    assert url in message.content
    assert outcome in message.content
    assert "[S" not in message.content

    fetch_incidents = [i for i in run_log.incidents() if i.kind == "fetch_failed"]
    assert len(fetch_incidents) == 1
    assert url in fetch_incidents[0].detail
    assert "[S" not in fetch_incidents[0].detail


async def test_a_successful_fetch_mints_sn_writes_capture_and_renders_sn_heading(  # R5
    install_crawler, make_config, tmp_path
):
    """Unchanged today-behavior pin: a `fetched` page still gets a normal `[Sn]` id, a full-text
    capture file, and an `## [Sn] url` heading.
    """
    config = make_config(agent=AgentSettings(workspace_dir=tmp_path))
    registry = SourceRegistry()
    url = "https://ok.test"
    install_crawler(
        [_FakeResult(url, markdown=_FakeMarkdown(raw_markdown="ok body", fit_markdown="ok body"))]
    )
    fetch_pages = fetch.build_fetch_tool(config, registry)

    message = await fetch_pages.ainvoke(_tool_call([url], "call-success"))

    page = message.artifact[0]
    assert page.outcome == "fetched"
    assert page.source_id is not None
    assert registry.get(page.source_id) is not None
    assert f"## [{page.source_id}] {url}" in message.content
    captures_dir = sources_dir(config, registry)
    text = (captures_dir / f"{page.source_id}.md").read_text(encoding="utf-8")
    assert text.splitlines()[0] == f"# {page.source_id}: {url}"
    assert "ok body" in text


async def test_a_url_that_fails_then_succeeds_later_mints_a_fresh_sn_normally(  # R5
    install_crawler, make_config, tmp_path
):
    """A URL that fails on one call and succeeds on a later one is treated as if it had never
    been attempted: the failure minted nothing, so the later success gets a normal, fresh `Sn`.
    """
    config = make_config(agent=AgentSettings(workspace_dir=tmp_path))
    registry = SourceRegistry()
    run_log = RunLog()
    url = "https://retry.test"
    fetch_pages = fetch.build_fetch_tool(config, registry, run_log)

    install_crawler([_FakeResult(url, status_code=500, error_message="boom", markdown=None)])
    first = await fetch_pages.ainvoke(_tool_call([url], "call-retry-1"))

    assert first.artifact[0].source_id is None
    assert registry.all() == []

    install_crawler(
        [_FakeResult(url, markdown=_FakeMarkdown(raw_markdown="finally", fit_markdown="finally"))]
    )
    second = await fetch_pages.ainvoke(_tool_call([url], "call-retry-2"))

    page = second.artifact[0]
    assert page.outcome == "fetched"
    assert page.source_id == "S1"
    captures_dir = sources_dir(config, registry)
    text = (captures_dir / "S1.md").read_text(encoding="utf-8")
    assert "finally" in text
    assert list(captures_dir.glob("*.md")) == [captures_dir / "S1.md"]


async def test_pdf_extension_url_is_routed_to_the_pdf_crawler_and_lands_fetched(
    install_crawler, make_config, tmp_path
):
    config = make_config(agent=AgentSettings(workspace_dir=tmp_path))
    registry = SourceRegistry()
    pdf_results = [
        _FakeResult(
            "https://docs.test/report.pdf",
            markdown=_FakeMarkdown(
                raw_markdown="Extracted PDF text", fit_markdown="Extracted PDF text"
            ),
        )
    ]
    fake_cls = install_crawler([], pdf_results=pdf_results)
    fetch_pages = fetch.build_fetch_tool(config, registry)

    message = await fetch_pages.ainvoke(
        _tool_call(["https://docs.test/report.pdf"], "call-pdf-ext")
    )

    # The Playwright batch never ran: the fake recorded exactly one call, via the PDF seam.
    assert len(fake_cls.calls) == 1
    assert fake_cls.calls[0].is_pdf is True
    assert fake_cls.calls[0].urls == ["https://docs.test/report.pdf"]

    page = message.artifact[0]
    assert page.outcome == "fetched"
    assert page.markdown == "Extracted PDF text"
    text = (sources_dir(config, registry) / f"{page.source_id}.md").read_text(encoding="utf-8")
    assert "- Outcome: fetched" in text
    assert "Extracted PDF text" in text


async def test_empty_pdf_extraction_mints_no_id_and_never_fetched_or_non_html(  # R5
    install_crawler, make_config, tmp_path
):
    config = make_config(agent=AgentSettings(workspace_dir=tmp_path))
    registry = SourceRegistry()
    pdf_results = [
        _FakeResult(
            "https://docs.test/empty.pdf", markdown=_FakeMarkdown(raw_markdown="", fit_markdown="")
        )
    ]
    install_crawler([], pdf_results=pdf_results)
    fetch_pages = fetch.build_fetch_tool(config, registry)

    message = await fetch_pages.ainvoke(
        _tool_call(["https://docs.test/empty.pdf"], "call-empty-pdf")
    )

    page = message.artifact[0]
    # Exactly "error": `classify` is deterministic for an empty extraction, so accepting
    # "blocked" or "timeout" too would let a real reclassification pass unnoticed.
    assert page.outcome == "error"
    assert page.source_id is None
    assert list(sources_dir(config, registry).glob("*.md")) == []


@pytest.mark.parametrize(
    ("result_kwargs", "expected_outcome"),
    [
        ({"status_code": 500, "error_message": "internal server error"}, "error"),
        ({"status_code": 403}, "blocked"),
    ],
)
async def test_pdf_batch_failure_mints_no_id_and_writes_no_file(  # R5
    install_crawler, make_config, tmp_path, result_kwargs, expected_outcome
):
    config = make_config(agent=AgentSettings(workspace_dir=tmp_path))
    registry = SourceRegistry()
    pdf_results = [_FakeResult("https://docs.test/broken.pdf", markdown=None, **result_kwargs)]
    install_crawler([], pdf_results=pdf_results)
    fetch_pages = fetch.build_fetch_tool(config, registry)

    message = await fetch_pages.ainvoke(
        _tool_call(["https://docs.test/broken.pdf"], f"call-pdf-fail-{expected_outcome}")
    )

    page = message.artifact[0]
    assert page.outcome == expected_outcome
    assert page.source_id is None
    assert list(sources_dir(config, registry).glob("*.md")) == []


async def test_a_mixed_html_and_pdf_batch_writes_both_captures_with_existing_shapes(
    install_crawler, make_config, tmp_path
):
    config = make_config(agent=AgentSettings(workspace_dir=tmp_path))
    registry = SourceRegistry()
    html_results = [
        _FakeResult(
            "https://article.test/page",
            markdown=_FakeMarkdown(raw_markdown="html body", fit_markdown="html body"),
        )
    ]
    pdf_results = [
        _FakeResult(
            "https://docs.test/report.pdf",
            markdown=_FakeMarkdown(raw_markdown="pdf body", fit_markdown="pdf body"),
        )
    ]
    fake_cls = install_crawler(html_results, pdf_results=pdf_results)
    fetch_pages = fetch.build_fetch_tool(config, registry)

    message = await fetch_pages.ainvoke(
        _tool_call(["https://article.test/page", "https://docs.test/report.pdf"], "call-mixed-pdf")
    )

    assert [call.is_pdf for call in fake_cls.calls] == [False, True]
    assert fake_cls.calls[0].urls == ["https://article.test/page"]
    assert fake_cls.calls[1].urls == ["https://docs.test/report.pdf"]

    html_page, pdf_page = message.artifact
    assert html_page.outcome == "fetched"
    assert pdf_page.outcome == "fetched"
    html_text = (sources_dir(config, registry) / f"{html_page.source_id}.md").read_text(
        encoding="utf-8"
    )
    pdf_text = (sources_dir(config, registry) / f"{pdf_page.source_id}.md").read_text(
        encoding="utf-8"
    )
    assert "html body" in html_text
    assert "pdf body" in pdf_text


async def test_char_cap_applies_to_extracted_pdf_text_same_as_markdown(
    install_crawler, make_config
):
    cap = 50
    config = make_config(per_page_char_cap=cap)
    registry = SourceRegistry()
    long_text = "z" * 500
    pdf_results = [
        _FakeResult(
            "https://docs.test/long.pdf",
            markdown=_FakeMarkdown(raw_markdown=long_text, fit_markdown=long_text),
        )
    ]
    install_crawler([], pdf_results=pdf_results)

    content, pages = await fetch._fetch(["https://docs.test/long.pdf"], config, registry, RunLog())

    assert len(content) < len(long_text)
    assert str(cap) in content
    assert pages[0].markdown == long_text


async def test_failed_fetch_outcomes_are_recorded_on_the_run_log(install_crawler, make_config):
    config = make_config()
    registry = SourceRegistry()
    run_log = RunLog()
    # Direct `_fetch` call, so the capture directory the builder normally creates must exist
    # here — otherwise every write fails and pollutes the log with capture_write_failed.
    sources_dir(config, registry).mkdir(parents=True, exist_ok=True)
    results = [
        _FakeResult(
            "https://ok.test",
            markdown=_FakeMarkdown(raw_markdown="fine", fit_markdown="fine"),
        ),
        _FakeResult(
            "https://blocked.test",
            status_code=403,
            markdown=_FakeMarkdown(raw_markdown="denied", fit_markdown="denied"),
        ),
    ]
    install_crawler(results)

    await fetch._fetch(["https://ok.test", "https://blocked.test"], config, registry, run_log)

    incidents = run_log.incidents()
    assert [incident.kind for incident in incidents] == ["fetch_failed"]
    assert "https://blocked.test" in incidents[0].detail
    assert "blocked" in incidents[0].detail
    assert "status 403" in incidents[0].detail


# --- Phase 3: firewall wiring (scan -> classify -> mint -> sanitize -> capture -> render) ----


async def test_a_page_carrying_an_attack_string_mints_no_sn_writes_no_file_and_is_absent(  # R1
    install_crawler, make_config, tmp_path
):
    """A blocked page vanishes from the pipeline entirely; a clean page in the same batch
    still fetches and renders normally (mixed batch)."""
    config = make_config(agent=AgentSettings(workspace_dir=tmp_path))
    registry = SourceRegistry()
    run_log = RunLog()
    attack_markdown = _attack_markdown()
    results = [
        _FakeResult(
            "https://evil.test",
            markdown=_FakeMarkdown(raw_markdown=attack_markdown, fit_markdown=attack_markdown),
        ),
        _FakeResult(
            "https://clean.test",
            markdown=_FakeMarkdown(raw_markdown="clean body", fit_markdown="clean body"),
        ),
    ]
    install_crawler(results)
    fetch_pages = fetch.build_fetch_tool(config, registry, run_log)

    message = await fetch_pages.ainvoke(
        _tool_call(["https://evil.test", "https://clean.test"], "call-guard-1")
    )

    # Blocked page vanished: no artifact entry, no Sn, absent from render. The clean page
    # in the same batch still fetches and registers normally.
    assert [page.url for page in message.artifact] == ["https://clean.test"]
    assert [source.url for source in registry.all()] == ["https://clean.test"]
    assert "https://evil.test" not in message.content
    assert "clean body" in message.content

    captures_dir = sources_dir(config, registry)
    written = list(captures_dir.glob("*.md"))
    assert len(written) == 1
    assert "clean body" in written[0].read_text(encoding="utf-8")

    incidents = [i for i in run_log.incidents() if i.kind == "guard_blocked"]
    assert len(incidents) == 1
    assert "https://evil.test" in incidents[0].detail
    assert "instruction_override" in incidents[0].detail


async def test_a_blocked_pdf_page_is_dropped_identically_to_an_html_page(  # R1
    install_crawler, make_config
):
    config = make_config()
    registry = SourceRegistry()
    run_log = RunLog()
    attack_markdown = _attack_markdown()
    pdf_results = [
        _FakeResult(
            "https://docs.test/evil.pdf",
            markdown=_FakeMarkdown(raw_markdown=attack_markdown, fit_markdown=attack_markdown),
        )
    ]
    install_crawler([], pdf_results=pdf_results)

    content, pages = await fetch._fetch(["https://docs.test/evil.pdf"], config, registry, run_log)

    assert pages == []
    assert registry.all() == []
    assert "https://docs.test/evil.pdf" not in content

    incidents = [i for i in run_log.incidents() if i.kind == "guard_blocked"]
    assert len(incidents) == 1
    assert "https://docs.test/evil.pdf" in incidents[0].detail


async def test_guard_disabled_bypasses_scanning_and_the_attack_page_fetches_normally(  # R1
    install_crawler, make_config
):
    config = make_config(guard=GuardSettings(enabled=False))
    registry = SourceRegistry()
    run_log = RunLog()
    attack_markdown = _attack_markdown()
    results = [
        _FakeResult(
            "https://evil.test",
            markdown=_FakeMarkdown(raw_markdown=attack_markdown, fit_markdown=attack_markdown),
        ),
    ]
    install_crawler(results)

    content, pages = await fetch._fetch(["https://evil.test"], config, registry, run_log)

    assert len(pages) == 1
    assert pages[0].outcome == "fetched"
    assert pages[0].source_id is not None
    assert registry.all() != []
    assert [i for i in run_log.incidents() if i.kind == "guard_blocked"] == []


async def test_survivor_markdown_zero_width_chars_stripped_when_guard_disabled(  # D5/D3
    install_crawler, make_config, tmp_path
):
    """The obfuscation family blocks on zero-width chars, so proving the sanitize-still-runs
    invariant (guard toggles detection, not hygiene) needs the guard OFF for THIS variant.
    """
    config = make_config(
        agent=AgentSettings(workspace_dir=tmp_path), guard=GuardSettings(enabled=False)
    )
    registry = SourceRegistry()
    dirty_markdown = "wo​rd"
    results = [
        _FakeResult(
            "https://zerowidth.test",
            markdown=_FakeMarkdown(raw_markdown=dirty_markdown, fit_markdown=dirty_markdown),
        )
    ]
    install_crawler(results)
    fetch_pages = fetch.build_fetch_tool(config, registry)

    message = await fetch_pages.ainvoke(_tool_call(["https://zerowidth.test"], "call-zw-1"))

    page = message.artifact[0]
    assert page.outcome == "fetched"
    captures_dir = sources_dir(config, registry)
    text = (captures_dir / f"{page.source_id}.md").read_text(encoding="utf-8")
    assert "word" in text
    assert "​" not in text


async def test_survivor_markdown_control_chars_stripped_with_guard_enabled(  # D5/D3
    install_crawler, make_config, tmp_path
):
    config = make_config(agent=AgentSettings(workspace_dir=tmp_path))
    registry = SourceRegistry()
    dirty_markdown = "be\x07ll"
    results = [
        _FakeResult(
            "https://control.test",
            markdown=_FakeMarkdown(raw_markdown=dirty_markdown, fit_markdown=dirty_markdown),
        )
    ]
    install_crawler(results)
    fetch_pages = fetch.build_fetch_tool(config, registry)

    message = await fetch_pages.ainvoke(_tool_call(["https://control.test"], "call-ctrl-1"))

    page = message.artifact[0]
    assert page.outcome == "fetched"
    captures_dir = sources_dir(config, registry)
    text = (captures_dir / f"{page.source_id}.md").read_text(encoding="utf-8")
    assert "bell" in text
    assert "\x07" not in text
