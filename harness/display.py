"""Typed display events and renderers — the seam between the run loop
(`harness/__main__.py`, the sole emitter) and the terminal (D2).
"""

import sys
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

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
class Activity:
    text: str


@dataclass(frozen=True)
class Question:
    text: str


@dataclass(frozen=True)
class Alert:
    """A degraded-coverage warning (a `RunLog` incident): rendered as a PERSISTENT line, not
    part of the scrolling activity tail — a failed search must not vanish off the feed."""

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


DisplayEvent = (
    StageStarted
    | StageCompleted
    | Activity
    | Question
    | Alert
    | RunFinished
    | TodosUpdated
    | RoundsUpdated
)


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
        if isinstance(event, StageStarted):
            print(f"{event.stage}...", file=stream)
        elif isinstance(event, StageCompleted):
            print(f"{event.stage} done ({event.elapsed_seconds:.1f}s)", file=stream)
        elif isinstance(event, Question):
            print(event.text, file=stream)
        elif isinstance(event, Alert):
            print(f"warning: {event.text}", file=stream)
        elif isinstance(event, RunFinished):
            for line in _summary_lines(event):
                print(line, file=stream)
        elif isinstance(event, TodosUpdated):
            for item in event.todos:
                print(f"  [{item.status}] {item.content}", file=stream)
        elif isinstance(event, RoundsUpdated):
            # The only event this renderer drops. It is pure live-frame decoration (D2), and
            # a line per model turn would spam the non-TTY CI logs; every other event carries
            # a real, one-time textual meaning worth printing.
            pass
        else:  # Activity
            print(f"  {event.text}", file=stream)

    def suspend(self) -> AbstractContextManager[None]:
        return nullcontext()

    def close(self) -> None:
        pass


class StageTracker:
    """Owns stage state and timings (D2: display state lives in the display layer)."""

    def __init__(self, renderer: Renderer, clock: Callable[[], float] = time.monotonic) -> None:
        self._renderer = renderer
        self._clock = clock
        self._current: Stage | None = None
        self._started_at: float = 0.0
        self._timings: list[tuple[Stage, float]] = []

    def advance(self, stage: Stage) -> None:
        """Move to `stage`, completing whatever stage was current. A no-op if already there.

        One `clock()` read per transition: the same instant serves as both the elapsed
        endpoint of the stage being completed and the start of the new one.
        """
        if stage == self._current:
            return
        now = self._clock()
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
        elapsed = self._clock() - self._started_at
        self._timings.append((self._current, elapsed))
        self._renderer.emit(StageCompleted(self._current, elapsed))
        self._current = None

    def timings(self) -> tuple[tuple[Stage, float], ...]:
        """Completed `(stage, elapsed_seconds)` pairs, in the order they finished."""
        return tuple(self._timings)


def _format_elapsed(seconds: float) -> str:
    """`MM:SS`, minutes uncapped past 59 (e.g. `61:01`) — a deliberate choice for runs past
    an hour rather than an accidental one, since nothing here rolls over into an hours field."""
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}:{secs:02d}"


class RichRenderer:
    """Full-screen TUI (D1/R5): a pinned checklist over a gray rule over the activity feed
    (R1), collapsing stage completions to a timeline line (R2), with a one-line exit-hint
    footer.

    One `Live` region for the whole run, on the alternate screen buffer, low refresh rate,
    every print routed through the same `Console` — the risk #2 mitigations from the parent
    plan.
    """

    _ACTIVITY_TAIL = 8

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
        self._alerts: list[Text] = []
        self._todos: tuple[TodoItem, ...] = ()
        self._pending_question: str | None = None
        self._closed = False
        self._clock = clock
        # RUN elapsed, not stage elapsed: this sits beside a run-level round budget, and
        # per-stage timings are already shown in the completed-stage timeline (Step 3).
        self._started_at = clock()
        self._rounds: tuple[int, int] | None = None

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
            return Spinner("dots", text=f"[bold]{self._PRE_STAGE_LABEL}[/bold]", style=_FG_2)
        spinner = Spinner("dots", text=f"[bold]{self._stage}[/bold]", style=_FG_2)
        elapsed = _format_elapsed(self._clock() - self._started_at)
        if self._rounds is not None:
            rounds_used, max_rounds = self._rounds
            elapsed = f"{elapsed} · round {rounds_used}/{max_rounds}"
        grid = Table.grid(expand=True)
        grid.add_column()
        grid.add_column(justify="right")
        grid.add_row(spinner, Text(elapsed, style=_MUTED))
        return grid

    def _build_activity_group(self) -> Group:
        header = self._build_stage_header()
        activity_lines = [Text(f"  {text}", style="dim") for text in self._activities]
        return Group(*self._timeline, *self._alerts, header, *activity_lines)

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
            # Held, not printed: while the Live owns the alternate screen a print would land
            # there and be discarded when `suspend()` exits it — the question must appear on
            # the NORMAL screen, so `_suspend()` prints it after stopping the Live.
            self._pending_question = event.text
        elif isinstance(event, Alert):
            # Appended to a PERSISTENT list rendered inside the frame, not `console.print`ed
            # and not part of the scrolling `_activities` tail: under `screen=True` a print
            # while the Live runs is overwritten and then discarded on exit, and a failed
            # search must not scroll away. `Text(...)` for the same markup-safety reason as
            # `Question` — the detail can carry model- or URL-derived brackets. The run's
            # full incident list still reaches the normal screen via `RunFinished` and the
            # report's `## Gaps and disclosures`.
            warning = Text(f"warning: {event.text}", style="yellow")
            self._alerts.append(warning)
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
        if self._pending_question is not None:
            # `Text(...)`, not the raw string: `Panel` renders console markup, and the question
            # is model-authored. A bracketed path (`[/var/log]`) would raise `MarkupError` and
            # end the run instead of asking, and a `[a]`-style option label would be parsed as
            # an unknown style and silently dropped from the question the developer answers.
            self._console.print(Panel(Text(self._pending_question), border_style="cyan"))
            self._pending_question = None
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

_HINTS: tuple[tuple[str, str], ...] = (
    ("enter", "run"),
    ("/", "commands"),
    ("ctrl+j", "newline"),
    ("ctrl+c", "exit"),
)

# At most this many choice rows render in the `/model` picker — a long list (19 choices
# today) gets `…` affordances above/below instead of blowing up the frame.
_PICKER_WINDOW = 12


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


def _build_ask_rows(view: WelcomeView) -> list[Text]:
    """One `Text` per buffer line.

    Returns a list rather than a single `Text` so the block cursor can be placed on
    `cursor_row` at `cursor_col`, and so every line gets its own accent bar. `cursor_col`
    is a column WITHIN its row, never an offset into the newline-joined text — indexing
    the joined string draws the cursor over the `\\n` once Ctrl+J has split the buffer.
    """
    if not view.question:
        row = Text()
        row.append(_PLACEHOLDER, style=f"{_DIM} on {_SURFACE}")
        row.append(_EXAMPLE_QUESTION, style=f"{_MUTED} on {_SURFACE}")
        return [row]
    lines = view.question.split("\n")
    cursor_row = max(0, min(view.cursor_row, len(lines) - 1))
    rows: list[Text] = []
    for index, line in enumerate(lines):
        row = Text()
        if index != cursor_row:
            row.append(line, style=f"{_FG} on {_SURFACE}")
            rows.append(row)
            continue
        col = max(0, min(view.cursor_col, len(line)))
        row.append(line[:col], style=f"{_FG} on {_SURFACE}")
        cursor_char = line[col] if col < len(line) else " "
        # Reverse video: the cursor's foreground is the box's own background, and its
        # background is the box's own foreground — swapped, not a `reverse` style attribute,
        # so the exact palette colors are what actually swap.
        row.append(cursor_char, style=f"{_SURFACE} on {_FG}")
        row.append(line[col + 1 :], style=f"{_FG} on {_SURFACE}")
        rows.append(row)
    return rows


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


def build_welcome(view: WelcomeView) -> Group:
    """Render the welcome screen top-to-bottom, per the mockup's `#screen-welcome`."""
    parts: list[RenderableType] = [_build_wordmark(), _build_input_box(view)]
    if view.notice:
        parts.append(Text(view.notice, style=_WARN))
    if view.panel is not None:
        parts.append(view.panel)
    parts.append(_build_hints_line())
    parts.append(_build_roles_line(view))
    parts.append(_build_tip_line())
    parts.append(_build_status_bar(view))
    return Group(*parts)


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
            self._live.update(build_welcome(view))
