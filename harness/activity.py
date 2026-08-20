"""Per-run tool-activity sink (Phase 6, D-A).

The producer is `harness/agent.py`'s `_ToolActivityMiddleware`, wrapping every researcher- and
reader-tier tool call; the consumer is `harness/__main__.py`, which builds `ToolCall`/
`ReadersUpdated` events (`harness/display.py`) from `records()`/`readers()`. Lives in its own
module, mirroring `harness/runlog.py`, for the same reason that one does: neither producer nor
consumer needs to import the other, and — since `harness/agent.py`'s docstring says it is "the
ONLY module that imports `deepagents`" — a plain data collector living there would drag that
~2s import cost into every consumer and test that only wants the sink.

PUSHED, not drained (fix-pass item 1): the middleware writes from inside the lead's `task` tool
NODE, and one node is one superstep, so no top-level `astream` chunk arrives until the whole
researcher->reader pipeline has finished -- a poll from the stream loop would see every live
transition arrive all at once, after the fact, with `live_reader_count() == 0` at every chunk in
between. `ActivitySink.__init__`'s `on_change` callback is called as the LAST action of every
mutating method, so `harness/__main__.py` can push events to the renderer the moment they happen.
"""

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

_ACTIVE_READER: ContextVar[str | None] = ContextVar("active_reader", default=None)


@dataclass(frozen=True)
class ToolCallRecord:
    """One observed tool call, at one point in its life."""

    call_id: str
    tool: str
    arg_summary: str
    result_summary: str | None  # None => still running
    elapsed_seconds: float | None  # None => still running
    retry: bool


@dataclass(frozen=True)
class ReaderState:
    id: str  # "reader/1", assigned in dispatch order
    brief: str  # the task call's `description` arg
    status_text: str
    done: bool


class DisplayError(Exception):
    """A failure in the sink's `on_change` CONSUMER -- the display, not the tool being observed.

    Exists to be excluded from the `task` retry/error guard in `harness/agent.py`. The sink
    pushes from inside `awrap_tool_call`, so without this an exception from the renderer would
    be caught by `ToolErrorMiddleware`, re-run a whole subagent once via `ToolRetryMiddleware`,
    and then surface as `"READER FAILED (...)"` plus a `subagent_failed` incident -- a display
    bug masquerading as a reader failure, at double that subagent's token cost.

    Defined here rather than in `harness/display.py` so `harness/agent.py` can name it without
    importing the display layer, which would point the dependency the wrong way. Raised by
    whoever supplies `on_change`; the sink itself never raises it.
    """


def _format_status_elapsed(seconds: float) -> str:
    """`{n}s` under a minute, `{m}m{s:02d}s` past it — the one home for BOTH reader status
    shapes (live and done), so those two cannot drift apart.

    Deliberately not named `_format_elapsed`: `harness/display.py` has its own function of
    that name producing a different shape (`MM:SS`, for the stage line), and two same-named
    formatters with different output invite the wrong import."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    return f"{minutes}m{secs:02d}s"


class ActivitySink:
    """Collects one run's observed tool calls and reader dispatches.

    No lock: every mutation happens inside `_ToolActivityMiddleware.awrap_tool_call`, which
    is coroutine code running on the one event loop, and the consumer's drain runs on that
    same loop -- there is no concurrent-thread access to guard against, so a lock here would
    guard against a condition that cannot occur (right-sizing, not an oversight).
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._clock = clock
        # Called as the LAST action of every mutating method below (see the module docstring
        # for why this pushes rather than being drained).
        self._on_change = on_change
        self._records: list[ToolCallRecord] = []
        self._readers: dict[str, ReaderState] = {}
        self._started: dict[str, float] = {}
        self._seen_call_ids: set[str] = set()
        # The most recent start's (tool, arg_summary, retry) for each call id, so `finish_call`
        # can carry them through without re-deriving anything from the caller.
        self._pending: dict[str, tuple[str, str, bool]] = {}
        self._reader_seq = 0

    def start_call(self, call_id: str, tool: str, arg_summary: str) -> bool:
        """Record a running call, returning whether this `call_id` was already seen (retry)."""
        retry = call_id in self._seen_call_ids
        self._seen_call_ids.add(call_id)
        self._started[call_id] = self._clock()
        self._pending[call_id] = (tool, arg_summary, retry)
        self._records.append(
            ToolCallRecord(
                call_id=call_id,
                tool=tool,
                arg_summary=arg_summary,
                result_summary=None,
                elapsed_seconds=None,
                retry=retry,
            )
        )
        self._notify()
        return retry

    def finish_call(self, call_id: str, result_summary: str) -> None:
        """Record the completion of `call_id`, carrying the same `retry` flag as its start."""
        tool, arg_summary, retry = self._pending.get(call_id, ("", "", False))
        started_at = self._started[call_id] if call_id in self._started else self._clock()
        self._records.append(
            ToolCallRecord(
                call_id=call_id,
                tool=tool,
                arg_summary=arg_summary,
                result_summary=result_summary,
                elapsed_seconds=self._clock() - started_at,
                retry=retry,
            )
        )
        self._notify()

    def start_reader(self, brief: str) -> str:
        """Assign the next `reader/N` id, in dispatch order -- the ONLY source of reader ids."""
        self._reader_seq += 1
        reader_id = f"reader/{self._reader_seq}"
        self._started[reader_id] = self._clock()
        self._readers[reader_id] = ReaderState(
            id=reader_id, brief=brief, status_text="dispatched", done=False
        )
        self._notify()
        return reader_id

    def reopen_reader(self, reader_id: str) -> None:
        """Bring a previously-failed reader row back to live, for a retried dispatch.

        Without this, a `task(reader)` retry that reuses the same call id (and so the same
        reader id, per the middleware's own reuse map) stayed marked `done`/failed from the
        first attempt for the whole retry: `live_reader_count()` undercounted, and the strip
        could vanish while the reader was still working.
        """
        current = self._readers.get(reader_id)
        if current is None:
            return
        self._readers[reader_id] = ReaderState(
            id=current.id, brief=current.brief, status_text="dispatched", done=False
        )
        self._notify()

    def _reader_elapsed(self, reader_id: str) -> str:
        """This reader's formatted elapsed time -- one home for both status shapes."""
        started = self._started.get(reader_id, self._clock())
        return _format_status_elapsed(self._clock() - started)

    def note_reader_tool(self, reader_id: str, tool: str) -> None:
        current = self._readers.get(reader_id)
        if current is None:
            return
        elapsed = self._reader_elapsed(reader_id)
        self._readers[reader_id] = ReaderState(
            id=current.id,
            brief=current.brief,
            status_text=f"{tool} · {elapsed}",
            done=False,
        )
        self._notify()

    def finish_reader(self, reader_id: str, *, failed: bool) -> None:
        current = self._readers.get(reader_id)
        if current is None:
            return
        elapsed = self._reader_elapsed(reader_id)
        # No sources-count clause (fix-pass item 4): nothing in the diff ever produces one --
        # `note_reader_source` was written by nothing and has been removed -- so this stays a
        # plain elapsed-only status rather than a count no real run could ever populate.
        status_text = f"failed · {elapsed}" if failed else f"done · {elapsed}"
        self._readers[reader_id] = ReaderState(
            id=current.id, brief=current.brief, status_text=status_text, done=True
        )
        self._notify()

    def _notify(self) -> None:
        if self._on_change is not None:
            self._on_change()

    def records(self) -> tuple[ToolCallRecord, ...]:
        """Every observed record so far, oldest first -- append-only and monotonic."""
        return tuple(self._records)

    def readers(self) -> tuple[ReaderState, ...]:
        """Every reader dispatched so far, in dispatch order -- a copy, immutable snapshot."""
        return tuple(self._readers.values())

    def live_reader_count(self) -> int:
        return sum(1 for reader in self._readers.values() if not reader.done)


@contextmanager
def reader_scope(reader_id: str) -> Iterator[None]:
    """Mark `reader_id` as the currently-executing reader for the duration of the block.

    Context-local (mirrors `harness/sources.py`'s `pending_digest_scope`), so a reader-tier
    tool call started inside can attribute itself via `active_reader()` without the caller
    threading an id through every tool signature. Restores the prior value on exit, including
    on an exception, so nested scopes (a reader's own nested dispatch, if any) unwind cleanly.
    """
    token = _ACTIVE_READER.set(reader_id)
    try:
        yield
    finally:
        _ACTIVE_READER.reset(token)


def active_reader() -> str | None:
    """The currently-scoped reader id, or `None` outside any `reader_scope` (lead/researcher
    tier tool calls) -- never an error, per D-D."""
    return _ACTIVE_READER.get()


def or_default(sink: "ActivitySink | None") -> ActivitySink:
    """The shared sink if one was passed, otherwise a throwaway one.

    `build_agent` and the fixtures that call `_reader_spec`/`_researcher_spec` directly both
    need this: a test that does not care about tool activity should not have to construct and
    thread its own `ActivitySink`, and this is the one place that default is decided.
    """
    return sink if sink is not None else ActivitySink()
