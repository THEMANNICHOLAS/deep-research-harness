"""Typed display events and renderers — the seam between the run loop
(`harness/__main__.py`, the sole emitter) and the terminal (D2).
"""

import sys
import time
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from typing import Literal, Protocol

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.spinner import Spinner
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
class Activity:
    text: str


@dataclass(frozen=True)
class Question:
    text: str


@dataclass(frozen=True)
class RunFinished:
    stage_timings: tuple[tuple[Stage, float], ...]
    usable_sources: int
    unusable_sources: int
    cut_short: str | None
    verification_failures: int


@dataclass(frozen=True)
class TodoItem:
    content: str
    status: str


@dataclass(frozen=True)
class TodosUpdated:
    """The agent's full, ordered todo list — a replacement, not a delta (Contracts)."""

    todos: tuple[TodoItem, ...]


DisplayEvent = StageStarted | StageCompleted | Activity | Question | RunFinished | TodosUpdated


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
        elif isinstance(event, RunFinished):
            for line in _summary_lines(event):
                print(line, file=stream)
        elif isinstance(event, TodosUpdated):
            for item in event.todos:
                print(f"  [{item.status}] {item.content}", file=stream)
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

    def __init__(self, console: Console | None = None, *, auto_refresh: bool = True) -> None:
        self._console = console or Console()
        self._auto_refresh = auto_refresh
        self._live: Live | None = None
        self._stage: Stage | None = None
        self._activities: list[str] = []
        self._timeline: list[Text] = []
        self._todos: tuple[TodoItem, ...] = ()
        self._pending_question: str | None = None
        self._closed = False

    def _build_checklist(self) -> Group:
        heading = Text("Tasks", style="bold blue")
        if not self._todos:
            return Group(heading, Text("  (none yet)", style="dim"))
        lines: list[Text] = []
        for item in self._todos:
            if item.status == "completed":
                lines.append(Text(f"  [x] {item.content}", style="green"))
            elif item.status == "in_progress":
                lines.append(Text(f"  > {item.content}", style="bold bright_blue"))
            else:  # pending
                lines.append(Text(f"  [ ] {item.content}", style="#207d99"))
        return Group(heading, *lines)

    def _build_activity_group(self) -> Group:
        header = Spinner("dots", text=f"[bold]{self._stage or self._PRE_STAGE_LABEL}[/bold]")
        activity_lines = [Text(f"  {text}", style="dim") for text in self._activities]
        return Group(*self._timeline, header, *activity_lines)

    def _build_renderable(self) -> Group:
        return Group(
            self._build_checklist(),
            Rule(style="grey50"),
            Panel(self._build_activity_group(), border_style="blue"),
            Text(self._FOOTER_HINT, style="dim"),
        )

    def _start_live(self) -> None:
        self._live = Live(
            self._build_renderable(),
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
                self._live.update(self._build_renderable(), refresh=True)
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
                self._live.update(self._build_renderable(), refresh=True)
        elif isinstance(event, Question):
            # Held, not printed: while the Live owns the alternate screen a print would land
            # there and be discarded when `suspend()` exits it — the question must appear on
            # the NORMAL screen, so `_suspend()` prints it after stopping the Live.
            self._pending_question = event.text
        elif isinstance(event, TodosUpdated):
            self._todos = event.todos
            if self._live is None:
                self._start_live()
            else:
                self._live.update(self._build_renderable(), refresh=True)
        elif isinstance(event, RunFinished):
            # Leave the alternate screen FIRST: the summary belongs on the normal terminal
            # (R5), and a later `close()` must then be a safe no-op (idempotent).
            if self._live is not None:
                self._live.stop()
                self._live = None
            lines = _summary_lines(event)
            self._console.print(lines[0], style="bold")
            for line in lines[1:]:
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
                self._live.update(self._build_renderable(), refresh=True)

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
