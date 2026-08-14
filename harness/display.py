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


DisplayEvent = StageStarted | StageCompleted | Activity | Question | RunFinished


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
    """Live-updating stage header + activity feed (R1), collapsing to a timeline line (R2).

    One `Live` region for the whole run, low refresh rate, every print routed through the
    same `Console` — the risk #2 mitigations from the parent plan.
    """

    _ACTIVITY_TAIL = 8

    def __init__(self, console: Console | None = None, *, auto_refresh: bool = True) -> None:
        self._console = console or Console()
        self._auto_refresh = auto_refresh
        self._live: Live | None = None
        self._stage: Stage | None = None
        self._activities: list[str] = []
        self._closed = False

    def _build_renderable(self) -> Group:
        header = Spinner("dots", text=f"[bold]{self._stage}[/bold]")
        activity_lines = [Text(f"  {text}", style="dim") for text in self._activities]
        return Group(header, *activity_lines)

    def _start_live(self) -> None:
        self._live = Live(
            self._build_renderable(),
            console=self._console,
            refresh_per_second=4,
            transient=True,
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
            if self._live is not None:
                self._live.update("", refresh=True)
            self._console.print(
                f"[green]✓[/green] {event.stage} [dim]({event.elapsed_seconds:.1f}s)[/dim]"
            )
        elif isinstance(event, Question):
            self._console.print(Panel(event.text, border_style="cyan"))
        elif isinstance(event, RunFinished):
            lines = _summary_lines(event)
            self._console.print(lines[0], style="bold")
            for line in lines[1:]:
                self._console.print(line, style="dim")
        else:  # Activity
            self._activities = (self._activities + [event.text])[-self._ACTIVITY_TAIL :]
            if self._live is not None:
                self._live.update(self._build_renderable(), refresh=True)

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
            self._live.update("", refresh=True)
            self._live.stop()
            self._live = None


def build_renderer() -> Renderer:
    """Pick the renderer implementation: TTY -> `RichRenderer`, non-TTY -> `PlainRenderer` (R5)."""
    if sys.stdout.isatty():
        return RichRenderer()
    return PlainRenderer()
