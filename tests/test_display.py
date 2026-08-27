"""Behavioral tests for harness.display: events, renderers, and StageTracker."""

import pathlib
import re
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from io import StringIO
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage
from rich.console import Console

import harness.__main__ as main_module
import harness.display
import harness.session as session_module
from harness.activity import ActivitySink
from harness.config import AgentSettings
from harness.display import (
    Activity,
    Alert,
    DisplayEvent,
    PlainRenderer,
    Question,
    ReaderItem,
    ReadersUpdated,
    RichRenderer,
    RoundsUpdated,
    RunFinished,
    StageCompleted,
    StageStarted,
    StageTracker,
    TodoItem,
    TodosUpdated,
    ToolCall,
    WelcomeView,
    build_help_panel,
    build_model_picker,
    build_renderer,
    build_welcome,
)
from harness.sources import SourceRegistry
from tests.conftest import (
    _dispatch_call,
    _FakeMarkdown,
    _FakeResult,
    _lead_model,
    _submit_call,
    drain_stdout,
    install_search_transport,
    patch_run,
    patch_run_by_role,
    verify_reply,
    write_source_capture,
)
from tests.test_agent import _reader_fetch_call, _task_call

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _span(hex_color: str, value: str) -> str:
    """The exact truecolor ANSI span Rich emits for `Text(value, style=hex_color)`.

    Whole-value, not just the escape code: a highlighter-shredded line (per-token colours
    overriding `style=`) would satisfy `"38;2;r;g;b" in raw` too, so every styled-output
    assertion in this file pins the WHOLE value inside one span (plan `## Discoveries`,
    2026-08-20). One home for the construction, shared by every Phase 6 pin.
    """
    stripped = hex_color.lstrip("#")
    r, g, b = (int(stripped[i : i + 2], 16) for i in (0, 2, 4))
    return f"\x1b[38;2;{r};{g};{b}m{value}\x1b[0m"


def _make_console(
    *, width: int = 80, get_time: Callable[[], float] | None = None
) -> tuple[Console, StringIO]:
    buffer = StringIO()
    # `legacy_windows=False` (rather than relying on auto-detection): the alternate-screen
    # codes `RichRenderer` now relies on (D1/R5) are suppressed by Rich whenever
    # `legacy_windows` is true, which it auto-detects as True on this platform even over a
    # `StringIO` file — leaving the alt-screen behavior untestable on Windows otherwise.
    # `color_system="truecolor"` (rather than "auto"): the `_PENDING` (`#474747`) pending
    # style (R6) only emits its literal `38;2;...` truecolor escape under truecolor; "auto"
    # resolves to 16-color "standard" here, downgrading the hex style to a named ANSI color.
    # `_environ={}`: an ambient NO_COLOR in the invoking shell would strip color styles
    # even under force_terminal, making the truecolor assertions env-dependent.
    console = Console(
        file=buffer,
        force_terminal=True,
        width=width,
        legacy_windows=False,
        color_system="truecolor",
        _environ={},
        # `get_time` drives `Spinner.render`'s animation frame — injectable so the spinner
        # rotation test can advance render time deterministically (None keeps Rich's default).
        get_time=get_time,
    )
    return console, buffer


def _rich_renderer(*, clock: Callable[[], float] | None = None) -> tuple[RichRenderer, StringIO]:
    console, buffer = _make_console()
    kwargs: dict[str, Any] = {} if clock is None else {"clock": clock}
    return RichRenderer(console=console, auto_refresh=False, **kwargs), buffer


def _fixed_elapsed_clock(elapsed_seconds: float) -> Callable[[], float]:
    """A clock returning 0 on its first call (`RichRenderer.__init__`'s `_started_at`) and
    `elapsed_seconds` on every call after (each render-time `clock() - self._started_at`)."""
    calls = iter([0.0])

    def clock() -> float:
        return next(calls, elapsed_seconds)

    return clock


@pytest.mark.parametrize(
    ("event", "expected_line"),
    [
        (StageStarted("researching"), "researching..."),
        (StageCompleted("researching", 12.34), "researching done (12.3s)"),
        (Activity("[pending] Find sources"), "  [pending] Find sources"),
        (Question("Which region?"), "Which region?"),
    ],
)
def test_plain_renderer_emits_the_expected_line(capsys, event: DisplayEvent, expected_line: str):
    renderer = PlainRenderer()

    renderer.emit(event)

    out, lines = drain_stdout(capsys)
    assert lines == [expected_line]


def test_plain_renderer_prints_todos_updated_as_sequential_lines_with_no_alt_screen(capsys):
    renderer = PlainRenderer()
    todos = (
        TodoItem(content="Find sources", status="pending"),
        TodoItem(content="Draft outline", status="completed"),
    )

    renderer.emit(TodosUpdated(todos))

    out, lines = drain_stdout(capsys)
    assert lines == ["  [pending] Find sources", "  [completed] Draft outline"]
    assert "\x1b[?1049" not in out


def test_plain_renderer_suspend_is_a_no_op_context_manager(capsys):
    renderer = PlainRenderer()

    with renderer.suspend():
        pass

    out, _ = drain_stdout(capsys)
    assert out == ""


def test_plain_renderer_close_is_a_no_op():
    renderer = PlainRenderer()

    renderer.close()  # must not raise


def test_build_renderer_returns_a_plain_renderer():
    assert isinstance(build_renderer(), PlainRenderer)


class _RecordingRenderer:
    """A fake `Renderer` that records emitted events instead of printing them."""

    def __init__(self) -> None:
        self.events: list[DisplayEvent] = []
        self.closes = 0

    def emit(self, event: DisplayEvent) -> None:
        self.events.append(event)

    def suspend(self) -> AbstractContextManager[None]:
        raise NotImplementedError

    def close(self) -> None:
        self.closes += 1


def _fake_clock(times: list[float]):
    values = iter(times)

    def _clock() -> float:
        return next(values)

    return _clock


def test_stage_tracker_advancing_to_the_same_stage_twice_emits_one_stage_started():
    renderer = _RecordingRenderer()
    tracker = StageTracker(renderer, clock=_fake_clock([0.0, 1.0]))

    tracker.advance("researching")
    tracker.advance("researching")

    assert renderer.events == [StageStarted("researching")]


def test_stage_tracker_advancing_to_a_new_stage_completes_the_old_one_first():
    renderer = _RecordingRenderer()
    tracker = StageTracker(renderer, clock=_fake_clock([0.0, 5.0]))

    tracker.advance("researching")
    tracker.advance("verifying")

    assert renderer.events == [
        StageStarted("researching"),
        StageCompleted("researching", 5.0),
        StageStarted("verifying"),
    ]


def test_stage_tracker_finish_with_no_current_stage_emits_nothing():
    renderer = _RecordingRenderer()
    tracker = StageTracker(renderer, clock=_fake_clock([]))

    tracker.finish()

    assert renderer.events == []


def test_stage_tracker_finish_emits_completion_and_is_safe_to_call_twice():
    renderer = _RecordingRenderer()
    tracker = StageTracker(renderer, clock=_fake_clock([0.0, 3.5]))

    tracker.advance("writing")
    tracker.finish()
    tracker.finish()

    assert renderer.events == [
        StageStarted("writing"),
        StageCompleted("writing", 3.5),
    ]


def _install_stub_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route `search_web` to a no-network empty-results stub (ordering caveat: see helper)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"query": "x", "results": []})

    install_search_transport(monkeypatch, handler)


# --- Graph-driven tests (drive main() with a scripted model) ---------------------------


async def test_a_direct_answer_skips_the_clarifying_and_researching_lines(
    make_config, monkeypatch, scripted_model, capsys
):
    config = make_config()
    # D3: the answer is delivered by `submit_report`, then one more turn closes the tool call.
    final = _submit_call("Final answer.").model_copy(
        update={"usage_metadata": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
    )
    model = scripted_model([final, AIMessage(content="Report submitted.")])
    patch_run(monkeypatch, config, model)

    await main_module.main(["a question with no tool calls"])

    out, lines = drain_stdout(capsys)
    assert "clarifying..." not in out
    assert "researching..." not in out
    assert lines[-1].strip().endswith(".md")


async def test_a_research_call_and_todo_produce_todos_updated_lines(
    make_config, monkeypatch, scripted_model, capsys
):
    """Todos now arrive via `TodosUpdated` (Contracts), not flattened `Activity` text.

    A second `write_todos` call changes only one item's status; the PlainRenderer's
    `TodosUpdated` handling reprints the FULL current list (replacement, not a diff) — so the
    UNCHANGED second item is printed again too, which the old per-item flattening would not
    have done.

    "Research activity visible in the TUI" manifests as the LEAD's `dispatch_researcher` line
    (D1) — the researcher's own nested `search_web` call runs in a separate graph and never
    reaches the lead's stream at all, so only ONE activity line is expected, not one per
    internal tool.
    """
    config = make_config()
    plan_search_and_replan: list[Any] = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_todos",
                    "args": {
                        "todos": [
                            {"content": "Search for the answer", "status": "pending"},
                            {"content": "Write the summary", "status": "pending"},
                        ]
                    },
                    "id": "call_todo_1",
                },
            ],
        ),
        _dispatch_call("Search for the answer", "call_dispatch"),
        AIMessage(content="Angle dispatched."),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_todos",
                    "args": {
                        "todos": [
                            {"content": "Search for the answer", "status": "completed"},
                            {"content": "Write the summary", "status": "pending"},
                        ]
                    },
                    "id": "call_todo_2",
                },
            ],
        ),
    ]
    final = _submit_call("Final answer.").model_copy(
        update={"usage_metadata": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
    )
    head_model = scripted_model(
        [*plan_search_and_replan, final, AIMessage(content="Report submitted.")]
    )
    researcher_model = scripted_model([AIMessage(content="Researcher report: the answer is 42.")])
    patch_run_by_role(monkeypatch, config, {"head": head_model, "researcher": researcher_model})
    _install_stub_search(monkeypatch)

    await main_module.main(["a question needing research"])

    out, lines = drain_stdout(capsys)
    assert "  dispatch_researcher: Search for the answer" in out
    assert "  [pending] Search for the answer" in lines
    assert "  [completed] Search for the answer" in lines
    assert lines.count("  [pending] Write the summary") == 2
    assert lines[-1].strip().endswith(".md")


def test_todo_items_prefers_in_flight_over_sources_over_none():
    """Fix-pass item 5: `_todo_items`'s meta rule was entirely unasserted. Counts chosen so a
    hardcoded `1`/`3` (the mockup's own numbers) would fail this."""
    registry = SourceRegistry()
    for i in range(4):
        source_id = registry.add(f"https://example.test/{i}")
        registry.mark_read(source_id, "digested")
    todos = [
        {"content": "Investigate the topic", "status": "in_progress"},
        {"content": "Write the summary", "status": "pending"},
    ]

    # Live readers present -> "N in flight" wins over the sources count.
    sink_with_readers = ActivitySink()
    sink_with_readers.start_reader("Angle A")
    sink_with_readers.start_reader("Angle B")
    items = session_module._todo_items(todos, registry, sink_with_readers)
    in_progress = next(i for i in items if i.status == "in_progress")
    pending = next(i for i in items if i.status == "pending")
    assert in_progress.meta == "2 in flight"
    assert pending.meta is None  # only the in_progress row ever carries meta

    # No live readers, but sources have been read -> falls back to the sources count.
    sink_idle = ActivitySink()
    items = session_module._todo_items(todos, registry, sink_idle)
    in_progress = next(i for i in items if i.status == "in_progress")
    assert in_progress.meta == "4 sources"

    # Neither live readers nor read sources -> no meta at all.
    items = session_module._todo_items(todos, SourceRegistry(), sink_idle)
    in_progress = next(i for i in items if i.status == "in_progress")
    assert in_progress.meta is None


async def test_todos_meta_refreshes_on_live_reader_count_change_and_only_then(
    make_config, monkeypatch, scripted_model
):
    """Fix-pass item 5's second half: with the `on_change` push callback wired (item 1), one
    `write_todos` call sets the in_progress row ONCE; the todos list never changes again, so
    any further `TodosUpdated` for it must come from the live-reader-count refresh alone, and
    the refresh must not repeat while the count stays put.
    """
    config = make_config()
    model = scripted_model(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_todos",
                        "args": {
                            "todos": [{"content": "Investigate the topic", "status": "in_progress"}]
                        },
                        "id": "call_todo",
                    }
                ],
            ),
            _dispatch_call("Read the source", "call_dispatch"),
            AIMessage(content="Angle dispatched."),
            _submit_call("Final answer about the topic."),
            AIMessage(content="Report submitted."),
        ]
    )
    researcher_model = scripted_model(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {"description": "Digest the source", "subagent_type": "reader"},
                        "id": "call_reader",
                    }
                ],
            ),
            AIMessage(content="Researcher summary text."),
        ]
    )
    reader_model = scripted_model([AIMessage(content="Reader digest text.")])
    patch_run_by_role(
        monkeypatch,
        config,
        {"head": model, "researcher": researcher_model, "reader": reader_model},
    )

    recorder = _RecordingRenderer()
    monkeypatch.setattr(main_module, "build_renderer", lambda: recorder)

    await main_module.main(["a question needing delegation"])

    metas = [
        item.meta
        for event in recorder.events
        if isinstance(event, TodosUpdated)
        for item in event.todos
        if item.status == "in_progress"
    ]

    # The reader's live window produced exactly one "1 in flight" meta -- not zero (the push
    # never fired) and not more than one (a repeat while the count was static).
    assert metas.count("1 in flight") == 1
    # Once the reader finished, a LATER emission for the SAME todos list carries a different
    # meta -- proof the refresh is driven by the sink's live-reader count, not by a change to
    # the todos themselves (there is only ever one `write_todos` call in this script).
    assert metas[-1] != "1 in flight"


async def test_the_source_count_is_emitted_only_when_the_registry_grows(
    make_config, monkeypatch, scripted_model, install_crawler
):
    """A full 3-tier run (lead -> researcher -> reader, twice, each fetching a real page)
    registers a source at two DIFFERENT points in the stream -- proves `_emit_source_count`
    (Contracts) is change-gated, not a per-chunk poll. Template: the 3-tier single-shared-
    model script `test_todos_meta_refreshes_on_live_reader_count_change_and_only_then` uses
    above, and the real-source registration `test_the_summary_counts_real_usable_and_unusable
    _sources` (~line 1421) uses, but here through the real `fetch_pages` tool so the registry
    actually grows mid-run rather than being pre-populated.
    """
    config = make_config()
    # One angle per lead turn, not two at once: the two researchers would otherwise run
    # concurrently (D1) and consume the single researcher script out of order. The point of
    # this test — a registry that grows at two different points in the stream — is unchanged.
    model = scripted_model(
        [
            _dispatch_call("angle A", "call_r1"),
            AIMessage(content="Angle A dispatched."),
            _dispatch_call("angle B", "call_r2"),
            AIMessage(content="Angle B dispatched."),
            _submit_call("Final answer summarizing both pages.").model_copy(
                update={
                    "usage_metadata": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                    }
                }
            ),
            AIMessage(content="Report submitted."),
        ]
    )
    researcher_model = scripted_model(
        [
            _task_call(
                "Fetch and digest https://a.test", call_id="call_reader_a", subagent_type="reader"
            ),
            AIMessage(content="Researcher A report: finding confirmed."),
            _task_call(
                "Fetch and digest https://b.test", call_id="call_reader_b", subagent_type="reader"
            ),
            AIMessage(content="Researcher B report: finding confirmed."),
        ]
    )
    reader_model = scripted_model(
        [
            _reader_fetch_call("https://a.test"),
            AIMessage(content="Digest A."),
            # Not `_reader_fetch_call` (hardcodes id="call_fetch", already used for a.test's
            # fetch above -- a REUSED tool-call id across two different dispatches, sharing one
            # thread, corrupted the graph's message history and silently broke this fetch).
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "fetch_pages",
                        "args": {"urls": ["https://b.test"]},
                        "id": "call_fetch_b",
                    }
                ],
            ),
            AIMessage(content="Digest B."),
        ]
    )
    # `_pair` (fetch.py) matches a result to its URL by the result's OWN `.url` attribute, not
    # by position -- one result per URL, or the second fetch pairs with nothing and reads as
    # "no result returned for this URL".
    install_crawler(
        [
            _FakeResult(
                "https://a.test",
                markdown=_FakeMarkdown(raw_markdown="A body", fit_markdown="A body"),
            ),
            _FakeResult(
                "https://b.test",
                markdown=_FakeMarkdown(raw_markdown="B body", fit_markdown="B body"),
            ),
        ]
    )
    # A real (not no-op'd) browser preflight: it constructs its warm crawler through the very
    # same `_crawler_class()` seam `install_crawler` just faked, which is what makes the
    # HTTP-first fetch below actually succeed instead of failing with no browser to escalate to.
    patch_run_by_role(
        monkeypatch,
        config,
        {"head": model, "researcher": researcher_model, "reader": reader_model},
        run_browser_preflight=True,
    )

    # Counts every `SourceRegistry.count()` call regardless of instance -- `main()` builds its
    # own registry internally, so there is no instance to wrap directly. This is the exact
    # count of `_emit_source_count()` invocations, i.e. of relevant stream chunks polled.
    poll_calls = 0
    original_count = SourceRegistry.count

    def _counting_count(self: SourceRegistry) -> int:
        nonlocal poll_calls
        poll_calls += 1
        return original_count(self)

    monkeypatch.setattr(SourceRegistry, "count", _counting_count)

    recorder = _RecordingRenderer()
    monkeypatch.setattr(main_module, "build_renderer", lambda: recorder)

    # Pasted URLs are the sanctioned pre-approval route (R2) -- no `search_web` call needed.
    await main_module.main(["Please read https://a.test and https://b.test and summarize"])

    counts = [
        event.count
        for event in recorder.events
        if isinstance(event, harness.display.SourcesUpdated)
    ]
    assert counts == [1, 2]
    assert len(counts) < poll_calls


# --- RichRenderer tests -------------------------------------------------------------------


def test_rich_renderer_stage_lifecycle_collapses_to_one_line():
    renderer, buffer = _rich_renderer()

    renderer.emit(StageStarted("researching"))
    renderer.emit(Activity('search_web: "first query"'))
    renderer.emit(Activity('search_web: "second query"'))
    before = len(buffer.getvalue())
    renderer.emit(StageCompleted("researching", 2.0))
    frame = _strip_ansi(buffer.getvalue()[before:])
    renderer.close()

    text = _strip_ansi(buffer.getvalue())
    assert "researching" in text
    assert 'search_web: "first query"' in text
    assert 'search_web: "second query"' in text
    # The completion frame collapses the stage: exactly one timeline line, activities gone.
    lines = [line for line in frame.splitlines() if line.strip()]
    collapsed = [line for line in lines if re.search(r"researching \(2\.0s\)", line)]
    assert len(collapsed) == 1
    assert re.search(r"ok\s+researching \(2\.0s\)", collapsed[0])
    assert "first query" not in frame
    assert "second query" not in frame


def test_rich_renderer_activity_tail_shows_only_last_eight():
    # Earlier frames' text stays physically present in the raw recorded buffer (the Live
    # region overwrites in place via ANSI cursor control), so the assertable frame is the
    # buffer delta written by the LAST update alone.
    renderer, buffer = _rich_renderer()

    renderer.emit(StageStarted("researching"))
    for i in range(9):
        renderer.emit(Activity(f"activity {i}"))
    before = len(buffer.getvalue())
    renderer.emit(Activity("activity 9"))
    last_frame = _strip_ansi(buffer.getvalue()[before:])
    renderer.close()

    assert "activity 0" not in last_frame
    assert "activity 1" not in last_frame
    for i in range(2, 10):
        assert f"activity {i}" in last_frame


def test_rich_renderer_renders_pre_stage_activities_with_the_first_frame():
    renderer, buffer = _rich_renderer()

    renderer.emit(Activity("[pending] Search for the answer"))
    renderer.emit(StageStarted("researching"))
    renderer.close()

    text = _strip_ansi(buffer.getvalue())
    assert "[pending] Search for the answer" in text


def test_rich_renderer_paints_activity_before_any_stage_starts():
    """The first stage trails the agent's first model turn, so waiting for `StageStarted`
    left the terminal blank through it and buffered the todo plan out of sight (R1)."""
    renderer, buffer = _rich_renderer()

    renderer.emit(Activity("[pending] Search for the answer"))
    painted_before_any_stage = _strip_ansi(buffer.getvalue())
    renderer.close()

    assert "[pending] Search for the answer" in painted_before_any_stage


def test_rich_renderer_stage_line_is_ascii_only():
    """CLAUDE.md forbids non-ASCII in output strings; a checkmark here broke ASCII readers."""
    renderer, buffer = _rich_renderer()

    renderer.emit(StageStarted("researching"))
    renderer.emit(StageCompleted("researching", 2.0))
    renderer.close()

    lines = _strip_ansi(buffer.getvalue()).splitlines()
    collapsed = next(line for line in lines if "researching (2.0s)" in line)
    # The timeline line now renders inside the log Panel, whose box-drawing border shares
    # the row — the ASCII requirement is about the line's CONTENT, so strip the border.
    assert collapsed.strip().strip("│").strip().isascii()


def test_rich_renderer_two_stages_produce_two_collapsed_lines_in_order():
    renderer, buffer = _rich_renderer()

    renderer.emit(StageStarted("researching"))
    renderer.emit(StageCompleted("researching", 1.0))
    renderer.emit(StageStarted("verifying"))
    # Timeline lines live INSIDE the frame (a print under `screen=True` would be discarded
    # with the alt buffer), so every later frame repeats them — assert on the single frame
    # painted by the final StageCompleted, which must carry both lines in order.
    before = len(buffer.getvalue())
    renderer.emit(StageCompleted("verifying", 3.5))
    frame = _strip_ansi(buffer.getvalue()[before:])
    renderer.close()

    lines = [line for line in frame.splitlines() if line.strip()]
    collapsed = [line for line in lines if "ok " in line]
    assert len(collapsed) == 2
    assert re.search(r"researching \(1\.0s\)", collapsed[0])
    assert re.search(r"verifying \(3\.5s\)", collapsed[1])


def test_build_renderer_picks_rich_on_a_tty(monkeypatch):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    assert isinstance(build_renderer(), RichRenderer)


def test_build_renderer_picks_plain_off_a_tty(monkeypatch):
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)

    assert isinstance(build_renderer(), PlainRenderer)


def test_rich_renderer_close_twice_does_not_raise():
    renderer, _ = _rich_renderer()
    renderer.emit(StageStarted("researching"))

    renderer.close()
    renderer.close()  # must not raise


def test_rich_renderer_suspend_is_a_context_manager():
    renderer, _ = _rich_renderer()

    with renderer.suspend():
        pass

    renderer.close()


# --- TodosUpdated / pinned checklist tests (Phase 1, D1/R5/R6) -----------------------------


def test_rich_renderer_todos_updated_renders_checklist_and_replaces_on_next_update():
    renderer, buffer = _rich_renderer()

    renderer.emit(
        TodosUpdated(
            (
                TodoItem(content="Draft outline", status="completed"),
                TodoItem(content="Search for sources", status="in_progress"),
                TodoItem(content="Write summary", status="pending"),
            )
        )
    )

    raw = buffer.getvalue()
    text = _strip_ansi(raw)
    assert "[x] Draft outline" in text
    assert "> Search for sources" in text
    assert "[ ] Write summary" in text
    # `_PENDING` (`#474747`) as `Console(force_terminal=True)` truecolor: 0x47,0x47,0x47 = 71,71,71.
    assert "38;2;71;71;71" in raw
    # in_progress is visually distinct from both completed and pending — distinct markers.
    assert "[x] Search for sources" not in text
    assert "[ ] Search for sources" not in text

    before = len(buffer.getvalue())
    renderer.emit(TodosUpdated((TodoItem(content="Only item", status="pending"),)))
    last_frame = _strip_ansi(buffer.getvalue()[before:])
    renderer.close()

    assert "[ ] Only item" in last_frame
    assert "Draft outline" not in last_frame
    assert "Search for sources" not in last_frame
    assert "Write summary" not in last_frame


def test_rich_renderer_layout_order_is_checklist_then_rule_then_activity_then_footer():
    renderer, buffer = _rich_renderer()

    renderer.emit(TodosUpdated((TodoItem(content="Find sources", status="pending"),)))
    # Checklist, rule, and footer are part of EVERY frame (Contracts/D1), so comparing raw
    # indices across the whole cumulative buffer would just find each one's earliest
    # occurrence in the FIRST frame — before the activity line exists at all. Isolating the
    # frame delta since just before this update (mirrors the activity-tail test) captures one
    # single, self-contained screen to check the top-to-bottom order within.
    before = len(buffer.getvalue())
    renderer.emit(Activity('search_web: "a query"'))
    frame = _strip_ansi(buffer.getvalue()[before:])
    renderer.close()

    checklist_index = frame.index("Find sources")
    rule_index = frame.index("─")  # the gray `Rule` separator (R6)
    activity_index = frame.index('search_web: "a query"')
    footer_index = frame.index("Ctrl+C to exit")
    assert checklist_index < rule_index < activity_index < footer_index

    lines = [line for line in frame.splitlines() if line.strip()]
    footer_lines = [line for line in lines if "Ctrl+C to exit" in line]
    assert footer_lines
    assert footer_lines[-1].strip() == "Ctrl+C to exit"


# --- ask_user in-place overlay (Phase 5) ---------------------------------------------------
#
# Replaces the old "suspend prints the question panel" tests: the overlay is now the only
# place a question is shown, and `_suspend()` no longer touches it (step 4).


def test_overlay_renders_in_frame_with_the_ledger_and_stage_line_still_visible():
    """R4: the task ledger and stage line stay visible; the overlay covers only the
    tool-log region, and carries the panel title, submit hint, and pause disclosure."""
    renderer, buffer = _rich_renderer()

    renderer.emit(StageStarted("researching"))
    renderer.emit(TodosUpdated((TodoItem(content="Find sources", status="in_progress"),)))
    renderer.emit(Question("Which reading did you mean?"))
    text = _strip_ansi(buffer.getvalue())
    renderer.close()

    assert "Which reading did you mean?" in text
    assert "ask_user" in text
    assert "Enter to submit" in text
    assert "clock paused while the agent waits" in text
    assert "Find sources" in text
    assert "researching" in text


def test_overlay_echoes_the_typed_answer_draft():
    renderer, buffer = _rich_renderer()

    renderer.emit(StageStarted("researching"))
    renderer.emit(Question("Which reading did you mean?"))
    renderer.emit(harness.display.AnswerDraft("sticker price", 0, 13))
    text = _strip_ansi(buffer.getvalue())
    renderer.close()

    assert "sticker price" in text


def test_overlay_question_is_not_parsed_as_console_markup():
    """The question is model-authored, and the overlay panel renders console markup.

    `[/var/log]` raised `MarkupError` and ended the run instead of asking it; `[a]` parsed as
    an unknown style and vanished from the question the developer was answering. Mirrors the
    old suspend-path test of the same name, now against the in-frame overlay.
    """
    renderer, buffer = _rich_renderer()

    renderer.emit(Question("Which log, [/var/log] or [a] the app's own?"))
    text = _strip_ansi(buffer.getvalue())
    renderer.close()

    assert "[/var/log]" in text
    assert "[a]" in text


def test_overlay_retracts_on_question_answered():
    renderer, buffer = _rich_renderer()

    renderer.emit(StageStarted("researching"))
    renderer.emit(Activity('search_web: "a query"'))
    renderer.emit(Question("Which region?"))
    # Timeline/activity lines live INSIDE the frame (a print under `screen=True` would be
    # discarded), so every later frame repeats whatever is still current -- assert on the
    # single frame painted by the retraction itself, not the whole cumulative buffer, which
    # still carries the earlier (open-overlay) frame's text.
    before = len(buffer.getvalue())
    renderer.emit(harness.display.QuestionAnswered())
    frame = _strip_ansi(buffer.getvalue()[before:])
    renderer.close()

    assert "Which region?" not in frame
    assert "clock paused while the agent waits" not in frame
    assert 'search_web: "a query"' in frame


def test_rich_renderer_displayed_clock_excludes_the_paused_interval():
    """R4/the mockup's own note: the stage line's `MM:SS` must exclude time spent with the
    overlay open. Asserted on the FORMATTED value actually painted, not a private field."""
    now = [0.0]
    renderer, buffer = _rich_renderer(clock=lambda: now[0])

    renderer.emit(StageStarted("researching"))
    now[0] = 5.0
    renderer.emit(Activity("tick"))  # unpaused so far: 5s
    renderer.emit(Question("Which region?"))
    now[0] = 65.0  # 60s pass while the overlay is open -- must not count
    renderer.emit(harness.display.QuestionAnswered())
    now[0] = 67.0  # 2 more unpaused seconds: 5 + 2 = 7s total
    renderer._live.refresh()
    text = _strip_ansi(buffer.getvalue())
    renderer.close()

    assert "00:07" in text
    assert "01:07" not in text


def test_stage_tracker_pause_resume_excludes_the_paused_interval():
    """The recorded `clarifying` timing must exclude the paused interval too — otherwise a
    `clarifying 183.0s` that is mostly human thinking time is a wrong number in the report."""
    renderer = _RecordingRenderer()
    now = [0.0]
    tracker = StageTracker(renderer, clock=lambda: now[0])

    tracker.advance("clarifying")
    now[0] = 1.0
    tracker.pause()
    now[0] = 50.0  # a long human wait, must not count
    tracker.resume()
    now[0] = 51.0
    tracker.advance("researching")

    assert tracker.timings() == (("clarifying", 2.0),)


def test_pausable_clock_half_pairing_does_not_corrupt_elapsed():
    """`pause()` while already paused and `resume()` while not paused are both no-ops — the
    overlay can be opened and retracted on paths that do not perfectly pair."""
    now = [0.0]
    clock = harness.display._PausableClock(lambda: now[0])

    clock.pause()
    clock.pause()  # already paused: must not double up
    now[0] = 10.0
    clock.resume()
    now[0] = 15.0

    assert clock.now() == 5.0  # 15 - 10s paused

    clock.resume()  # no matching pause: must be a no-op
    now[0] = 20.0

    assert clock.now() == 10.0  # 20 - 10s paused, unaffected by the spurious resume


def test_rich_renderer_suspend_stops_the_live_region_and_resume_repaints_after_marker():
    renderer, buffer = _rich_renderer()

    renderer.emit(StageStarted("researching"))
    with renderer.suspend():
        renderer._console.print("MARKER")

    text = _strip_ansi(buffer.getvalue())
    marker_index = text.index("MARKER")
    # The live region must restart on suspend exit and repaint the stage header — proving
    # the refresh thread was actually stopped, not merely hidden, during the suspended body.
    resume_index = text.index("researching", marker_index + len("MARKER"))
    assert resume_index > marker_index

    renderer.close()


def test_rich_renderer_close_while_suspended_does_not_repaint_the_stage():
    renderer, buffer = _rich_renderer()

    renderer.emit(StageStarted("researching"))
    length_at_close = None
    with renderer.suspend():
        renderer.close()
        length_at_close = len(buffer.getvalue())

    # Exiting the suspend body after a mid-prompt close() must not resurrect the live region.
    assert len(buffer.getvalue()) == length_at_close


# --- RunFinished summary tests (Phase 4) ---------------------------------------------------


def _render_lines(kind: str, event: RunFinished, capsys=None) -> list[str]:
    """Emit `event` through the named renderer kind and return its stripped output lines."""
    if kind == "plain":
        plain_renderer = PlainRenderer()
        plain_renderer.emit(event)
        plain_renderer.close()
        return drain_stdout(capsys)[1]
    rich_renderer, buffer = _rich_renderer()
    rich_renderer.emit(event)
    rich_renderer.close()
    text = _strip_ansi(buffer.getvalue())
    return [line for line in text.splitlines() if line.strip()]


@pytest.mark.parametrize("kind", ["plain", "rich"])
def test_run_finished_renders_the_full_summary(kind, capsys):
    event = RunFinished(
        stage_timings=(("researching", 12.0), ("verifying", 3.0)),
        usable_sources=3,
        unusable_sources=1,
        cut_short="wall_clock",
        verification_failures=2,
        incidents=3,
    )

    lines = _render_lines(kind, event, capsys)

    assert lines[0] == "summary:"
    assert "  researching 12.0s" in lines
    assert "  verifying 3.0s" in lines
    assert lines.index("  researching 12.0s") < lines.index("  verifying 3.0s")
    assert "  sources: 3 usable, 1 unusable" in lines
    assert "  cut short: wall clock" in lines
    assert "  verification failures: 2" in lines
    assert "  tool failures: 3" in lines


@pytest.mark.parametrize("kind", ["plain", "rich"])
def test_run_finished_omits_empty_sections(kind, capsys):
    event = RunFinished(
        stage_timings=(),
        usable_sources=4,
        unusable_sources=0,
        cut_short=None,
        verification_failures=0,
    )

    lines = _render_lines(kind, event, capsys)

    assert "  sources: 4 usable" in lines
    assert not any(line.startswith("  sources: 4 usable,") for line in lines)
    assert not any("cut short:" in line for line in lines)
    assert not any("verification failures:" in line for line in lines)
    assert not any("tool failures:" in line for line in lines)


@pytest.mark.parametrize("kind", ["plain", "rich"])
def test_run_finished_renders_the_report_path_as_the_trailing_block(kind, capsys):
    # `str(Path(...))` renders with the platform's native separator, so the expected value is
    # derived from the same `Path` rather than hardcoded — this test still asserts EQUALITY on
    # the whole stripped line, not `endswith`.
    report_path = pathlib.Path("/tmp/reports/2026-08-20-120000-q.md")
    event = RunFinished(
        stage_timings=(),
        usable_sources=1,
        unusable_sources=0,
        cut_short=None,
        verification_failures=0,
        report_path=report_path,
    )

    lines = _render_lines(kind, event, capsys)

    assert lines[-2].strip() == "report written"
    assert lines[-1].strip() == str(report_path)


@pytest.mark.parametrize("kind", ["plain", "rich"])
def test_run_finished_omits_the_report_block_when_no_report_was_written(kind, capsys):
    event = RunFinished(
        stage_timings=(),
        usable_sources=1,
        unusable_sources=0,
        cut_short=None,
        verification_failures=0,
        report_path=None,
    )

    lines = _render_lines(kind, event, capsys)

    assert not any("report written" in line for line in lines)
    assert lines[-1].strip().startswith("sources:")


def test_the_report_path_line_is_accent_coloured():
    event = RunFinished(
        stage_timings=(),
        usable_sources=1,
        unusable_sources=0,
        cut_short=None,
        verification_failures=0,
        report_path=pathlib.Path("/tmp/reports/2026-08-20-120000-q.md"),
    )
    renderer, buffer = _rich_renderer()

    renderer.emit(event)
    renderer.close()

    raw = buffer.getvalue()
    # The WHOLE path in ONE accent span, not merely the escape appearing somewhere: Rich's
    # `ReprHighlighter` shreds any raw `str` handed to `Console.print` into per-token colours,
    # and on POSIX it claims the whole path as magenta so the accent never appears at all. The
    # weaker `in raw` form passed on Windows against exactly that bug.
    assert _span(harness.display._ACCENT, str(event.report_path)) in raw


def test_a_report_path_containing_markup_brackets_still_renders():
    """`reports_dir` comes from `harness.toml`, so the path may contain `[` — which would raise
    `MarkupError` inside `emit` if the line were printed as a raw string, failing a run whose
    report was already written."""
    event = RunFinished(
        stage_timings=(),
        usable_sources=1,
        unusable_sources=0,
        cut_short=None,
        verification_failures=0,
        report_path=pathlib.Path("/tmp/re[po]rts/q.md"),
    )
    renderer, buffer = _rich_renderer()

    renderer.emit(event)
    renderer.close()

    assert str(event.report_path) in _strip_ansi(buffer.getvalue())


def test_a_long_report_path_is_not_wrapped_across_lines():
    """The block exists to hand the operator one copy-pasteable path; soft-wrapping it into
    fragments at the terminal width defeats that."""
    report_path = pathlib.Path("/tmp/" + "d" * 90 + "/q.md")
    event = RunFinished(
        stage_timings=(),
        usable_sources=1,
        unusable_sources=0,
        cut_short=None,
        verification_failures=0,
        report_path=report_path,
    )
    renderer, buffer = _rich_renderer()  # 80 columns, far narrower than the path

    renderer.emit(event)
    renderer.close()

    lines = [line for line in _strip_ansi(buffer.getvalue()).splitlines() if line.strip()]
    assert lines[-1].strip() == str(report_path)


def test_rich_renderer_run_finished_summary_prints_on_the_normal_screen_after_the_tui():
    """R5: the alternate screen vanishes on `RunFinished`, so the post-run summary must be
    visible AFTER leaving it, on the normal terminal, not trapped inside the alt-screen pair.
    """
    renderer, buffer = _rich_renderer()
    renderer.emit(TodosUpdated((TodoItem(content="Find sources", status="pending"),)))

    renderer.emit(
        RunFinished(
            stage_timings=(),
            usable_sources=1,
            unusable_sources=0,
            cut_short=None,
            verification_failures=0,
        )
    )
    renderer.close()

    raw = buffer.getvalue()
    summary_index = raw.index("summary:")
    last_exit_index = raw.rindex("\x1b[?1049l")
    assert last_exit_index < summary_index
    # No FURTHER alt-screen entry after the summary — it stays on the normal screen.
    assert "\x1b[?1049h" not in raw[summary_index:]


# --- Phase 3: running pane (task meta, stage elapsed/round) ------------------------------


def test_rich_renderer_todo_item_renders_meta_when_present():
    renderer, buffer = _rich_renderer()

    renderer.emit(
        TodosUpdated((TodoItem(content="Find sources", status="completed", meta="14 sources"),))
    )
    text = _strip_ansi(buffer.getvalue())
    renderer.close()

    assert "Find sources" in text
    assert "14 sources" in text


def test_rich_renderer_todo_item_omits_meta_fragment_when_absent():
    renderer, buffer = _rich_renderer()

    renderer.emit(TodosUpdated((TodoItem(content="Find sources", status="pending"),)))
    text = _strip_ansi(buffer.getvalue())
    renderer.close()

    lines = [line for line in text.splitlines() if "Find sources" in line]
    assert lines[0].strip() == "[ ] Find sources"


def test_rich_renderer_stage_line_shows_round_after_rounds_updated():
    renderer, buffer = _rich_renderer(clock=_fixed_elapsed_clock(252))

    renderer.emit(StageStarted("researching"))
    before = len(buffer.getvalue())
    renderer.emit(RoundsUpdated(9, 50))
    frame = _strip_ansi(buffer.getvalue()[before:])
    renderer.close()

    assert "04:12 · round 9/50" in frame


# --- Phase 4 (fetch lifecycle and TUI hygiene): live source counter (R5, D5) -------------


def test_rich_renderer_frame_shows_the_source_count():
    renderer, buffer = _rich_renderer()

    renderer.emit(StageStarted("researching"))
    renderer.emit(harness.display.SourcesUpdated(7))
    frame = _strip_ansi(buffer.getvalue())

    assert "sources: 7" in frame

    before = len(buffer.getvalue())
    renderer.emit(harness.display.SourcesUpdated(9))
    frame = _strip_ansi(buffer.getvalue()[before:])
    renderer.close()

    # A replacement, not an accumulating log -- the new frame carries only the new count.
    assert "sources: 9" in frame
    assert "sources: 7" not in frame


def test_the_source_count_survives_the_ask_user_overlay():
    """`_build_activity_group` returns from TWO `Group(...)` sites (the overlay branch and
    the normal branch) -- this fails if the counter line is added to only one of them.
    """
    renderer, buffer = _rich_renderer()

    renderer.emit(StageStarted("researching"))
    renderer.emit(harness.display.SourcesUpdated(4))
    # Slice to the single frame the overlay itself paints, not the cumulative buffer: the
    # `SourcesUpdated(4)` frame above already carries "sources: 4", so a whole-buffer assert
    # would pass even with the counter appended to the non-overlay branch alone -- exactly
    # the bug this test exists to catch. Same pattern as
    # `test_overlay_retracts_on_question_answered`.
    before = len(buffer.getvalue())
    renderer.emit(Question("Which region?"))
    frame = _strip_ansi(buffer.getvalue()[before:])
    renderer.close()

    assert "sources: 4" in frame


def test_plain_renderer_prints_the_source_count(capsys):
    renderer = PlainRenderer()

    renderer.emit(harness.display.SourcesUpdated(3))

    out, lines = drain_stdout(capsys)
    assert any("sources: 3" in line for line in lines)


def test_rich_renderer_elapsed_advances_on_refresh_without_any_new_event():
    """The clock must tick between events, not only when one arrives.

    `Live` redraws whatever renderable it holds, so handing it a pre-built `Group` froze
    `MM:SS` until the next event (one `RoundsUpdated` per model turn, tens of seconds
    apart). Passing the builder as `get_renderable` is what makes a bare refresh repaint a
    newly computed elapsed — this test fails against a static frame.
    """
    # A settable "now" rather than a fixed sequence: the number of `clock()` reads per frame
    # is Rich's business, and the point being pinned is that a REPAINT re-reads it at all.
    now = [0.0]
    renderer, buffer = _rich_renderer(clock=lambda: now[0])

    renderer.emit(StageStarted("researching"))

    # Advance the clock and repaint with NO new event — exactly what auto-refresh does on a
    # live terminal between model turns.
    now[0] = 60.0
    before = len(buffer.getvalue())
    renderer._live.refresh()
    first = _strip_ansi(buffer.getvalue()[before:])

    now[0] = 252.0
    before = len(buffer.getvalue())
    renderer._live.refresh()
    second = _strip_ansi(buffer.getvalue()[before:])
    renderer.close()

    assert "01:00" in first
    assert "04:12" in second


def test_rich_renderer_stage_line_shows_elapsed_only_before_rounds_updated():
    renderer, buffer = _rich_renderer(clock=_fixed_elapsed_clock(252))

    renderer.emit(StageStarted("researching"))
    text = _strip_ansi(buffer.getvalue())
    renderer.close()

    assert "04:12" in text
    assert "round" not in text


def test_plain_renderer_rounds_updated_produces_no_output(capsys):
    renderer = PlainRenderer()

    renderer.emit(RoundsUpdated(rounds_used=3, max_rounds=50))

    out, lines = drain_stdout(capsys)
    assert lines == []
    assert out == ""


@pytest.mark.parametrize(
    ("elapsed_seconds", "expected"),
    [
        (0, "00:00"),
        (5, "00:05"),
        (65, "01:05"),
        (252, "04:12"),
        (3600, "60:00"),
        (3661, "61:01"),
    ],
)
def test_rich_renderer_stage_line_elapsed_formatting_table(elapsed_seconds, expected):
    renderer, buffer = _rich_renderer(clock=_fixed_elapsed_clock(elapsed_seconds))

    renderer.emit(StageStarted("researching"))
    text = _strip_ansi(buffer.getvalue())
    renderer.close()

    assert expected in text


# --- Phase 2: welcome screen (build_welcome, build_help_panel, build_model_picker) -------


def _welcome_view(**overrides: Any) -> WelcomeView:
    defaults: dict[str, Any] = dict(
        question="",
        cursor_col=0,
        head_model="glm-5.3",
        roles=(
            ("researcher", "deepseek-v4-pro"),
            ("reader", "deepseek-v4-flash"),
            ("verifier", "gpt-5.6-luna"),
        ),
        budget="50 rounds / 30 min",
        status_left="~/deep-research:searxng@localhost:8080",
        status_right="0.1.0",
    )
    defaults.update(overrides)
    return WelcomeView(**defaults)


def _render(renderable: Any) -> str:
    console, buffer = _make_console()
    console.print(renderable)
    return _strip_ansi(buffer.getvalue())


def _cursor_spans(row: Any) -> list[tuple[int, int]]:
    """(start, end) of the reverse-video block cursor within one rendered ask row."""
    cursor_style = f"{harness.display._SURFACE} on {harness.display._FG}"
    return [(span.start, span.end) for span in row.spans if span.style == cursor_style]


def test_ask_rows_place_the_block_cursor_on_its_own_row_not_the_joined_text():
    """`cursor_col` is a column WITHIN `cursor_row`, not an offset into `"\\n".join(lines)`.

    Indexing the joined string draws the block over the newline — visually parking the
    cursor at the end of line 1 no matter where the caret really is.
    """
    view = _welcome_view(question="ab\ncd", cursor_row=1, cursor_col=2)

    rows = harness.display._build_ask_rows(view)

    # Row 1 gains a trailing space: the caret sits past the last character.
    assert [row.plain for row in rows] == ["ab", "cd "]
    assert _cursor_spans(rows[0]) == []
    assert _cursor_spans(rows[1]) == [(2, 3)]


def test_ask_rows_put_the_cursor_mid_line_on_the_first_row():
    view = _welcome_view(question="ab\ncd", cursor_row=0, cursor_col=1)

    rows = harness.display._build_ask_rows(view)

    assert _cursor_spans(rows[0]) == [(1, 2)]
    assert _cursor_spans(rows[1]) == []


def test_every_ask_row_gets_its_own_accent_bar():
    """The mockup's left bar runs down the whole input box, not just its first line."""
    single = _render(build_welcome(_welcome_view(question="ab", cursor_col=2)))
    multi = _render(build_welcome(_welcome_view(question="ab\ncd", cursor_row=1, cursor_col=2)))

    # One bar per ask row, plus one for the mode row.
    assert single.count("▌") == 2
    assert multi.count("▌") == 3


def test_build_welcome_renders_hint_labels_and_current_non_default_head_model():
    view = _welcome_view(head_model="glm-5.3")

    text = _render(build_welcome(view))

    for key, label in (
        ("enter", "run"),
        ("/", "commands"),
        ("ctrl+j", "newline"),
        ("ctrl+c", "exit"),
    ):
        assert key in text
        assert label in text
    assert "researcher" in text
    assert "deepseek-v4-pro" in text
    assert "reader" in text
    assert "deepseek-v4-flash" in text
    assert "verifier" in text
    assert "gpt-5.6-luna" in text
    assert "50 rounds / 30 min" in text
    # Proves the head model is read from the view, not hardcoded to the shipped default.
    assert "glm-5.3" in text


def test_build_welcome_shows_placeholder_when_question_is_empty():
    view = _welcome_view(question="")

    text = _render(build_welcome(view))

    assert "Ask anything" in text


def test_build_welcome_shows_typed_text_when_question_is_non_empty():
    view = _welcome_view(question="what does Acme charge", cursor_col=5)

    text = _render(build_welcome(view))

    assert "what does Acme charge" in text
    assert "Ask anything" not in text


def test_the_stage_spinner_advances_its_frame_across_renders():
    """PR #25 review: a fresh `Spinner` per `_build_stage_header` call re-rendered frame 0
    forever (`Spinner.render` derives the frame from elapsed time since its OWN first render),
    so the glyph never rotated. One renderer-held spinner animates across frames."""
    now = {"value": 0.0}
    console, buffer = _make_console(get_time=lambda: now["value"])
    renderer = RichRenderer(console=console, auto_refresh=False)

    console.print(renderer._build_stage_header())
    first = _strip_ansi(buffer.getvalue())
    buffer.truncate(0)
    buffer.seek(0)
    now["value"] = 0.4  # five "dots" frames later (80ms per frame)
    console.print(renderer._build_stage_header())
    second = _strip_ansi(buffer.getvalue())

    assert first != second


def test_build_welcome_centers_the_hero_and_pins_the_status_bar_to_the_bottom():
    """The mockup's `#screen-welcome`: hero centered on both axes, status bar on the bottom
    row — not the whole screen stacked against the top of the terminal (PR #25 review)."""
    text = _render(build_welcome(_welcome_view()))
    lines = text.splitlines()

    assert "0.1.0" in lines[-1]
    assert "~/deep-research:searxng@localhost:8080" in lines[-1]
    wordmark_row = next(i for i, line in enumerate(lines) if "█" in line)
    # Vertical centering: blank rows precede the wordmark instead of it sitting on row 0.
    assert wordmark_row > 0
    assert all(not lines[i].strip() for i in range(wordmark_row))
    # Horizontal centering: the wordmark is indented off the left edge.
    assert lines[wordmark_row].startswith(" ")


def test_build_welcome_tip_line_names_help_and_never_sources():
    view = _welcome_view()

    text = _render(build_welcome(view))

    assert "Run /help to see available commands" in text
    assert "/sources" not in text


def test_build_help_panel_lists_every_command_in_the_dispatch_table():
    commands = [(cmd.name, cmd.summary) for cmd in main_module._COMMANDS.values()]
    assert len(commands) >= 2  # guards against an empty table silently passing

    text = _render(build_help_panel(commands))

    for name, summary in commands:
        assert name in text
        assert summary in text


def test_build_model_picker_highlights_selected_and_marks_current():
    choices = ["glm-5.2", "glm-5.3", "kimi-k3"]

    text = _render(build_model_picker(choices, selected=1, current="kimi-k3"))

    lines = text.splitlines()
    selected_line = next(line for line in lines if "glm-5.3" in line)
    current_line = next(line for line in lines if "kimi-k3" in line)
    assert ">" in selected_line
    assert "(current)" in current_line


def test_build_model_picker_windows_a_long_list_with_truncation_affordances():
    choices = [f"model-{i}" for i in range(19)]

    text = _render(build_model_picker(choices, selected=10, current="model-0"))

    # `re.escape(choice)(?!\d)`, not plain substring: "model-1" is a substring of "model-10"
    # through "model-19", which would overcount how many distinct rows actually rendered.
    rendered_choices = [
        choice for choice in choices if re.search(rf"{re.escape(choice)}(?!\d)", text)
    ]
    assert len(rendered_choices) <= 12
    assert "…" in text


def test_stage_tracker_timings_returns_completed_pairs_in_order():
    renderer = _RecordingRenderer()
    tracker = StageTracker(renderer, clock=_fake_clock([0.0, 1.0, 4.0]))

    tracker.advance("researching")
    tracker.advance("verifying")
    tracker.finish()

    assert tracker.timings() == (("researching", 1.0), ("verifying", 3.0))


async def test_a_full_run_prints_a_summary_above_the_report_path(
    make_config, monkeypatch, scripted_model, capsys
):
    config = make_config()
    # D3: the answer is delivered by `submit_report`, then one more turn closes the tool call.
    final = _submit_call("Final answer.").model_copy(
        update={"usage_metadata": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
    )
    model = scripted_model([final, AIMessage(content="Report submitted.")])
    patch_run(monkeypatch, config, model)

    await main_module.main(["a question with no tool calls"])

    out, lines = drain_stdout(capsys)
    assert "summary:" in out
    assert any(line.strip().startswith("sources:") for line in lines)
    assert lines[-1].strip().endswith(".md")


async def test_a_full_run_prints_the_report_block_with_the_bare_path_last(
    make_config, monkeypatch, scripted_model, capsys
):
    config = make_config()
    # D3: the answer is delivered by `submit_report`, then one more turn closes the tool call.
    final = _submit_call("Final answer.").model_copy(
        update={"usage_metadata": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
    )
    model = scripted_model([final, AIMessage(content="Report submitted.")])
    patch_run(monkeypatch, config, model)

    await main_module.main(["a question with no tool calls"])

    out, lines = drain_stdout(capsys)
    assert "report written" in out
    assert lines[-2].strip() == "report written"
    report_path = pathlib.Path(lines[-1].strip())
    assert report_path.exists()
    assert report_path.parent == config.agent.reports_dir
    assert out.count(lines[-1].strip()) == 1


async def test_the_summary_counts_real_usable_and_unusable_sources(
    make_config, monkeypatch, scripted_model, capsys
):
    """Drives the counting itself, which a `sources:`-prefix assertion cannot: both counts
    come from `report.partition_sources`, so the summary cannot drift from the report body.
    """
    config = make_config()
    # `main` builds the registry itself, so a pre-populated one is injected in its place,
    # standing in for the `fetch_pages` calls a real run would have made — one page that came
    # back, one that did not.
    registry = SourceRegistry(run_id="2020-01-01-000000")
    fetched = registry.add("https://example.test/pricing", title="Pricing")
    registry.add("https://example.test/blocked", title="Blocked")
    write_source_capture(config, registry, fetched, "Acme lists $5.10 per unit.")
    # The "blocked" source is registered but never captured: the new convention writes no file
    # at all for a failed fetch, so "no file" is the only unusable shape left to simulate.
    monkeypatch.setattr(main_module, "SourceRegistry", lambda run_id: registry)

    final = AIMessage(
        content=f"Acme lists $5.10 per unit [{fetched}].",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    model = scripted_model([final, verify_reply(True, "The page states $5.10.")])
    patch_run(monkeypatch, config, model)

    await main_module.main(["what does acme charge"])

    _, lines = drain_stdout(capsys)
    assert "  sources: 1 usable, 1 unusable" in lines
    # Proves the cited-claim leg actually ran the verify call rather than the run ending on
    # an unconsumed leading reply (which would leave `model._call_count` at 1 and no verdict
    # line at all): both the final answer AND the verify reply above were consumed.
    assert model._call_count == 2


async def test_a_failing_report_write_still_closes_the_display(
    make_config, monkeypatch, scripted_model
):
    """`Live.start` hides the cursor and rich registers no atexit restore, so a `write_report`
    error escaping `main` left the developer's shell cursorless behind the traceback.
    """
    config = make_config()
    renderer = _RecordingRenderer()
    monkeypatch.setattr(main_module, "build_renderer", lambda: renderer)

    def _unwritable(outcome: Any, cfg: Any) -> Any:
        raise OSError("reports dir is not writable")

    monkeypatch.setattr(session_module, "write_report", _unwritable)

    final = _submit_call("Final answer.").model_copy(
        update={"usage_metadata": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
    )
    model = scripted_model([final, AIMessage(content="Report submitted.")])
    patch_run(monkeypatch, config, model)

    with pytest.raises(OSError, match="reports dir is not writable"):
        await main_module.main(["a question whose report cannot be written"])

    assert renderer.closes == 1


async def test_failed_run_error_prints_only_after_the_renderer_is_closed(
    make_config, monkeypatch, scripted_model, capsys
):
    """Under `Live(screen=True)` anything printed before the Live stops lands on the alternate
    screen and is discarded with it. capsys cannot see that discard, so this pins the fix by
    ordering instead: the `error:` detail must reach stderr only AFTER `renderer.close()`.
    """

    class _CloseMarkingRenderer(_RecordingRenderer):
        def close(self) -> None:
            super().close()
            print("<renderer closed>", file=sys.stderr)

    config = make_config()
    renderer = _CloseMarkingRenderer()
    monkeypatch.setattr(main_module, "build_renderer", lambda: renderer)

    plan_call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_todos",
                "args": {"todos": [{"content": "Investigate", "status": "in_progress"}]},
                "id": "call_1",
            }
        ],
    )
    model = scripted_model([plan_call])  # no second response — the run dies here
    patch_run(monkeypatch, config, model)

    exit_code = await main_module.main(["question whose run dies mid-flight"])

    err = capsys.readouterr().err
    assert exit_code == 1
    marker_at = err.find("<renderer closed>")
    error_at = err.find("error:")
    assert marker_at != -1 and error_at != -1, err
    assert marker_at < error_at, f"the error printed while the Live still owned the screen: {err!r}"


async def test_a_round_cap_cut_short_run_shows_the_reason_in_the_summary(
    make_config, monkeypatch, scripted_model, tmp_path, capsys
):
    agent = AgentSettings(
        max_rounds=1, workspace_dir=tmp_path / "workspace", reports_dir=tmp_path / "reports"
    )
    config = make_config(agent=agent)
    keep_going = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_todos",
                "args": {"todos": [{"content": "Keep researching", "status": "in_progress"}]},
                "id": "call",
            }
        ],
    )
    # A lead that keeps proposing tool calls until the bounded synthesis pass reaches it, then
    # submits — the only route to a report on a cut-short run (D3).
    model = _lead_model(replies=[keep_going], answer="Only what the cap allowed.")
    patch_run(monkeypatch, config, model)

    await main_module.main(["question that never settles"])

    out, lines = drain_stdout(capsys)
    assert "cut short: round cap" in out
    assert lines[-1].strip().endswith(".md")


@pytest.mark.parametrize("kind", ["plain", "rich"])
def test_alert_renders_as_a_bounded_warning_line(kind, capsys):
    """Renamed from `..._as_a_persistent_warning_line` (D4): alerts are no longer persistent
    in the live TUI — the rolling window evicts them. A short detail still renders verbatim;
    the window/total behavior is pinned by the eviction test below."""
    event = Alert('search for "solar" failed: unreachable')

    lines = _render_lines(kind, event, capsys)

    assert any('warning: search for "solar" failed: unreachable' in line for line in lines)


@pytest.mark.parametrize("kind", ["plain", "rich"])
def test_alert_with_a_crawl4ai_shaped_dump_renders_as_one_capped_line(kind, capsys):
    """R3/D3: a multi-line upstream dump collapses to exactly one bounded line, and nothing
    past the first source line leaks into the output — a length check alone would also pass
    on a helper that wrapped instead of cutting, so this pins the cut itself."""
    leak_token = "SHOULD_NOT_APPEAR_BEYOND_FIRST_LINE"
    trailing_lines = [
        f"{leak_token} call log line {i} with padding to make this realistically long"
        for i in range(20)
    ]
    detail = "\n".join(["fetch failed for https://example.com/report"] + trailing_lines)
    event = Alert(detail)

    lines = _render_lines(kind, event, capsys)

    warning_lines = [line for line in lines if "warning:" in line]
    assert len(warning_lines) == 1
    assert len(warning_lines[0]) <= harness.display._ALERT_MAX_CHARS + len("warning: ")
    assert leak_token not in "\n".join(lines)


def test_bound_alert_truncates_an_enormous_single_line_at_the_cap():
    """The cap branch itself (PR review, Phase 3). The multi-line test above cannot reach it:
    its first source line is well under the cap, so its length assertion passes vacuously and
    an off-by-one or a dropped `.rstrip()` in the truncation would go unnoticed."""
    cap = harness.display._ALERT_MAX_CHARS
    tail = "TAIL_PAST_THE_CAP"
    detail = "E " + ("x" * (cap * 3)) + tail

    bounded = harness.display._bound_alert(detail)

    assert len(bounded) == cap
    assert bounded.endswith("...")
    assert tail not in bounded
    # One char under the cap is returned untouched -- pins the boundary from the other side.
    assert harness.display._bound_alert("y" * (cap - 1)) == "y" * (cap - 1)
    assert harness.display._bound_alert("y" * cap) == "y" * cap


def test_a_long_alert_occupies_one_row_inside_the_live_frame_on_a_narrow_terminal():
    """R3 literally, and the reason `no_wrap`/`overflow="ellipsis"` are set (PR review).

    The parametrized test above renders the `rich` arm through the no-Live fallback
    (`_console.print`), never the windowed in-frame group, so nothing there pins the one-ROW
    property. A 40-column console makes wrapping visible: the char cap alone would spill a
    capped line across two rows.
    """
    console, buffer = _make_console(width=40)
    renderer = RichRenderer(console=console, auto_refresh=False)
    renderer.emit(StageStarted("researching"))
    detail = "fetch failed for https://example.com/a-very-long-path-that-keeps-going-and-going"
    before = len(buffer.getvalue())

    renderer.emit(Alert(detail))

    frame_lines = _strip_ansi(buffer.getvalue()[before:]).splitlines()
    warning_rows = [line for line in frame_lines if "warning:" in line]
    assert len(warning_rows) == 1
    # The tail must be truncated away, not wrapped onto a following row.
    assert not any("keeps-going" in line for line in frame_lines)
    renderer.close()


def test_rich_renderer_alert_window_evicts_the_oldest_and_the_running_total_counts_all(capsys):
    renderer, buffer = _rich_renderer()
    # A stage must be running for alerts to render inside the live frame (where the window
    # and total line live) rather than print straight to the normal terminal.
    renderer.emit(StageStarted("researching"))
    renderer.emit(Alert("alert one"))
    renderer.emit(Alert("alert two"))
    renderer.emit(Alert("alert three"))
    before = len(buffer.getvalue())
    renderer.emit(Alert("alert four"))
    frame = _strip_ansi(buffer.getvalue()[before:])

    assert "alert one" not in frame
    assert "alert two" in frame
    assert "alert three" in frame
    assert "alert four" in frame
    assert "warnings: 4 this run" in frame
    renderer.close()  # releases Live's stdout redirect before the plain renderer prints below

    plain_renderer = PlainRenderer()
    for text in ["alert one", "alert two", "alert three", "alert four"]:
        plain_renderer.emit(Alert(text))
    _, plain_lines = drain_stdout(capsys)
    for text in ["alert one", "alert two", "alert three", "alert four"]:
        assert any(text in line for line in plain_lines)


def test_run_finished_summary_reports_the_full_incident_count_past_the_alert_window():
    renderer, buffer = _rich_renderer()
    for i in range(5):
        renderer.emit(Alert(f"alert {i}"))
    event = RunFinished(
        stage_timings=(),
        usable_sources=1,
        unusable_sources=0,
        cut_short=None,
        verification_failures=0,
        incidents=5,
    )

    renderer.emit(event)

    text = _strip_ansi(buffer.getvalue())
    assert "  tool failures: 5" in text


def test_plain_renderer_alert_survives_a_non_cp1252_character_on_a_cp1252_stream(monkeypatch):
    """LATER-PROBLEMS.md: redirected stdout on Windows is cp1252, and one non-ASCII byte in
    an upstream error string used to raise `UnicodeEncodeError` out of `emit` and abort the
    whole run. A `TextIOWrapper` over `BytesIO` with `encoding="cp1252", errors="strict"`
    reproduces the real failure mode -- confirmed below to raise without the fix."""
    import io

    raw = io.BytesIO()
    cp1252_stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    non_cp1252_char = "中"  # a CJK character: not in cp1252's repertoire
    monkeypatch.setattr(sys, "stdout", cp1252_stream)

    with pytest.raises(UnicodeEncodeError):
        print(f"warning: {non_cp1252_char}", file=cp1252_stream)

    renderer = PlainRenderer()
    renderer.emit(Alert(f"fetch failed: {non_cp1252_char}"))
    cp1252_stream.flush()

    raw.seek(0)
    written = raw.read().decode("cp1252")
    assert "warning:" in written
    assert non_cp1252_char not in written


def test_plain_renderer_survives_a_non_cp1252_character_on_every_branch(monkeypatch):
    """PR review, Phase 3: the alert path was never the only exposure.

    `ToolCall.result_summary` comes from fetched page content and `Activity.text` from
    model-authored prose, so both carry arbitrary web Unicode — and both were bare-printed.
    The fix moved to `emit`'s single `out()` boundary, so this pins the whole class rather
    than one branch; a future branch that prints directly would fail here.
    """
    import io

    cjk = "中"  # not in cp1252's repertoire
    events = [
        StageStarted(f"researching {cjk}"),
        Activity(f"reading {cjk}"),
        ToolCall(
            call_id="c1",
            tool="fetch_pages",
            arg_summary=f"https://example.com/{cjk}",
            result_summary=f"digested {cjk}",
        ),
        Alert(f"fetch failed: {cjk}"),
    ]

    for event in events:
        raw = io.BytesIO()
        cp1252_stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        monkeypatch.setattr(sys, "stdout", cp1252_stream)

        PlainRenderer().emit(event)  # must not raise

        cp1252_stream.flush()
        raw.seek(0)
        written = raw.read().decode("cp1252")
        assert cjk not in written
        assert written.strip(), f"{type(event).__name__} printed nothing at all"


async def test_a_dead_search_backend_is_disclosed_on_the_terminal_and_in_the_report(
    make_config, monkeypatch, scripted_model, capsys
):
    """The invariant-pinning path (best-effort + disclose): a SearchFailure must reach the
    developer through the CLI as a warning line AND through the report's gaps section — not
    only through model-facing tool output the model may never repeat.

    `search_web` lives on the researcher, so the lead first dispatches one — a single failure
    does not trip `SearchUnavailableError` (the default `max_consecutive_failures` is 3), so
    the researcher reports back and the lead still submits a best-effort answer.
    """
    config = make_config()
    search_call = AIMessage(
        content="",
        tool_calls=[{"name": "search_web", "args": {"query": "the answer"}, "id": "call_search"}],
    )
    researcher_report = AIMessage(content="Researcher report: no sources found.")
    head_model = scripted_model(
        [
            _dispatch_call("Search for the answer"),
            AIMessage(content="Angle dispatched."),
            _submit_call("Best-effort answer without sources.").model_copy(
                update={
                    "usage_metadata": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                    }
                }
            ),
            AIMessage(content="Report submitted."),
        ]
    )
    researcher_model = scripted_model([search_call, researcher_report])
    patch_run_by_role(monkeypatch, config, {"head": head_model, "researcher": researcher_model})

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    install_search_transport(monkeypatch, handler)

    await main_module.main(["a question needing research"])

    out, lines = drain_stdout(capsys)
    warning_lines = [line for line in lines if line.startswith("warning:")]
    assert any("unreachable" in line and "the answer" in line for line in warning_lines)
    assert "  tool failures: 1" in out

    report_path = lines[-1].strip()
    body = pathlib.Path(report_path).read_text(encoding="utf-8")
    assert "## Gaps and disclosures" in body
    assert "Tool failures during the run:" in body
    assert "unreachable" in body


async def test_a_full_run_frame_shows_the_counter_alongside_alerts_and_todos(
    make_config, monkeypatch, scripted_model
):
    """The phase's acceptance criterion (R5): the todo checklist, the R4 alert total, and the
    new R5 source counter all coexist in the same frame -- no layout regression from adding
    the third. Template: the dead-search-backend test above, plus a pre-populated registry
    (the `SourceRegistry` injection `test_the_summary_counts_real_usable_and_unusable_sources`
    uses) standing in for a real fetch.
    """
    config = make_config()

    registry = SourceRegistry(run_id="2020-01-01-000000")
    registry.add("https://example.test/pricing", title="Pricing")
    monkeypatch.setattr(main_module, "SourceRegistry", lambda run_id: registry)

    todos_call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_todos",
                "args": {"todos": [{"content": "Find sources", "status": "pending"}]},
                "id": "call_todos",
            }
        ],
    )
    search_call = AIMessage(
        content="",
        tool_calls=[{"name": "search_web", "args": {"query": "the answer"}, "id": "call_search"}],
    )
    researcher_report = AIMessage(content="Researcher report: no sources found.")
    head_model = scripted_model(
        [
            todos_call,
            _dispatch_call("Search for the answer"),
            AIMessage(content="Angle dispatched."),
            _submit_call("Best-effort answer.").model_copy(
                update={
                    "usage_metadata": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                    }
                }
            ),
            AIMessage(content="Report submitted."),
        ]
    )
    researcher_model = scripted_model([search_call, researcher_report])
    patch_run_by_role(monkeypatch, config, {"head": head_model, "researcher": researcher_model})

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    install_search_transport(monkeypatch, handler)

    renderer, buffer = _rich_renderer()
    monkeypatch.setattr(main_module, "build_renderer", lambda: renderer)

    await main_module.main(["a question needing research"])

    # The cumulative buffer, deliberately: this test's claim is that all three reach the
    # renderer in a REAL run. It cannot claim they share one frame -- `RunFinished` stops
    # the Live and nulls `_live`, so no frame can be repainted afterwards to check. The
    # one-frame coexistence half of the acceptance criterion is
    # `test_the_counter_shares_one_frame_with_the_alert_total_and_todos` below.
    text = _strip_ansi(buffer.getvalue())

    assert "Find sources" in text
    assert "warnings: 1 this run - full list in report" in text
    assert "sources: 1" in text


def test_the_counter_shares_one_frame_with_the_alert_total_and_todos():
    """The layout half of Phase 4's acceptance criterion: counter present ALONGSIDE the
    alert total and the checklist, in a single painted frame -- not merely somewhere in the
    cumulative buffer. Fails if a new persistent line displaces either of the others.
    """
    renderer, buffer = _rich_renderer()

    renderer.emit(StageStarted("researching"))
    renderer.emit(TodosUpdated((TodoItem(content="Find sources", status="in_progress"),)))
    renderer.emit(Alert("fetch failed for https://a.test"))
    before = len(buffer.getvalue())
    renderer.emit(harness.display.SourcesUpdated(1))
    frame = _strip_ansi(buffer.getvalue()[before:])
    renderer.close()

    assert "Find sources" in frame
    assert "fetch failed for https://a.test" in frame
    assert "warnings: 1 this run - full list in report" in frame
    assert "sources: 1" in frame


# --- Phase 6: structured tool-call log + reader-strip visibility hook --------------------


def test_rich_renderer_tool_log_renders_tool_arg_result_and_flags_a_retry_row():
    renderer, buffer = _rich_renderer()

    renderer.emit(StageStarted("researching"))
    renderer.emit(ToolCall(call_id="c1", tool="search_web", arg_summary="university rankings"))
    before = len(buffer.getvalue())
    renderer.emit(
        ToolCall(
            call_id="c1",
            tool="search_web",
            arg_summary="university rankings",
            result_summary="11 results",
            elapsed_seconds=2.3,
        )
    )
    renderer.emit(
        ToolCall(
            call_id="c2",
            tool="fetch_raw",
            arg_summary="https://example.test/report",
            result_summary="fetched",
            elapsed_seconds=1.1,
            retry=True,
        )
    )
    text = _strip_ansi(buffer.getvalue())
    raw = buffer.getvalue()[before:]
    renderer.close()

    assert "search_web" in text
    assert "university rankings" in text
    assert "11 results" in text
    # The completion emit's own frame replaced the running row -- checked against the
    # buffer written AFTER the start emit's frame, since that earlier frame legitimately
    # painted "running..." once before being replaced.
    assert "running..." not in _strip_ansi(raw)
    # The retry row's tool name is pinned as a whole-value _WARN span, not merely present.
    assert _span(harness.display._WARN, "fetch_raw") in raw


def test_rich_renderer_tool_log_truncates_an_overlong_arg_summary_with_an_ellipsis():
    renderer, buffer = _rich_renderer()

    renderer.emit(StageStarted("researching"))
    long_summary = "https://example.test/" + "a" * 200
    renderer.emit(
        ToolCall(
            call_id="c1",
            tool="fetch_raw",
            arg_summary=long_summary,
            result_summary="fetched",
            elapsed_seconds=1.0,
        )
    )
    frame = _strip_ansi(buffer.getvalue())
    renderer.close()

    lines = [line for line in frame.splitlines() if "fetch_raw" in line]
    assert len(lines) == 1  # truncated onto one physical line, not wrapped onto a second
    assert "…" in frame
    assert long_summary not in frame


def test_rich_renderer_a_second_tool_call_with_the_same_id_replaces_the_running_row():
    renderer, buffer = _rich_renderer()

    renderer.emit(StageStarted("researching"))
    renderer.emit(ToolCall(call_id="c1", tool="search_web", arg_summary="a query"))
    before = len(buffer.getvalue())
    renderer.emit(
        ToolCall(
            call_id="c1",
            tool="search_web",
            arg_summary="a query",
            result_summary="11 results",
            elapsed_seconds=2.3,
        )
    )
    frame = _strip_ansi(buffer.getvalue()[before:])
    renderer.close()

    assert "running..." not in frame
    assert frame.count("search_web") == 1


def test_rich_renderer_reader_strip_is_absent_with_no_live_readers():
    renderer, buffer = _rich_renderer()

    renderer.emit(StageStarted("researching"))
    renderer.emit(ReadersUpdated(()))
    text = _strip_ansi(buffer.getvalue())
    renderer.close()

    assert "reader/1" not in text


def test_rich_renderer_reader_strip_renders_a_live_row():
    renderer, buffer = _rich_renderer()

    renderer.emit(StageStarted("researching"))
    before = len(buffer.getvalue())
    renderer.emit(
        ReadersUpdated(
            (
                ReaderItem(
                    id="reader/1", brief="Angle A", status_text="fetch_pages . 3s", done=False
                ),
            )
        )
    )
    frame = _strip_ansi(buffer.getvalue()[before:])
    renderer.close()

    assert "reader/1" in frame
    assert "Angle A" in frame
    assert "fetch_pages . 3s" in frame


def test_rich_renderer_reader_strip_uses_ok_for_a_done_row():
    renderer, buffer = _rich_renderer()

    renderer.emit(StageStarted("researching"))
    renderer.emit(
        ReadersUpdated(
            (
                ReaderItem(id="reader/1", brief="Angle A", status_text="dispatched", done=False),
                ReaderItem(id="reader/2", brief="Angle B", status_text="dispatched", done=False),
            )
        )
    )
    before = len(buffer.getvalue())
    # reader/1 finishes but reader/2 is still live -- per fix-pass item 2 the strip only
    # disappears once EVERY reader is done, so this is the mockup's "a finished row beside a
    # live one" shape, not the all-done case (covered by its own test below).
    renderer.emit(
        ReadersUpdated(
            (
                ReaderItem(id="reader/1", brief="Angle A", status_text="done . 8s", done=True),
                ReaderItem(id="reader/2", brief="Angle B", status_text="dispatched", done=False),
            )
        )
    )
    raw = buffer.getvalue()[before:]
    renderer.close()

    assert _span(harness.display._OK, "reader/1") in raw


def test_rich_renderer_reader_strip_is_absent_once_every_reader_is_done():
    """Fix-pass item 2: `ReadersUpdated(())` (no readers at all) is already covered above, but
    that emits no strip for a trivial reason. An all-DONE, non-empty tuple is the real gap: the
    strip must vanish here too, not linger until `StageCompleted`."""
    renderer, buffer = _rich_renderer()

    renderer.emit(StageStarted("researching"))
    renderer.emit(
        ReadersUpdated(
            (ReaderItem(id="reader/1", brief="Angle A", status_text="dispatched", done=False),)
        )
    )
    before = len(buffer.getvalue())
    renderer.emit(
        ReadersUpdated(
            (ReaderItem(id="reader/1", brief="Angle A", status_text="done . 8s", done=True),)
        )
    )
    frame = _strip_ansi(buffer.getvalue()[before:])
    renderer.close()

    assert "reader/1" not in frame


def test_rich_renderer_stage_line_shows_waiting_on_n_readers_while_live():
    renderer, buffer = _rich_renderer()

    renderer.emit(StageStarted("researching"))
    before = len(buffer.getvalue())
    renderer.emit(
        ReadersUpdated(
            tuple(
                ReaderItem(
                    id=f"reader/{i}", brief=f"Angle {i}", status_text="dispatched", done=False
                )
                for i in (1, 2, 3)
            )
        )
    )
    frame = _strip_ansi(buffer.getvalue()[before:])
    renderer.close()

    # The count is READ from the live readers, not a value a stub could hardcode as "2".
    assert "waiting on 3 readers" in frame


def test_rich_renderer_stage_line_omits_waiting_on_readers_when_none_are_live():
    renderer, buffer = _rich_renderer()

    renderer.emit(StageStarted("researching"))
    renderer.emit(
        ReadersUpdated(
            (ReaderItem(id="reader/1", brief="Angle A", status_text="dispatched", done=False),)
        )
    )
    before = len(buffer.getvalue())
    renderer.emit(
        ReadersUpdated(
            (ReaderItem(id="reader/1", brief="Angle A", status_text="done . 8s", done=True),)
        )
    )
    frame = _strip_ansi(buffer.getvalue()[before:])
    renderer.close()

    assert "waiting on" not in frame


def test_rich_renderer_stage_completed_clears_the_tool_log_and_the_reader_strip():
    renderer, buffer = _rich_renderer()

    renderer.emit(StageStarted("researching"))
    renderer.emit(
        ToolCall(
            call_id="c1",
            tool="search_web",
            arg_summary="a query",
            result_summary="11 results",
            elapsed_seconds=1.0,
        )
    )
    renderer.emit(
        ReadersUpdated(
            (ReaderItem(id="reader/1", brief="Angle A", status_text="dispatched", done=False),)
        )
    )
    before = len(buffer.getvalue())
    renderer.emit(StageCompleted("researching", 2.0))
    frame = _strip_ansi(buffer.getvalue()[before:])
    renderer.close()

    assert "search_web" not in frame
    assert "reader/1" not in frame


def test_plain_renderer_tool_call_prints_one_line_on_completion_only(capsys):
    renderer = PlainRenderer()

    renderer.emit(ToolCall(call_id="c1", tool="search_web", arg_summary="a query"))
    _, start_lines = drain_stdout(capsys)
    assert start_lines == []

    renderer.emit(
        ToolCall(
            call_id="c1",
            tool="search_web",
            arg_summary="a query",
            result_summary="11 results",
            elapsed_seconds=2.3,
        )
    )
    _, lines = drain_stdout(capsys)
    assert len(lines) == 1
    assert "search_web" in lines[0]
    assert "a query" in lines[0]
    assert "11 results" in lines[0]


def test_plain_renderer_readers_updated_produces_no_output(capsys):
    renderer = PlainRenderer()

    renderer.emit(
        ReadersUpdated(
            (ReaderItem(id="reader/1", brief="Angle A", status_text="dispatched", done=False),)
        )
    )

    out, lines = drain_stdout(capsys)
    assert lines == []
    assert out == ""


def test_rich_renderer_ignores_an_event_emitted_after_close():
    """A late event must not re-enter the alternate screen.

    Newly reachable in Phase 6: the activity sink pushes from inside middleware execution, so
    a dispatch unwinding under cancellation can emit after `close()` released the screen. The
    `ToolCall` branch starts the Live region whenever `_live is None`, so without the guard
    this would hide the cursor with nothing left to stop it.
    """
    renderer, buffer = _rich_renderer()

    renderer.emit(StageStarted("researching"))
    renderer.close()
    before = len(buffer.getvalue())

    renderer.emit(ToolCall(call_id="c1", tool="search_web", arg_summary="a query"))

    assert buffer.getvalue()[before:] == ""
    assert renderer._live is None


# --- PR #25 review fixes -----------------------------------------------------------------


def test_the_tool_log_never_evicts_a_still_running_call():
    """PR #25 review, Minor: eviction by insertion order dropped live rows.

    A `task(reader)` row stays `running...` for its whole nested subgraph, so with several
    researchers in flight more than `_TOOL_LOG_TAIL` calls really are open at once. Evicting
    the long-running one meant its completion emit re-inserted it as a NEW key -- at the
    BOTTOM of the log, out of order, which is exactly what keying by `call_id` exists to
    prevent.
    """
    renderer, buffer = _rich_renderer()
    renderer.emit(StageStarted("researching"))

    renderer.emit(ToolCall(call_id="reader-1", tool="task", arg_summary="reader -- Angle A"))
    # Enough finished calls to overflow the tail several times over.
    for index in range(renderer._TOOL_LOG_TAIL * 2):
        renderer.emit(
            ToolCall(
                call_id=f"c{index}",
                tool="search_web",
                arg_summary=f"query {index}",
                result_summary="3 results",
                elapsed_seconds=0.5,
            )
        )

    assert "reader-1" in renderer._tool_calls, "the live task row was evicted"
    # Still the FIRST row, not re-inserted at the tail.
    assert list(renderer._tool_calls)[0] == "reader-1"
    # The trim still bounds the dict rather than letting it grow with the run.
    assert len(renderer._tool_calls) == renderer._TOOL_LOG_TAIL
    renderer.close()
    assert "running..." in _strip_ansi(buffer.getvalue())


def test_the_tool_log_trim_still_bounds_the_dict_when_every_call_has_finished():
    renderer, _ = _rich_renderer()
    renderer.emit(StageStarted("researching"))

    for index in range(renderer._TOOL_LOG_TAIL * 3):
        renderer.emit(
            ToolCall(
                call_id=f"c{index}",
                tool="search_web",
                arg_summary=f"query {index}",
                result_summary="3 results",
                elapsed_seconds=0.5,
            )
        )
    renderer.close()

    assert len(renderer._tool_calls) == renderer._TOOL_LOG_TAIL


def test_the_welcome_screen_forces_a_repaint_on_every_update():
    """PR #25 review, Minor: `Live.update` defaults to refresh=False.

    Without the flag a keystroke waited for the 4 Hz auto-tick, so typing lagged by up to
    250ms -- and this was the one live-region mutation in the module not forcing a repaint.
    """
    screen = harness.display.WelcomeScreen(console=Console(file=StringIO(), force_terminal=True))
    calls: list[bool] = []

    class _RecordingLive:
        def update(self, renderable: Any, refresh: bool = False) -> None:
            calls.append(refresh)

    screen._live = _RecordingLive()
    screen.update(_welcome_view(question="a", cursor_col=1))

    assert calls == [True]


def test_the_ask_overlay_marks_the_draft_line_with_an_answer_prompt():
    """PR #25 review, Nit: the mockup's `answer >` prompt marks which line is the input."""
    renderer, buffer = _rich_renderer()

    renderer.emit(StageStarted("researching"))
    renderer.emit(Question("Which region should I focus on?"))
    renderer.emit(harness.display.AnswerDraft("euro", 0, 4))
    frame = _strip_ansi(buffer.getvalue())
    renderer.close()

    assert "answer >" in frame
    draft_lines = [line for line in frame.splitlines() if "euro" in line]
    assert draft_lines, frame
    assert all("answer >" in line for line in draft_lines)
