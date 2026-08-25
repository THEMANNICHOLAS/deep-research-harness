"""Typed display events and renderers — the seam between the run loop
(`harness/__main__.py`, the sole emitter) and the terminal (D2).
"""

import sys
import time
from collections import deque
from collections.abc import Callable, Generator, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from harness.guard import strip_invisibles

Stage = Literal["clarifying", "researching", "verifying", "writing"]


@dataclass(frozen=True)
class StageStarted:
    stage: Stage


@dataclass(frozen=True)
class StageCompleted:
    stage: Stage
    elapsed_seconds: float


@dataclass(frozen=True)
class RoundsUpdated:
    """Rounds advance WITHIN a stage, so this cannot ride on `StageStarted` — a separate
    event carries the run-level round budget to the running pane's stage line."""

    rounds_used: int
    max_rounds: int


@dataclass(frozen=True)
class SourcesUpdated:
    """The count of registered sources (`[Sn]` minted) -- a replacement, not a delta (R5).

    Separate from `RunFinished`'s end-of-run totals: this is the LIVE counter, polled from
    `__main__` while the run is still going.
    """

    count: int


@dataclass(frozen=True)
class Activity:
    text: str


@dataclass(frozen=True)
class Question:
    text: str


@dataclass(frozen=True)
class AnswerDraft:
    """One repaint of the `ask_user` overlay's answer field (Phase 5), fired after every
    buffer-mutating key. `cursor_row`/`cursor_col` are the same per-row coordinates `LineBuffer`
    tracks -- `cursor_col` is a column WITHIN `cursor_row`, never an offset into `text`."""

    text: str
    cursor_row: int
    cursor_col: int


@dataclass(frozen=True)
class QuestionAnswered:
    """Retracts the `ask_user` overlay and resumes the displayed (paused) stage clock."""


@dataclass(frozen=True)
class Alert:
    """A degraded-coverage warning (a `RunLog` incident).

    Bounded to ONE line by `_bound_alert` before it is rendered (R3), and EPHEMERAL in the
    live TUI: `RichRenderer` keeps only the most recent `_ALERT_WINDOW` of them plus a
    running total (R4), because the grow-forever list this replaced filled the terminal on a
    run with many incidents. Nothing is lost — the full list still reaches the `RunFinished`
    summary count and the report's `## Gaps and disclosures`, verbatim and unbounded.
    """

    text: str


@dataclass(frozen=True)
class RunFinished:
    stage_timings: tuple[tuple[Stage, float], ...]
    usable_sources: int
    unusable_sources: int
    cut_short: str | None
    verification_failures: int
    incidents: int = 0
    report_path: Path | None = None


@dataclass(frozen=True)
class TodoItem:
    content: str
    status: str
    meta: str | None = None


@dataclass(frozen=True)
class TodosUpdated:
    """The agent's full, ordered todo list — a replacement, not a delta (Contracts)."""

    todos: tuple[TodoItem, ...]


@dataclass(frozen=True)
class ToolCall:
    """One row of the structured tool-call log (R2), emitted twice per call -- once on
    start (`result_summary is None`) and once on completion, keyed by `call_id` so the
    renderer replaces the running row in place rather than appending a duplicate."""

    call_id: str
    tool: str
    arg_summary: str
    result_summary: str | None = None
    elapsed_seconds: float | None = None
    retry: bool = False


@dataclass(frozen=True)
class ReaderItem:
    id: str
    brief: str
    status_text: str
    done: bool


@dataclass(frozen=True)
class ReadersUpdated:
    """Every reader dispatched so far this stage -- a replacement, not a delta (same
    contract as `TodosUpdated`). The strip renders only while at least one is live."""

    readers: tuple[ReaderItem, ...]


DisplayEvent = (
    StageStarted
    | StageCompleted
    | Activity
    | Question
    | AnswerDraft
    | QuestionAnswered
    | Alert
    | RunFinished
    | TodosUpdated
    | RoundsUpdated
    | ToolCall
    | ReadersUpdated
    | SourcesUpdated
)


# R4's rolling window: the live region shows only the most recent alerts, with a running
# total standing in for everything evicted. 3 is the developer's confirmed default.
_ALERT_WINDOW = 3
# R3's char bound. Generous enough that an ordinary incident line survives intact, small
# enough that crawl4ai's multi-hundred-character error dumps cannot own the screen. The
# first-line cut in `_bound_alert` is what actually stops the 25-line floods; this caps the
# pathological single-line case.
_ALERT_MAX_CHARS = 120


def _bound_alert(text: str) -> str:
    """Reduce any alert detail to ONE bounded line (R3/D3).

    Bounds at the display layer, not at `run_log.record`'s 13 call sites: the report's
    `## Gaps and disclosures` must keep the full verbatim detail, and bounding at the source
    would thin it there too. Any future emitter gets this for free.

    First line, then char cap, then `strip_invisibles` — the order matters. crawl4ai's error
    text embeds call logs and code context over dozens of lines, so the newline cut is what
    actually prevents a flood; the cap only handles a pathological single line. Stripping
    last means a control character can never survive as the last thing written.

    NOT a duplicate of `activity.brief_summary` or `agent._summarize_tool_result`, despite the
    shared first-line-plus-cap shape (PR review, Phase 3). Each is scoped to a different kind
    of text and differs where it matters: those two summarize model-authored prose, so
    `brief_summary` cuts at the first SENTENCE — which would be wrong here, since an error's
    first sentence is often a useless prefix ("Error: ") and the diagnostic follows it. This
    one also strips invisibles, which the other two have no need to (their input is not
    adversarial), and marks truncation with ASCII "..." rather than U+2026, because the plain
    renderer's stream may be cp1252. Merging them would need a parameter per difference —
    three, for two real call sites each.
    """
    stripped = text.strip()
    first_line = stripped.splitlines()[0] if stripped else ""
    if len(first_line) > _ALERT_MAX_CHARS:
        first_line = first_line[: _ALERT_MAX_CHARS - 3].rstrip() + "..."
    return strip_invisibles(first_line)


def _encodable(text: str, stream: object) -> str:
    """Replace any character `stream` cannot encode, instead of raising (LATER-PROBLEMS.md).

    Redirected stdout on Windows is cp1252, so one non-ASCII character inside crawl4ai or
    site error text raised `UnicodeEncodeError` out of `_emit_new_alerts` and killed the run
    — discarding a report for a display detail. The characters are not ours, so no ASCII-only
    rule on our own source prevents it.

    Round-trips through the stream's own encoding rather than reconfiguring the stream:
    `PlainRenderer` re-resolves `sys.stdout` on EVERY emit (pytest's `capsys` swaps it), so
    there is no single construction-time stream to reconfigure. A stream with no `encoding`
    — `StringIO` in the tests — takes any `str`, so it passes through untouched. UTF-8
    terminals are unaffected: every character round-trips.
    """
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return text
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def _summary_lines(event: RunFinished) -> list[str]:
    """The end-of-run summary content, shared by both renderers (styling differs, not text)."""
    lines = ["summary:"]
    for stage, elapsed in event.stage_timings:
        lines.append(f"  {stage} {elapsed:.1f}s")
    sources_line = f"  sources: {event.usable_sources} usable"
    if event.unusable_sources > 0:
        sources_line += f", {event.unusable_sources} unusable"
    lines.append(sources_line)
    if event.cut_short is not None:
        lines.append(f"  cut short: {event.cut_short.replace('_', ' ')}")
    if event.verification_failures > 0:
        lines.append(f"  verification failures: {event.verification_failures}")
    if event.incidents > 0:
        lines.append(f"  tool failures: {event.incidents}")
    if event.report_path is not None:
        lines.append("report written")
        lines.append(str(event.report_path))
    return lines


class Renderer(Protocol):
    """The display protocol: turn events into terminal output."""

    def emit(self, event: DisplayEvent) -> None: ...

    def suspend(self) -> AbstractContextManager[None]: ...

    def close(self) -> None: ...


class PlainRenderer:
    """Sequential text to stdout (R5) — the non-TTY, always-works renderer."""

    def emit(self, event: DisplayEvent) -> None:
        # Resolved at emit time, not captured in `__init__`: pytest's capsys replaces
        # `sys.stdout` per-test, and a captured reference would miss that swap.
        stream = sys.stdout

        def out(text: str) -> None:
            """Every write goes through `_encodable` — this is THE plain-output boundary.

            Not just the `Alert` branch (PR review, Phase 3): `ToolCall.result_summary` is
            derived from fetched page content and `Activity.text` from model-authored prose,
            so both carry arbitrary web Unicode. On redirected Windows stdout (cp1252) either
            one raised `UnicodeEncodeError` and killed the run. Wrapping the boundary rather
            than the branches means a future branch cannot reintroduce the crash.
            """
            print(_encodable(text, stream), file=stream)

        if isinstance(event, StageStarted):
            out(f"{event.stage}...")
        elif isinstance(event, StageCompleted):
            out(f"{event.stage} done ({event.elapsed_seconds:.1f}s)")
        elif isinstance(event, Question):
            out(event.text)
        elif isinstance(event, Alert):
            out(f"warning: {_bound_alert(event.text)}")
        elif isinstance(event, RunFinished):
            for line in _summary_lines(event):
                out(line)
        elif isinstance(event, TodosUpdated):
            for item in event.todos:
                out(f"  [{item.status}] {item.content}")
        elif isinstance(event, RoundsUpdated):
            # Dropped: pure live-frame decoration (D2), and a line per model turn would spam
            # the non-TTY CI logs; every other event carries a real, one-time textual meaning
            # worth printing.
            pass
        elif isinstance(event, AnswerDraft | QuestionAnswered):
            # Dropped, same reasoning as `RoundsUpdated` above: both are pure live-frame
            # decoration of the `ask_user` overlay, and one line per keystroke would flood a
            # non-TTY log. `Question` above already prints the question text itself.
            pass
        elif isinstance(event, ToolCall):
            # Only the completion emit prints: the start emit's `running...` row is live-frame
            # decoration (D-B), and the completed line is the durable fact worth a CI log line.
            if event.result_summary is not None:
                retry_suffix = " (retry)" if event.retry else ""
                out(f"  {event.tool}: {event.arg_summary} -- {event.result_summary}{retry_suffix}")
        elif isinstance(event, ReadersUpdated):
            # Dropped, same policy as `RoundsUpdated`/`AnswerDraft`/`QuestionAnswered` above:
            # the reader strip is pure live-frame decoration, presence-only, and a line per
            # status tick would flood a non-TTY log.
            pass
        elif isinstance(event, SourcesUpdated):
            out(f"sources: {event.count}")
        else:  # Activity
            out(f"  {event.text}")

    def suspend(self) -> AbstractContextManager[None]:
        return nullcontext()

    def close(self) -> None:
        pass


class _PausableClock:
    """A monotonic clock whose paused intervals do not count toward elapsed time.

    Shared by `StageTracker` (the recorded stage timings) and `RichRenderer` (the displayed
    `MM:SS`) - both derive elapsed time from a clock callable and both must pause while the
    `ask_user` overlay is open. This is deliberately NOT the wall clock (`asyncio.timeout` in
    `__main__`, risk #2): that one must keep running unconditionally, and nothing here touches
    it. `pause()` while already paused and `resume()` while not paused are both no-ops, so a
    half-paired open/retract sequence cannot corrupt the running total.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._paused_at: float | None = None
        self._paused_total: float = 0.0

    def now(self) -> float:
        if self._paused_at is not None:
            return self._paused_at - self._paused_total
        return self._clock() - self._paused_total

    def pause(self) -> None:
        if self._paused_at is None:
            self._paused_at = self._clock()

    def resume(self) -> None:
        if self._paused_at is not None:
            self._paused_total += self._clock() - self._paused_at
            self._paused_at = None


class StageTracker:
    """Owns stage state and timings (D2: display state lives in the display layer)."""

    def __init__(self, renderer: Renderer, clock: Callable[[], float] = time.monotonic) -> None:
        self._renderer = renderer
        self._elapsed = _PausableClock(clock)
        self._current: Stage | None = None
        self._started_at: float = 0.0
        self._timings: list[tuple[Stage, float]] = []

    def advance(self, stage: Stage) -> None:
        """Move to `stage`, completing whatever stage was current. A no-op if already there.

        One clock read per transition: the same instant serves as both the elapsed endpoint
        of the stage being completed and the start of the new one.
        """
        if stage == self._current:
            return
        now = self._elapsed.now()
        if self._current is not None:
            elapsed = now - self._started_at
            self._timings.append((self._current, elapsed))
            self._renderer.emit(StageCompleted(self._current, elapsed))
        self._renderer.emit(StageStarted(stage))
        self._current = stage
        self._started_at = now

    def finish(self) -> None:
        """Complete the current stage, if any. Safe to call with none current, or twice."""
        if self._current is None:
            return
        elapsed = self._elapsed.now() - self._started_at
        self._timings.append((self._current, elapsed))
        self._renderer.emit(StageCompleted(self._current, elapsed))
        self._current = None

    def timings(self) -> tuple[tuple[Stage, float], ...]:
        """Completed `(stage, elapsed_seconds)` pairs, in the order they finished."""
        return tuple(self._timings)

    def pause(self) -> None:
        """Freeze the current stage's elapsed time -- the `ask_user` overlay is open."""
        self._elapsed.pause()

    def resume(self) -> None:
        """Resume the current stage's elapsed time -- the overlay has retracted."""
        self._elapsed.resume()


def _format_elapsed(seconds: float) -> str:
    """`MM:SS`, minutes uncapped past 59 (e.g. `61:01`) — a deliberate choice for runs past
    an hour rather than an accidental one, since nothing here rolls over into an hours field."""
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}:{secs:02d}"


def _styled_text(value: str, style: str) -> Text:
    """`Text(value)` with `style` applied as a SPAN over just `value`'s own length, rather
    than `Text(value, style=style)`'s base style.

    The difference matters inside a `Table.grid` column: a base style also colors any
    right-padding Rich adds later to equalize that column's width across rows (`pad_right`
    appends plain characters with no span of their own, so they fall back to the Text's
    base style) — bleeding a retry row's `_WARN` into the padding *between* columns, one
    character past the value a caller (or a test pinning the whole-value ANSI span) expects
    to end cleanly. A span scoped to `value`'s own length keeps the padding unstyled instead.
    """
    text = Text(value)
    text.stylize(style)
    return text


class RichRenderer:
    """Full-screen TUI (D1/R5): a pinned checklist over a gray rule over the activity feed
    (R1), collapsing stage completions to a timeline line (R2), with a one-line exit-hint
    footer.

    One `Live` region for the whole run, on the alternate screen buffer, low refresh rate,
    every print routed through the same `Console` — the risk #2 mitigations from the parent
    plan.
    """

    _ACTIVITY_TAIL = 8

    # Same intent as `_ACTIVITY_TAIL` (bound the tail so a long run's log cannot grow the
    # frame unboundedly), but a separate constant: the tool log and the free-text activity
    # feed are now two distinct regions (Phase 6), not one.
    _TOOL_LOG_TAIL = 8

    # The header shown while activity is arriving but no stage has started yet — the agent's
    # first model turn and its initial todo plan, which precede the first `search_web` call.
    _PRE_STAGE_LABEL = "starting"

    _FOOTER_HINT = "Ctrl+C to exit"

    def __init__(
        self,
        console: Console | None = None,
        *,
        auto_refresh: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._console = console or Console()
        self._auto_refresh = auto_refresh
        self._live: Live | None = None
        self._stage: Stage | None = None
        self._activities: list[str] = []
        self._timeline: list[Text] = []
        # R4: a bounded window, not a list -- `deque(maxlen=...)` evicts the oldest for us.
        self._alerts: deque[Text] = deque(maxlen=_ALERT_WINDOW)
        # Counts every alert this run, including the ones the window has evicted, so the
        # total line stays truthful. `len(self._alerts)` caps at the window and cannot.
        self._alert_count = 0
        self._todos: tuple[TodoItem, ...] = ()
        # Insertion-ordered: a replaced key (the completion emit) keeps its original
        # position, which is what holds the log stable rather than jumping the row to the
        # bottom on completion.
        self._tool_calls: dict[str, ToolCall] = {}
        self._readers: tuple[ReaderItem, ...] = ()
        self._overlay_question: str | None = None
        self._answer_draft: AnswerDraft | None = None
        self._closed = False
        # ONE spinner for the renderer's lifetime, never rebuilt per frame: `Spinner.render`
        # derives the animation frame from elapsed time since ITS OWN first render, so a fresh
        # instance per `_build_renderable` call rendered frame 0 forever and the glyph never
        # rotated (PR #25 review). `_build_stage_header` only updates its text.
        self._spinner = Spinner("dots", style=_FG_2)
        self._elapsed = _PausableClock(clock)
        # RUN elapsed, not stage elapsed: this sits beside a run-level round budget, and
        # per-stage timings are already shown in the completed-stage timeline (Step 3).
        self._started_at = self._elapsed.now()
        self._rounds: tuple[int, int] | None = None
        # Starts at 0, not `None` (R5): the counter is in the frame from the first render,
        # not only after the first source lands.
        self._source_count: int = 0

    def _build_checklist(self) -> Group:
        heading = Text("Tasks", style=f"bold {_ACCENT_2}")
        if not self._todos:
            return Group(heading, Text("  (none yet)", style="dim"))
        lines: list[Text] = []
        for item in self._todos:
            if item.status == "completed":
                row = Text(f"  [x] {item.content}", style=_OK)
            elif item.status == "in_progress":
                row = Text(f"  > {item.content}", style=_ACCENT_2)
            else:  # pending
                row = Text(f"  [ ] {item.content}", style=_PENDING)
            if item.meta:
                row.append(f"  {item.meta}", style=_MUTED)
            lines.append(row)
        return Group(heading, *lines)

    def _build_stage_header(self) -> RenderableType:
        if self._stage is None:
            self._spinner.update(text=f"[bold]{self._PRE_STAGE_LABEL}[/bold]")
            return self._spinner
        self._spinner.update(text=f"[bold]{self._stage}[/bold]")
        elapsed = _format_elapsed(self._elapsed.now() - self._started_at)
        if self._rounds is not None:
            rounds_used, max_rounds = self._rounds
            elapsed = f"{elapsed} · round {rounds_used}/{max_rounds}"
        live_readers = sum(1 for reader in self._readers if not reader.done)
        if live_readers:
            noun = "reader" if live_readers == 1 else "readers"
            elapsed = f"{elapsed} · waiting on {live_readers} {noun}"
        grid = Table.grid(expand=True)
        grid.add_column()
        grid.add_column(justify="right")
        grid.add_row(self._spinner, Text(elapsed, style=_MUTED))
        return grid

    def _build_tool_log(self) -> Table | None:
        """The structured tool-call log (R2): tool / arg summary / result-or-running, one
        grid row per call, truncating rather than wrapping an overlong arg summary."""
        if not self._tool_calls:
            return None
        grid = Table.grid(expand=True, padding=(0, 1, 0, 0))
        grid.add_column(no_wrap=True)
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        grid.add_column(justify="right", no_wrap=True)
        calls = list(self._tool_calls.values())[-self._TOOL_LOG_TAIL :]
        for call in calls:
            tool_style = _WARN if call.retry else _FG
            arg_style = _WARN if call.retry else _DIM
            if call.result_summary is None:
                result_text = "running..."
            elif call.elapsed_seconds is not None:
                result_text = f"{call.result_summary} · {call.elapsed_seconds:.1f}s"
            else:
                result_text = call.result_summary
            grid.add_row(
                _styled_text(call.tool, tool_style),
                _styled_text(call.arg_summary, arg_style),
                _styled_text(result_text, _MUTED),
            )
        return grid

    def _build_reader_strip(self) -> Table | None:
        """The reader strip (D-D): one row per reader dispatched this stage, present ONLY
        while at least one is LIVE (fix-pass item 2 -- an all-done set renders nothing, even
        though the done rows themselves still render alongside any still-live ones)."""
        if not any(not reader.done for reader in self._readers):
            return None
        grid = Table.grid(expand=True, padding=(0, 1, 0, 0))
        grid.add_column(no_wrap=True)
        grid.add_column(no_wrap=True, min_width=9)
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        grid.add_column(justify="right", no_wrap=True)
        last_index = len(self._readers) - 1
        for index, reader in enumerate(self._readers):
            glyph = "└" if index == last_index else "├"
            id_style = _OK if reader.done else _ACCENT_2
            status_style = _MUTED if reader.done else _FG_2
            grid.add_row(
                _styled_text(glyph, _RULE),
                _styled_text(reader.id, id_style),
                _styled_text(reader.brief, _DIM),
                _styled_text(reader.status_text, status_style),
            )
        return grid

    def _build_ask_overlay(self) -> Panel:
        assert self._overlay_question is not None
        draft = self._answer_draft or AnswerDraft("", 0, 0)
        body = Group(
            # `Text(...)`, not the raw string: the question is model-authored, and `Panel`
            # renders console markup -- a bracketed path (`[/var/log]`) would raise
            # `MarkupError` and end the run instead of asking, and a `[a]`-style option label
            # would be parsed as an unknown style and silently dropped from the question the
            # developer is answering.
            Text(self._overlay_question),
            *_build_answer_rows(draft),
            Text("Enter to submit", style=_MUTED),
            # "clock", not the mockup's "stage clock": what freezes is the RUN elapsed time
            # shown on the stage line, not a per-stage timer. Worth being exact, because the
            # WALL clock keeps counting through this pause -- a run can be cut short at a wall
            # time this display never reached.
            Text("clock paused while the agent waits", style=_MUTED),
        )
        return Panel(body, border_style=_CYAN, title="ask_user")

    def _build_activity_group(self) -> Group:
        header = self._build_stage_header()
        strip = self._build_reader_strip()
        # The strip is pinned live state, like the timeline and alerts -- not part of the
        # log region, so it stays visible even when the overlay below replaces the log.
        strip_part: tuple[RenderableType, ...] = (strip,) if strip is not None else ()
        # R4: the rolling window plus a running total, shown whenever any alert has fired --
        # not only once eviction starts (D4's example line reads that way, and one steady
        # line is not the flood R3/R4 exist to prevent).
        status_part: list[Text] = list(self._alerts)
        if self._alert_count:
            status_part.append(
                Text(
                    f"warnings: {self._alert_count} this run - full list in report",
                    style="yellow",
                )
            )
        # R5: the live source counter, always present -- placed AFTER the warnings total so
        # the alert window stays adjacent to the timeline above it.
        status_part.append(Text(f"sources: {self._source_count}", style=_MUTED))
        if self._overlay_question is not None:
            # The overlay REPLACES the activity lines only -- the checklist (outside this
            # panel), the timeline, the stage header, and the reader strip all stay visible (R4).
            return Group(
                *self._timeline, *status_part, header, *strip_part, self._build_ask_overlay()
            )
        activity_lines = [Text(f"  {text}", style="dim") for text in self._activities]
        log = self._build_tool_log()
        log_or_activity: tuple[RenderableType, ...] = (
            *activity_lines,
            *((log,) if log is not None else ()),
        )
        return Group(*self._timeline, *status_part, header, *strip_part, *log_or_activity)

    def _build_renderable(self) -> Group:
        return Group(
            self._build_checklist(),
            Rule(style=_RULE),
            Panel(self._build_activity_group(), border_style=_FG_2),
            Text(self._FOOTER_HINT, style="dim"),
        )

    def _start_live(self) -> None:
        # `get_renderable=` (a callable), NOT a pre-built renderable: `Live` redraws whatever
        # it was handed on each auto-refresh, so a static `Group` would freeze the stage
        # line's elapsed clock between events — it would only advance when an event happened
        # to arrive (one `RoundsUpdated` per model turn, tens of seconds apart). Passing the
        # builder makes every refresh recompute it, which is what "computed at render time"
        # requires. Callers therefore use `_live.refresh()`, never `_live.update(...)`:
        # `update()` would replace this callable with a static frame and reintroduce the bug.
        self._live = Live(
            get_renderable=self._build_renderable,
            console=self._console,
            refresh_per_second=4,
            screen=True,
            auto_refresh=self._auto_refresh,
        )
        # refresh=True paints the first frame immediately — without it the header
        # (and any pre-stage activity buffer) waits for the next update to render.
        self._live.start(refresh=True)

    def emit(self, event: DisplayEvent) -> None:
        if self._closed:
            # A late event must not re-enter the alternate screen. Newly reachable in Phase 6:
            # the activity sink PUSHES from inside middleware execution, so a dispatch
            # unwinding under cancellation can emit after `close()` has already released the
            # screen and printed the summary on the normal terminal -- and the branches below
            # call `_start_live()` whenever `_live is None`, which would hide the cursor with
            # nothing left to stop it. `_suspend` guards on the same flag.
            return
        if isinstance(event, StageStarted):
            self._stage = event.stage
            # Deliberately NOT clearing `_activities`: events emitted before the first stage
            # (the agent's initial todo plan) buffer here and render with the first frame.
            # `StageCompleted` clears, and the tracker always pairs Completed -> Started.
            if self._live is None:
                self._start_live()
            else:
                self._live.refresh()
        elif isinstance(event, StageCompleted):
            self._stage = None
            self._activities = []
            # The log and strip are per-stage: a stale row/strip after this stage ends would
            # read as a tool still running or a reader still in flight from the PREVIOUS stage.
            self._tool_calls = {}
            self._readers = ()
            # Collapsed timeline lines live INSIDE the frame: under `screen=True` a
            # `console.print` while the Live runs paints onto the alternate screen at the
            # home position and is overwritten by the next refresh, then discarded on exit.
            self._timeline.append(
                Text.assemble(
                    ("ok", "green"),
                    f" {event.stage} ",
                    (f"({event.elapsed_seconds:.1f}s)", "dim"),
                )
            )
            if self._live is not None:
                self._live.refresh()
        elif isinstance(event, Question):
            # In-frame overlay (Phase 5), not held for `suspend()` to print: the checklist,
            # timeline, and stage line stay visible, and the stage clock pauses for exactly
            # as long as the overlay is open (risk #2's OTHER clock -- the wall clock in
            # `__main__` is untouched here).
            self._overlay_question = event.text
            self._answer_draft = AnswerDraft("", 0, 0)
            self._elapsed.pause()
            if self._live is None:
                self._start_live()
            else:
                self._live.refresh()
        elif isinstance(event, AnswerDraft):
            self._answer_draft = event
            if self._live is not None:
                self._live.refresh()
        elif isinstance(event, QuestionAnswered):
            self._overlay_question = None
            self._answer_draft = None
            self._elapsed.resume()
            if self._live is not None:
                self._live.refresh()
        elif isinstance(event, Alert):
            # Appended to a bounded rolling WINDOW (R4) rendered inside the frame, not
            # `console.print`ed
            # and not part of the scrolling `_activities` tail: under `screen=True` a print
            # while the Live runs is overwritten and then discarded on exit, and a failed
            # search must not scroll away. `Text(...)` for the same markup-safety reason as
            # `Question` — the detail can carry model- or URL-derived brackets. The run's
            # full incident list still reaches the normal screen via `RunFinished` and the
            # report's `## Gaps and disclosures`.
            warning = Text(
                f"warning: {_bound_alert(event.text)}",
                style="yellow",
                # R3 literally: one ROW, whatever the terminal width. The char cap alone
                # would still wrap a long line into two rows on a narrow terminal.
                no_wrap=True,
                overflow="ellipsis",
            )
            self._alerts.append(warning)
            self._alert_count += 1
            if self._live is not None:
                self._live.refresh()
            else:
                # No Live owns the screen yet (an incident before the first stage, or a run
                # whose feed never started): print it straight to the normal terminal, where
                # it stays. Once the Live starts, `_alerts` replays it inside the frame.
                self._console.print(warning)
        elif isinstance(event, TodosUpdated):
            self._todos = event.todos
            if self._live is None:
                self._start_live()
            else:
                self._live.refresh()
        elif isinstance(event, RoundsUpdated):
            # Pure live-frame decoration (Step 3): does not start the Live region on its
            # own — it only repaints an already-running frame, since a round only advances
            # once a stage (and so the frame) exists.
            self._rounds = (event.rounds_used, event.max_rounds)
            if self._live is not None:
                self._live.refresh()
        elif isinstance(event, SourcesUpdated):
            # Same policy as `RoundsUpdated`: pure decoration of an already-running frame,
            # never starts the Live region on its own.
            self._source_count = event.count
            if self._live is not None:
                self._live.refresh()
        elif isinstance(event, ToolCall):
            self._tool_calls[event.call_id] = event
            # Trimmed to the tail on every emit, not just at render time, so the dict cannot
            # grow unboundedly across a long run.
            #
            # STILL-RUNNING calls are never evicted, however old: evicting one only to have
            # its completion emit re-insert it as a new key put the row at the BOTTOM of the
            # log, out of order, breaking the in-place replacement this dict exists for. A
            # `task(reader)` runs for its whole nested subgraph, so with 3 researchers each
            # allowed several searches and reader dispatches, more than `_TOOL_LOG_TAIL`
            # calls really are in flight at once (PR #25 review).
            if len(self._tool_calls) > self._TOOL_LOG_TAIL:
                finished = [
                    call_id
                    for call_id, call in self._tool_calls.items()
                    if call.result_summary is not None
                ]
                for stale_id in finished[: len(self._tool_calls) - self._TOOL_LOG_TAIL]:
                    del self._tool_calls[stale_id]
            # A tool call can precede the first stage, exactly as `Activity` already handles.
            if self._live is None:
                self._start_live()
            else:
                self._live.refresh()
        elif isinstance(event, ReadersUpdated):
            self._readers = event.readers
            # Pure decoration of a running frame (same as `RoundsUpdated`): does not start
            # the Live region on its own.
            if self._live is not None:
                self._live.refresh()
        elif isinstance(event, RunFinished):
            # Leave the alternate screen FIRST: the summary belongs on the normal terminal
            # (R5), and a later `close()` must then be a safe no-op (idempotent).
            if self._live is not None:
                self._live.stop()
                self._live = None
            lines = _summary_lines(event)
            path_str = str(event.report_path) if event.report_path is not None else None
            self._console.print(lines[0], style="bold")
            for line in lines[1:]:
                if line == path_str:
                    # `Text`, not a raw string: `Console.print` would run the path through
                    # markup parsing (a `[` in `reports_dir` then raises inside `emit`, after
                    # the report was already written) and through `ReprHighlighter`, whose
                    # per-token colours override `_ACCENT` — on POSIX it claims the whole path
                    # as magenta, losing the accent entirely. `soft_wrap` keeps a path longer
                    # than the terminal on one copy-pasteable line.
                    self._console.print(Text(line, style=_ACCENT), soft_wrap=True)
                else:
                    self._console.print(line, style="dim")
        else:  # Activity
            self._activities = (self._activities + [event.text])[-self._ACTIVITY_TAIL :]
            # Starts the region if no stage has begun yet: the first stage is `clarifying` or
            # `researching`, both of which trail the agent's first model turn, so waiting for
            # `StageStarted` left the terminal blank through it and buffered the todo plan
            # instead of showing it as it happened (R1).
            if self._live is None:
                self._start_live()
            else:
                self._live.refresh()

    def suspend(self) -> AbstractContextManager[None]:
        return self._suspend()

    @contextmanager
    def _suspend(self) -> Generator[None, None, None]:
        was_running = self._live is not None
        if self._live is not None:
            self._live.stop()
            self._live = None
        try:
            yield
        finally:
            if was_running and self._stage is not None and not self._closed:
                self._start_live()

    def close(self) -> None:
        self._closed = True
        if self._live is not None:
            self._live.stop()
            self._live = None


def build_renderer() -> Renderer:
    """Pick the renderer implementation: TTY -> `RichRenderer`, non-TTY -> `PlainRenderer` (R5)."""
    if sys.stdout.isatty():
        return RichRenderer()
    return PlainRenderer()


# --- Welcome screen (Phase 2, PLAN-tui-redesign) ----------------------------------------
#
# Palette taken from docs/design/deep-research-tui.html's `:root` block. Defined once here —
# later phases (running pane, ask_user overlay) reuse these same constants rather than
# re-declaring the hex values (CLAUDE.md: "a constant lives in exactly one place").
_FG = "#e8e8e8"
_DIM = "#8a8f96"
_MUTED = "#6a6f76"
_ACCENT = "#4a7fd8"
_SURFACE = "#1c1c1c"
_WORDMARK_BACK = "#7a7a7a"
_WARN = "#d9a33a"
_OK = "#3fd15a"
_CYAN = "#56b6c2"

# Running-pane palette (Phase 3, PLAN-tui-redesign): `.tasks-head`, `.panel`/stage spinner,
# the checklist/panel divider, and pending task rows respectively.
_ACCENT_2 = "#6b8cff"
_FG_2 = "#c8ccd1"
_RULE = "#4a4f55"
_PENDING = "#474747"

# Glyph rows taken VERBATIM from docs/design/deep-research-tui.html's `.wordmark` `<pre>`
# blocks — not hand-drawn letterforms.
_WORDMARK_BACK_LINE1 = "█▀▄ █▀▀ █▀▀ █▀█"
_WORDMARK_BACK_LINE2 = "█▄▀ ██▄ ██▄ █▀▀"
_WORDMARK_FRONT_LINE1 = "█▀█ █▀▀ █▀ █▀▀ ▄▀█ █▀█ █▀▀ █ █"
_WORDMARK_FRONT_LINE2 = "█▀▄ ██▄ ▄█ ██▄ █▀█ █▀▄ █▄▄ █▀█"

_EXAMPLE_QUESTION = '"How does DeepSeek V4 Pro price long-context runs?"'
_PLACEHOLDER = "Ask anything… "
# The `ask_user` overlay's input prompt (docs/design/deep-research-tui.html's `answer >`).
_ANSWER_PROMPT = "answer > "

_HINTS: tuple[tuple[str, str], ...] = (
    ("enter", "run"),
    ("/", "commands"),
    ("ctrl+j", "newline"),
    ("ctrl+c", "exit"),
)

# At most this many choice rows render in the `/model` picker — a long list (19 choices
# today) gets `…` affordances above/below instead of blowing up the frame.
_PICKER_WINDOW = 12

# The mockup's `.entry{width:min(760px,100%)}`: the input box and its hint/roles lines are
# capped to this many columns so centering is visible on a wide terminal instead of the box
# stretching edge to edge.
_ENTRY_WIDTH = 80


@dataclass(frozen=True)
class WelcomeView:
    """Everything the welcome screen renders — no config object reached into at render
    time, which keeps `build_welcome` pure and the tests trivial."""

    question: str
    cursor_col: int
    head_model: str
    roles: tuple[tuple[str, str], ...]
    budget: str
    status_left: str
    status_right: str
    # Pairs with `cursor_col` above, but carries a default so it sits in the defaulted
    # block: `cursor_col` is a PER-ROW column, so the cursor cannot be placed without
    # knowing its row once Ctrl+J has split the buffer.
    cursor_row: int = 0
    notice: str | None = None
    panel: RenderableType | None = None


def _build_wordmark() -> Group:
    gap = "  "
    line1 = Text(_WORDMARK_BACK_LINE1, style=_WORDMARK_BACK)
    line1.append(gap)
    line1.append(_WORDMARK_FRONT_LINE1, style=_FG)
    line2 = Text(_WORDMARK_BACK_LINE2, style=_WORDMARK_BACK)
    line2.append(gap)
    line2.append(_WORDMARK_FRONT_LINE2, style=_FG)
    return Group(line1, line2)


def _build_cursor_rows(
    text: str, cursor_row: int, cursor_col: int, *, style: str, cursor_style: str
) -> list[Text]:
    """One `Text` per line of `text`, with a block cursor placed on `cursor_row` at
    `cursor_col`.

    Shared by `_build_ask_rows` (the welcome box) and the `ask_user` overlay (Phase 5) -
    the per-row cursor placement is the same logic either way. `cursor_col` is a column
    WITHIN its row, never an offset into the newline-joined text - indexing the joined
    string draws the cursor over the `\n` once Ctrl+J has split the buffer.
    """
    lines = text.split("\n")
    row_index = max(0, min(cursor_row, len(lines) - 1))
    rows: list[Text] = []
    for index, line in enumerate(lines):
        row = Text()
        if index != row_index:
            row.append(line, style=style)
            rows.append(row)
            continue
        col = max(0, min(cursor_col, len(line)))
        row.append(line[:col], style=style)
        cursor_char = line[col] if col < len(line) else " "
        # Reverse video: the cursor's foreground is the box's own background, and its
        # background is the box's own foreground - swapped, not a `reverse` style
        # attribute, so the exact palette colors are what actually swap.
        row.append(cursor_char, style=cursor_style)
        row.append(line[col + 1 :], style=style)
        rows.append(row)
    return rows


def _build_answer_rows(draft: AnswerDraft) -> list[Text]:
    """The `ask_user` overlay's draft rows, behind the mockup's `answer >` prompt.

    The prompt is what marks which line of the panel is the input; without it the draft sits
    under the question prose as more text (PR #25 review). Continuation rows (Ctrl+J) are
    padded to the same width so the typed text stays in one column.
    """
    rows = _build_cursor_rows(
        draft.text,
        draft.cursor_row,
        draft.cursor_col,
        style=_FG,
        cursor_style=f"{_SURFACE} on {_FG}",
    )
    prefixed: list[Text] = []
    for index, row in enumerate(rows):
        out = Text()
        if index == 0:
            out.append(_ANSWER_PROMPT, style=_CYAN)
        else:
            out.append(" " * len(_ANSWER_PROMPT))
        out.append_text(row)
        prefixed.append(out)
    return prefixed


def _build_ask_rows(view: WelcomeView) -> list[Text]:
    """One `Text` per buffer line, or the placeholder row when the buffer is empty.

    Delegates the non-empty case to `_build_cursor_rows`; the placeholder and `_SURFACE`
    background stay here, since the `ask_user` overlay needs neither.
    """
    if not view.question:
        row = Text()
        row.append(_PLACEHOLDER, style=f"{_DIM} on {_SURFACE}")
        row.append(_EXAMPLE_QUESTION, style=f"{_MUTED} on {_SURFACE}")
        return [row]
    return _build_cursor_rows(
        view.question,
        view.cursor_row,
        view.cursor_col,
        style=f"{_FG} on {_SURFACE}",
        cursor_style=f"{_SURFACE} on {_FG}",
    )


def _build_mode_row(view: WelcomeView) -> Text:
    row = Text()
    row.append("Research", style=f"{_ACCENT} on {_SURFACE}")
    row.append(" · ", style=f"{_MUTED} on {_SURFACE}")
    row.append(view.head_model, style=f"{_FG} on {_SURFACE}")
    return row


def _left_bar(content: Text) -> Text:
    """A leading accent bar per line — Rich has no single-side `Panel` border, so this is
    the chosen substitute (over a `box.HEAVY`-style full border, which reads further from
    the mockup's thin left-only bar)."""
    line = Text("▌ ", style=f"{_ACCENT} on {_SURFACE}")
    line.append_text(content)
    return line


def _build_input_box(view: WelcomeView) -> Panel:
    rows = [_left_bar(row) for row in _build_ask_rows(view)]
    body = Group(*rows, _left_bar(_build_mode_row(view)))
    # `border_style=_SURFACE`: matches the fill so no visible frame competes with the left
    # bar, which is the only border cue the mockup actually shows.
    return Panel(body, style=f"on {_SURFACE}", border_style=_SURFACE, padding=(0, 1))


def _build_hints_line() -> Text:
    line = Text()
    for i, (key, label) in enumerate(_HINTS):
        if i:
            line.append("  ")
        line.append(key, style=_FG)
        line.append(" ")
        line.append(label, style=_MUTED)
    return line


def _build_roles_line(view: WelcomeView) -> Text:
    """`researcher`/`reader`/`verifier` (whatever `view.roles` holds) then `budget` — the
    head model is NOT repeated here; the input box's mode row already shows it (Reconciliations)."""
    line = Text()
    entries = (*view.roles, ("budget", view.budget))
    for i, (label, value) in enumerate(entries):
        if i:
            line.append(" · ", style=_MUTED)
        line.append(label, style=_DIM)
        line.append(" ")
        line.append(value, style=_MUTED)
    return line


def _build_tip_line() -> Text:
    """Advertises `/help`, not `/sources` (Reconciliations — `/sources` is dropped)."""
    line = Text()
    line.append("●", style=_WARN)
    line.append(" Tip", style=_WARN)
    line.append(" Run ", style=_MUTED)
    line.append("/help", style=_FG)
    line.append(" to see available commands", style=_MUTED)
    return line


def _build_status_bar(view: WelcomeView) -> Table:
    grid = Table.grid(expand=True)
    grid.add_column()
    grid.add_column(justify="right")
    grid.add_row(Text(view.status_left, style=_MUTED), Text(view.status_right, style=_MUTED))
    return grid


def build_welcome(view: WelcomeView) -> Layout:
    """Render the welcome screen per the mockup's `#screen-welcome`: the hero block
    (wordmark over the entry box and its hint lines, then the tip) centered on both axes
    (`.welcome-stage{align-items:center;justify-content:center}`), the status bar pinned
    to the bottom row (`justify-content:space-between`) — NOT stacked top-to-bottom, which
    crammed the whole screen against the top of the terminal (PR #25 review)."""
    entry = Table.grid(padding=0)
    entry.add_column(width=_ENTRY_WIDTH)
    entry.add_row(_build_input_box(view))
    if view.notice:
        entry.add_row(Text(view.notice, style=_WARN))
    if view.panel is not None:
        entry.add_row(view.panel)
    entry.add_row(_build_hints_line())
    entry.add_row(_build_roles_line(view))
    hero = Group(
        Align.center(_build_wordmark()),
        Text(""),
        Align.center(entry),
        Text(""),
        Align.center(_build_tip_line()),
    )
    layout = Layout()
    layout.split_column(
        Layout(Align.center(hero, vertical="middle"), name="stage"),
        Layout(_build_status_bar(view), name="statusbar", size=1),
    )
    return layout


def build_help_panel(commands: Sequence[tuple[str, str]]) -> Panel:
    """Lists `commands` (name, summary) pairs — the caller iterates its dispatch table to
    build this list, so a newly registered command appears automatically."""
    rows: list[Text] = []
    for name, summary in commands:
        row = Text()
        row.append(name, style=_FG)
        row.append(f"  {summary}", style=_MUTED)
        rows.append(row)
    body = Group(*rows, Text(""), _build_hints_line())
    return Panel(body, title="Help", border_style=_ACCENT)


def build_model_picker(choices: Sequence[str], selected: int, current: str) -> Panel:
    """One row per choice, `selected` highlighted, the row equal to `current` marked active.

    Renders a window of at most `_PICKER_WINDOW` rows centered on `selected`, with `…`
    affordances above/below when truncated — a long list (19 choices today) must not blow
    up the frame.
    """
    n = len(choices)
    window = min(_PICKER_WINDOW, n)
    if n <= _PICKER_WINDOW:
        start, end = 0, n
    else:
        half = window // 2
        start = max(0, min(selected - half, n - window))
        end = start + window
    lines: list[Text] = []
    if start > 0:
        lines.append(Text("…", style=_MUTED))
    for i in range(start, end):
        choice = choices[i]
        row = Text()
        if i == selected:
            row.append("> ", style=_ACCENT)
            row.append(choice, style=f"{_SURFACE} on {_ACCENT}")
        else:
            row.append("  ")
            row.append(choice, style=_FG)
        if choice == current:
            row.append("  (current)", style=_MUTED)
        lines.append(row)
    if end < n:
        lines.append(Text("…", style=_MUTED))
    return Panel(Group(*lines), title="Select model", border_style=_ACCENT)


class WelcomeScreen:
    """Thin `Live` owner for the welcome screen — mirrors `RichRenderer`'s ownership of its
    own `Live`: the live region owns terminal state (module docstring)."""

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()
        self._live: Live | None = None

    def __enter__(self) -> "WelcomeScreen":
        self._live = Live(console=self._console, screen=True, refresh_per_second=4)
        self._live.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def update(self, view: WelcomeView) -> None:
        if self._live is not None:
            # `refresh=True`, matching every other live-region mutation in this module:
            # `Live.update` defaults to refresh=False, so without it a keystroke waited for
            # the 4 Hz auto-tick and typing visibly lagged by up to 250ms (PR #25 review).
            self._live.update(build_welcome(view), refresh=True)
