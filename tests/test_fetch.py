"""Behavioral tests for harness.tools.fetch."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from crawl4ai import DefaultMarkdownGenerator, PruningContentFilter  # type: ignore[import-untyped]
from langchain_core.tools import BaseTool
from pydantic import ValidationError

from harness.config import AgentSettings, BrowserSettings
from harness.sources import SourceRegistry
from harness.tools import fetch


class _FakeMarkdown:
    """Stand-in for crawl4ai's StringCompatibleMarkdown, exposing raw and fit variants."""

    def __init__(self, raw_markdown: str = "", fit_markdown: str = "") -> None:
        self.raw_markdown = raw_markdown
        self.fit_markdown = fit_markdown


class _FakeResult:
    """Stand-in for crawl4ai's CrawlResult, exposing only the attributes fetch.py reads."""

    def __init__(
        self,
        url: str,
        *,
        error_message: str | None = None,
        status_code: int | None = 200,
        response_headers: dict | None = None,
        metadata: dict | None = None,
        markdown: _FakeMarkdown | None = None,
    ) -> None:
        self.url = url
        self.error_message = error_message
        self.status_code = status_code
        self.response_headers = response_headers
        self.metadata = metadata
        self.markdown = markdown


def _make_fake_crawler_class(results: list[_FakeResult]) -> type:
    """Build a fake AsyncWebCrawler class recording construction and `arun_many` calls."""

    class _FakeCrawler:
        constructed_with: list[object] = []
        calls: list[SimpleNamespace] = []

        def __init__(self, config: object = None) -> None:
            _FakeCrawler.constructed_with.append(config)

        async def __aenter__(self) -> "_FakeCrawler":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        async def arun_many(
            self, urls: list[str], config: object = None, dispatcher: object = None
        ) -> list[_FakeResult]:
            _FakeCrawler.calls.append(
                SimpleNamespace(urls=urls, config=config, dispatcher=dispatcher)
            )
            return results

    return _FakeCrawler


@pytest.fixture
def install_crawler(monkeypatch):
    """Patch fetch's AsyncWebCrawler with a fake serving canned results; returns the class."""

    def _install(results: list[_FakeResult]) -> type:
        fake_cls = _make_fake_crawler_class(results)
        monkeypatch.setattr("harness.tools.fetch.AsyncWebCrawler", fake_cls)
        return fake_cls

    return _install


async def test_empty_url_list_returns_empty_content_and_artifact(install_crawler, make_config):
    config = make_config()
    registry = SourceRegistry()
    fake_cls = install_crawler([])

    content, pages = await fetch._fetch([], config, registry)

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
        ["https://a.test", "https://b.test", "https://c.test"], config, registry
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
        (200, None, "application/pdf", "", "non_html"),
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
    )

    # One crawl, one page, one heading — never two [Sn] blocks over one identity.
    assert fake_cls.calls[0].urls == ["https://dup.test/a"]
    assert len(pages) == 1
    assert registry.get(pages[0].source_id) is not None
    assert len(registry.all()) == 1
    assert content.count(f"## [{pages[0].source_id}]") == 1


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

    content, pages = await fetch._fetch(["https://long.test"], config, registry)

    assert len(content) < len(long_markdown)
    assert str(cap) in content
    assert pages[0].markdown == long_markdown


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

    content, pages = await fetch._fetch(["https://ok.test", "https://fail.test"], config, registry)

    for page in pages:
        assert f"## [{page.source_id}] {page.url}" in content
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

    await fetch._fetch(["https://a.test"], config, registry)

    assert len(fake_cls.calls) == 1
    recorded = fake_cls.calls[0]
    assert recorded.config.page_timeout == 1234
    assert recorded.dispatcher.max_session_permit == 3


async def test_boilerplate_stripping_config_reaches_the_crawl4ai_call(install_crawler, make_config):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult("https://a.test", markdown=_FakeMarkdown(raw_markdown="a", fit_markdown="a"))
    ]
    fake_cls = install_crawler(results)

    await fetch._fetch(["https://a.test"], config, registry)

    recorded = fake_cls.calls[0].config
    assert set(recorded.excluded_tags) >= {"nav", "header", "footer", "aside", "script"}
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


def test_build_browser_config_maps_backend_to_browser_mode():
    lightpanda = fetch.build_browser_config(
        BrowserSettings(backend="lightpanda", cdp_url="ws://lightpanda.test:9222")
    )
    assert lightpanda.browser_mode == "cdp"
    assert lightpanda.cdp_url == "ws://lightpanda.test:9222"

    playwright = fetch.build_browser_config(BrowserSettings(backend="playwright"))
    assert playwright.cdp_url is None
    assert playwright.browser_mode == "dedicated"


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

    content, pages = await fetch._fetch(["https://pruned.test"], config, registry)

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

    _, pages = await fetch._fetch(["https://unpruned.test"], config, registry)

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

    _, pages = await fetch._fetch(["https://titled.test"], config, registry)

    assert pages[0].title == "An Article Title"
    source = registry.get(pages[0].source_id)
    assert source is not None
    assert source.title == "An Article Title"


async def test_content_type_header_on_the_result_drives_non_html_classification(
    install_crawler, make_config
):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://pdf.test/doc",
            response_headers={"Content-Type": "application/pdf"},
            markdown=_FakeMarkdown(raw_markdown="%PDF junk", fit_markdown="%PDF junk"),
        )
    ]
    install_crawler(results)

    _, pages = await fetch._fetch(["https://pdf.test/doc"], config, registry)

    # Exercises _content_type's case-insensitive header lookup through the real
    # pipeline — the classify() unit tests bypass it with a plain string.
    assert pages[0].outcome == "non_html"


async def test_result_whose_url_differs_from_the_input_is_still_paired(
    install_crawler, make_config
):
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

    _, pages = await fetch._fetch(["https://original.test/start"], config, registry)

    assert len(pages) == 1
    assert pages[0].url == "https://original.test/start"
    assert pages[0].markdown == "redirected content"


def _tool_call(urls: list[str], call_id: str) -> dict:
    """Build a `ToolCall`-shaped dict for `fetch_pages.ainvoke`."""
    return {"name": "fetch_pages", "args": {"urls": urls}, "id": call_id, "type": "tool_call"}


async def test_fetch_of_more_urls_than_the_cap_is_rejected(install_crawler, make_config):
    config = make_config(max_urls_per_call=4)
    registry = SourceRegistry()
    fake_cls = install_crawler([])
    fetch_pages = fetch.build_fetch_tool(config, registry)

    with pytest.raises(ValidationError):
        await fetch_pages.ainvoke(
            _tool_call([f"https://u{i}.test" for i in range(5)], "call-cap-reject")
        )

    assert fake_cls.calls == []


async def test_fetch_at_exactly_the_cap_proceeds(install_crawler, make_config):
    config = make_config(max_urls_per_call=4)
    registry = SourceRegistry()
    urls = [f"https://u{i}.test" for i in range(4)]
    results = [
        _FakeResult(url, markdown=_FakeMarkdown(raw_markdown="x", fit_markdown="x")) for url in urls
    ]
    fake_cls = install_crawler(results)
    fetch_pages = fetch.build_fetch_tool(config, registry)

    message = await fetch_pages.ainvoke(_tool_call(urls, "call-cap-exact"))

    assert len(fake_cls.calls) == 1
    assert len(message.artifact) == 4


async def test_cap_is_bound_from_config_not_hardcoded(install_crawler, make_config):
    config = make_config(max_urls_per_call=2)
    registry = SourceRegistry()
    fake_cls = install_crawler([])
    fetch_pages = fetch.build_fetch_tool(config, registry)

    with pytest.raises(ValidationError):
        await fetch_pages.ainvoke(
            _tool_call(["https://a.test", "https://b.test", "https://c.test"], "call-cap-3")
        )
    assert fake_cls.calls == []

    results = [
        _FakeResult("https://a.test", markdown=_FakeMarkdown(raw_markdown="a", fit_markdown="a")),
        _FakeResult("https://b.test", markdown=_FakeMarkdown(raw_markdown="b", fit_markdown="b")),
    ]
    fake_cls_2 = install_crawler(results)
    message = await fetch_pages.ainvoke(
        _tool_call(["https://a.test", "https://b.test"], "call-cap-2")
    )

    assert len(fake_cls_2.calls) == 1
    assert len(message.artifact) == 2


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
    source_path = tmp_path / "sources" / f"{source_id}.md"
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
    text = (tmp_path / "sources" / f"{source_id}.md").read_text(encoding="utf-8")
    assert long_markdown in text


@pytest.mark.parametrize(
    ("outcome", "result_kwargs"),
    [
        ("blocked", {"status_code": 403}),
        ("timeout", {"error_message": "Timeout after 15000ms", "status_code": 200}),
        ("non_html", {"response_headers": {"Content-Type": "application/pdf"}}),
        ("error", {"status_code": 500, "error_message": "internal server error"}),
    ],
)
async def test_failed_fetch_writes_a_stub_naming_its_outcome(
    install_crawler, make_config, tmp_path, outcome, result_kwargs
):
    config = make_config(agent=AgentSettings(workspace_dir=tmp_path))
    registry = SourceRegistry()
    page_text = "should never reach the stub"
    results = [
        _FakeResult(
            "https://fail.test",
            markdown=_FakeMarkdown(raw_markdown=page_text, fit_markdown=page_text),
            **result_kwargs,
        )
    ]
    install_crawler(results)
    fetch_pages = fetch.build_fetch_tool(config, registry)

    message = await fetch_pages.ainvoke(_tool_call(["https://fail.test"], f"call-stub-{outcome}"))

    page = message.artifact[0]
    assert page.outcome == outcome
    text = (tmp_path / "sources" / f"{page.source_id}.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == f"FETCH FAILED: {outcome}"
    assert "https://fail.test" in text
    assert page_text not in text

    # The stub keeps the status code and error text the artifact carries, omitting
    # a bullet when its value is absent rather than printing `None`.
    if page.status_code is not None:
        assert f"- Status: {page.status_code}" in text
    else:
        assert "- Status:" not in text
    if page.error:
        assert f"- Error: {page.error}" in text
    else:
        assert "- Error:" not in text


async def test_a_mixed_batch_writes_content_for_successes_and_stubs_for_failures(
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
    ok_text = (tmp_path / "sources" / f"{ok_page.source_id}.md").read_text(encoding="utf-8")
    bad_text = (tmp_path / "sources" / f"{bad_page.source_id}.md").read_text(encoding="utf-8")

    assert ok_text.splitlines()[0] == f"# {ok_page.source_id}: https://ok.test"
    assert "ok body" in ok_text
    assert bad_text.splitlines()[0] == f"FETCH FAILED: {bad_page.outcome}"
    assert "ok body" not in bad_text


async def test_one_source_write_failure_does_not_poison_the_batch_or_go_silent(
    install_crawler, make_config, tmp_path, monkeypatch, capsys
):
    config = make_config(agent=AgentSettings(workspace_dir=tmp_path))
    registry = SourceRegistry()
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
    fetch_pages = fetch.build_fetch_tool(config, registry)

    # Source IDs are minted in input order by a fresh registry, so the first URL is
    # S1 and the second S2 — predictable before the call, letting us target exactly
    # one write for failure.
    failing_path = tmp_path / "sources" / "S1.md"
    ok_path = tmp_path / "sources" / "S2.md"
    real_write_text = Path.write_text

    def raising_write_text(self: Path, *args: object, **kwargs: object) -> int:
        if self == failing_path:
            raise OSError("disk full")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", raising_write_text)

    message = await fetch_pages.ainvoke(
        _tool_call(["https://a.test", "https://b.test"], "call-write-fail")
    )

    # The batch was not failed — both pages still come back.
    assert [page.url for page in message.artifact] == ["https://a.test", "https://b.test"]
    # One failure did not poison the rest — the second file was still written.
    assert not failing_path.exists()
    assert ok_path.exists()

    err = capsys.readouterr().err
    assert "S1" in err
    assert "https://a.test" in err


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
    sources_dir = tmp_path / "sources"
    assert list(sources_dir.glob(f"{first_id}*.md")) == [sources_dir / f"{first_id}.md"]
    text = (sources_dir / f"{first_id}.md").read_text(encoding="utf-8")
    assert "second" in text
    assert "first" not in text
