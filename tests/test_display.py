"""Behavioral tests for harness.display: events, renderers, and StageTracker."""

import pathlib
import re
import sys
from contextlib import AbstractContextManager
from io import StringIO
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage
from rich.console import Console

import harness.__main__ as main_module
from harness.config import AgentSettings
from harness.display import (
    Activity,
    Alert,
    DisplayEvent,
    PlainRenderer,
    Question,
    RichRenderer,
    RunFinished,
    StageCompleted,
    StageStarted,
    StageTracker,
    TodoItem,
    TodosUpdated,
    build_renderer,
)
from harness.sources import SourceRegistry
from tests.conftest import (
    drain_stdout,
    install_search_transport,
    patch_run,
    verify_reply,
    write_failed_capture,
    write_source_capture,
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _rich_renderer() -> tuple[RichRenderer, StringIO]:
    buffer = StringIO()
    # `legacy_windows=False` (rather than relying on auto-detection): the alternate-screen
    # codes `RichRenderer` now relies on (D1/R5) are suppressed by Rich whenever
    # `legacy_windows` is true, which it auto-detects as True on this platform even over a
    # `StringIO` file — leaving the alt-screen behavior untestable on Windows otherwise.
    # `color_system="truecolor"` (rather than "auto"): the `#207d99` pending style (R6) only
    # emits its literal `38;2;...` truecolor escape under truecolor; "auto" resolves to
    # 16-color "standard" here, downgrading the hex style to a named ANSI color instead.
    # `_environ={}`: an ambient NO_COLOR in the invoking shell would strip color styles
    # even under force_terminal, making the truecolor assertions env-dependent.
    console = Console(
        file=buffer,
        force_terminal=True,
        width=80,
        legacy_windows=False,
        color_system="truecolor",
        _environ={},
    )
    return RichRenderer(console=console, auto_refresh=False), buffer


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
    ping = AIMessage(content="pong")
    final = AIMessage(
        content="Final answer.",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    model = scripted_model([ping, final])
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
    """
    config = make_config()
    ping = AIMessage(content="pong")
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
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "search_web",
                    "args": {"query": "the answer"},
                    "id": "call_search",
                }
            ],
        ),
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
    final = AIMessage(
        content="Final answer.",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    model = scripted_model([ping, *plan_search_and_replan, final])
    patch_run(monkeypatch, config, model)
    _install_stub_search(monkeypatch)

    await main_module.main(["a question needing research"])

    out, lines = drain_stdout(capsys)
    assert '  search_web: "the answer"' in out
    assert "  [pending] Search for the answer" in lines
    assert "  [completed] Search for the answer" in lines
    assert lines.count("  [pending] Write the summary") == 2
    assert lines[-1].strip().endswith(".md")


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
    # `#207d99` as `Console(force_terminal=True)` truecolor: 0x20, 0x7d, 0x99 = 32, 125, 153.
    assert "38;2;32;125;153" in raw
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


def test_rich_renderer_suspend_exits_the_alt_screen_for_a_question_and_restores_after():
    renderer, buffer = _rich_renderer()

    # Production order (`__main__.py`): the Question is emitted while the Live still owns
    # the alternate screen, and only then does the run loop enter `suspend()` for input().
    # The renderer must hold the question and print it on the NORMAL screen inside
    # `suspend()` — a print while the Live runs would be discarded with the alt buffer.
    renderer.emit(StageStarted("researching"))
    renderer.emit(Question("Which region?"))
    with renderer.suspend():
        pass

    raw = buffer.getvalue()
    question_index = raw.index("Which region?")
    exit_index = raw.rindex("\x1b[?1049l", 0, question_index)
    reenter_index = raw.index("\x1b[?1049h", question_index)
    assert exit_index < question_index < reenter_index

    renderer.close()


# --- Question panel + suspend tests (Phase 3) ---------------------------------------------


def test_rich_renderer_question_renders_as_a_bordered_panel():
    renderer, buffer = _rich_renderer()

    renderer.emit(Question("Which region?"))
    with renderer.suspend():
        pass
    renderer.close()

    text = _strip_ansi(buffer.getvalue())
    lines = [line for line in text.splitlines() if line.strip()]
    question_index = next(i for i, line in enumerate(lines) if "Which region?" in line)
    # A panel border above and below the question text — at least one non-empty line on
    # each side that is not itself the question text (do not assert box characters).
    assert question_index > 0
    assert question_index < len(lines) - 1


def test_rich_renderer_question_is_not_parsed_as_console_markup():
    """The question is model-authored, and `Panel` renders console markup.

    `[/var/log]` raised `MarkupError` and ended the run instead of asking it; `[a]` parsed as
    an unknown style and vanished from the question the developer was answering.
    """
    renderer, buffer = _rich_renderer()

    renderer.emit(Question("Which log, [/var/log] or [a] the app's own?"))
    with renderer.suspend():
        pass
    renderer.close()

    text = _strip_ansi(buffer.getvalue())
    assert "[/var/log]" in text
    assert "[a]" in text


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
    ping = AIMessage(content="pong")
    final = AIMessage(
        content="Final answer.",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    model = scripted_model([ping, final])
    patch_run(monkeypatch, config, model)

    await main_module.main(["a question with no tool calls"])

    out, lines = drain_stdout(capsys)
    assert "summary:" in out
    assert any(line.strip().startswith("sources:") for line in lines)
    assert lines[-1].strip().endswith(".md")


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
    blocked = registry.add("https://example.test/blocked", title="Blocked")
    write_source_capture(config, registry, fetched, "Acme lists $5.10 per unit.")
    write_failed_capture(config, registry, blocked)
    monkeypatch.setattr(main_module, "SourceRegistry", lambda run_id: registry)

    ping = AIMessage(content="pong")
    final = AIMessage(
        content=f"Acme lists $5.10 per unit [{fetched}].",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    model = scripted_model([ping, final, verify_reply(True, "The page states $5.10.")])
    patch_run(monkeypatch, config, model)

    await main_module.main(["what does acme charge"])

    _, lines = drain_stdout(capsys)
    assert "  sources: 1 usable, 1 unusable" in lines


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

    monkeypatch.setattr(main_module, "write_report", _unwritable)

    ping = AIMessage(content="pong")
    final = AIMessage(
        content="Final answer.",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    model = scripted_model([ping, final])
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

    ping = AIMessage(content="pong")
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
    model = scripted_model([ping, plan_call])  # no third response — the run dies here
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
    ping = AIMessage(content="pong")
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
    model = scripted_model([ping, *([keep_going] * 20)])
    patch_run(monkeypatch, config, model)

    await main_module.main(["question that never settles"])

    out, lines = drain_stdout(capsys)
    assert "cut short: round cap" in out
    assert lines[-1].strip().endswith(".md")


@pytest.mark.parametrize("kind", ["plain", "rich"])
def test_alert_renders_as_a_persistent_warning_line(kind, capsys):
    event = Alert('search for "solar" failed: unreachable')

    lines = _render_lines(kind, event, capsys)

    assert any('warning: search for "solar" failed: unreachable' in line for line in lines)


async def test_a_dead_search_backend_is_disclosed_on_the_terminal_and_in_the_report(
    make_config, monkeypatch, scripted_model, capsys
):
    """The invariant-pinning path (best-effort + disclose): a SearchFailure must reach the
    developer through the CLI as a warning line AND through the report's gaps section — not
    only through model-facing tool output the model may never repeat."""
    config = make_config()
    ping = AIMessage(content="pong")
    search_call = AIMessage(
        content="",
        tool_calls=[{"name": "search_web", "args": {"query": "the answer"}, "id": "call_search"}],
    )
    final = AIMessage(
        content="Best-effort answer without sources.",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    model = scripted_model([ping, search_call, final])
    patch_run(monkeypatch, config, model)

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
