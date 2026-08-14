"""Typed display events and renderers — the seam between the run loop
(`harness/__main__.py`, the sole emitter) and the terminal (D2).
"""

import sys
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import Literal, Protocol

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


DisplayEvent = StageStarted | StageCompleted | Activity


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

    def advance(self, stage: Stage) -> None:
        """Move to `stage`, completing whatever stage was current. A no-op if already there.

        One `clock()` read per transition: the same instant serves as both the elapsed
        endpoint of the stage being completed and the start of the new one.
        """
        if stage == self._current:
            return
        now = self._clock()
        if self._current is not None:
            self._renderer.emit(StageCompleted(self._current, now - self._started_at))
        self._renderer.emit(StageStarted(stage))
        self._current = stage
        self._started_at = now

    def finish(self) -> None:
        """Complete the current stage, if any. Safe to call with none current, or twice."""
        if self._current is None:
            return
        elapsed = self._clock() - self._started_at
        self._renderer.emit(StageCompleted(self._current, elapsed))
        self._current = None


def build_renderer() -> Renderer:
    """Pick the renderer implementation (Phase 1: always `PlainRenderer`)."""
    return PlainRenderer()
