"""Raw-key reading and pure line-editing — the seam shared by the welcome screen
(Phase 2) and the `ask_user` overlay (Phase 5) (D1).
"""

import sys
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

KeyKind = Literal[
    "char",
    "enter",
    "newline",
    "backspace",
    "word_backspace",
    "left",
    "right",
    "up",
    "down",
    "interrupt",
    "eof",
]

# Set by `read_keys()`'s POSIX branch to the closure that restores the saved `termios` mode,
# and popped by `restore_terminal()` before it is called -- a raising restore then cannot be
# retried into a loop. A closure over `(fd, saved)` rather than storing the pair directly is
# what keeps `restore_terminal()` itself platform-free: nothing here ever names `termios`.
_restore: Callable[[], None] | None = None


def restore_terminal() -> None:
    """Idempotently restore the terminal to its pre-raw-mode state, if anything set it raw.

    Safe to call from a thread other than the one running `read_keys()` -- the overlay's
    reader runs on a daemon thread, and if the wall clock cancels an open overlay that thread
    is parked in a blocking read, so its own `finally` never runs. A no-op on Windows, where
    `msvcrt` never changes terminal modes and nothing ever populates `_restore`.
    """
    global _restore
    restore, _restore = _restore, None
    if restore is not None:
        restore()


@dataclass(frozen=True)
class KeyEvent:
    kind: KeyKind
    char: str | None = None


def decode_posix(
    read_char: Callable[[], str],
    has_pending: Callable[[], bool] | None = None,
    unread: Callable[[str], None] | None = None,
) -> KeyEvent | None:
    """Decode one POSIX raw-mode key. `\\r` is Enter, `\\n` is Ctrl+J — do not collapse them.

    `has_pending` reports whether another byte is already buffered, and `unread` pushes an
    unconsumed byte back for the next decode. Both exist for the ESC branch alone: on a raw
    fd `read_char` BLOCKS, so a bare Escape (which sends `\\x1b` and nothing else) would
    otherwise swallow whatever the user typed next, and an Alt+key (`\\x1b` then the letter)
    would swallow the letter. `read_keys` supplies both; the pure decoder tests may omit
    them, in which case the lookahead stays blocking and an unrecognized byte is dropped.
    """
    c = read_char()
    if c == "":
        return None
    if c == "\r":
        return KeyEvent("enter")
    if c == "\n":
        return KeyEvent("newline")
    if c == "\x7f":
        return KeyEvent("backspace")
    if c == "\x08":
        # Ctrl+Backspace on the terminals that distinguish it (xterm-likes send `\x7f` for
        # plain Backspace and `\x08` for Ctrl+Backspace). A terminal configured to send
        # `\x08` for plain Backspace deletes a word instead of a character here -- accepted:
        # word-delete is the requested behavior and both keys still delete.
        return KeyEvent("word_backspace")
    if c == "\x03":
        return KeyEvent("interrupt")
    if c == "\x04":
        # Ctrl+D. Distinct from `interrupt` because the two differ downstream only in intent,
        # not in effect: the composer treats both as "quit", but a caller that wants
        # "end of input" separately from "abort" can tell them apart.
        return KeyEvent("eof")
    if c == "\x1b":
        if has_pending is not None and not has_pending():
            return None
        nxt = read_char()
        if nxt != "[":
            if unread is not None and nxt != "":
                unread(nxt)
            return None
        arrow = read_char()
        return {
            "A": KeyEvent("up"),
            "B": KeyEvent("down"),
            "C": KeyEvent("right"),
            "D": KeyEvent("left"),
        }.get(arrow)
    if ord(c) < 32:
        return None
    return KeyEvent("char", c)


def decode_windows(read_char: Callable[[], str]) -> KeyEvent | None:
    """Decode one Windows (`msvcrt`) raw key."""
    c = read_char()
    if c == "":
        return None
    if c == "\r":
        return KeyEvent("enter")
    if c == "\n":
        return KeyEvent("newline")
    if c == "\x08":
        return KeyEvent("backspace")
    if c == "\x7f":
        # `msvcrt.getwch()` returns `\x7f` for Ctrl+Backspace (plain Backspace is `\x08`).
        # Unmapped, it slipped past the extended-key branch, and ord 127 passes the `< 32`
        # guard below, so a literal DEL character landed in `LineBuffer` (PR #25 review).
        return KeyEvent("word_backspace")
    if c == "\x03":
        return KeyEvent("interrupt")
    if c == "\x04":
        # `getwch()` returns the same byte for Ctrl+D as a POSIX raw read, so both decoders
        # agree on this one without a platform branch.
        return KeyEvent("eof")
    if c in ("\xe0", "\x00"):
        nxt = read_char()
        return {
            "H": KeyEvent("up"),
            "P": KeyEvent("down"),
            "K": KeyEvent("left"),
            "M": KeyEvent("right"),
        }.get(nxt)
    # Same guard as `decode_posix`, for the same reason: `msvcrt.getwch()` returns Escape as
    # a bare `\x1b` and Tab as `\t`, neither of which is an `\xe0`/`\x00` extended-key pair,
    # so without this they reach `LineBuffer` as insertable text.
    if ord(c) < 32:
        return None
    return KeyEvent("char", c)


def read_keys() -> Iterator[KeyEvent]:
    """Blocking generator over raw terminal key events, platform-dispatched on `sys.platform`."""
    if sys.platform == "win32":
        import msvcrt

        while True:  # pragma: no cover -- blocking terminal read loop, no unit-testable seam
            event = decode_windows(msvcrt.getwch)
            if event is not None:
                yield event
    else:
        import select
        import termios
        import tty

        global _restore

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        tty.setraw(fd)

        def _do_restore() -> None:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)

        _restore = _do_restore

        # Outside the loop, not rebuilt per iteration: `decode_posix`'s ESC branch pushes an
        # unconsumed byte back here, and it has to survive into the NEXT decode to be read.
        pending: list[str] = []

        def read_char() -> str:
            if pending:
                return pending.pop(0)
            return sys.stdin.read(1)

        def has_pending() -> bool:
            # A zero timeout makes this a probe, never a wait: it answers "did the terminal
            # send more of this sequence already", which is what separates a real arrow key
            # from a bare Escape.
            return bool(pending) or bool(select.select([fd], [], [], 0)[0])

        def unread(ch: str) -> None:
            pending.insert(0, ch)

        try:
            while True:  # pragma: no cover -- blocking terminal read loop, no unit-testable seam
                first = read_char()
                if first == "":
                    break
                unread(first)
                event = decode_posix(read_char, has_pending, unread)
                if event is not None:
                    yield event
        finally:
            restore_terminal()


@contextmanager
def scoped_keys(keys: Iterable[KeyEvent]) -> Iterator[Iterator[KeyEvent]]:
    """Scope a key source so a generator's raw-mode restore always runs.

    `read_keys()` puts its `termios` restore in a `finally`, which only executes when the
    generator is closed — so a consumer that parks the iterator and keeps running would
    leave the terminal raw. Routing every consumer through here makes the release
    structural instead of a convention each call site has to remember (Phase 1 review).

    A plain iterable — what the tests inject — has nothing to close and passes straight
    through, which is why the close is probed rather than required. Probing it HERE, next
    to the generator that owns raw mode, keeps it out of every call site.
    """
    iterator = iter(keys)
    try:
        yield iterator
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()


class LineBuffer:
    """Pure multi-line text buffer with cursor tracking — no I/O."""

    def __init__(self) -> None:
        self._lines: list[str] = [""]
        self.cursor_row: int = 0
        self.cursor_col: int = 0

    def insert(self, ch: str) -> None:
        line = self._lines[self.cursor_row]
        self._lines[self.cursor_row] = line[: self.cursor_col] + ch + line[self.cursor_col :]
        self.cursor_col += 1

    def newline(self) -> None:
        line = self._lines[self.cursor_row]
        before, after = line[: self.cursor_col], line[self.cursor_col :]
        self._lines[self.cursor_row] = before
        self._lines.insert(self.cursor_row + 1, after)
        self.cursor_row += 1
        self.cursor_col = 0

    def backspace(self) -> None:
        if self.cursor_col > 0:
            line = self._lines[self.cursor_row]
            self._lines[self.cursor_row] = line[: self.cursor_col - 1] + line[self.cursor_col :]
            self.cursor_col -= 1
        elif self.cursor_row > 0:
            prev_len = len(self._lines[self.cursor_row - 1])
            self._lines[self.cursor_row - 1] += self._lines[self.cursor_row]
            del self._lines[self.cursor_row]
            self.cursor_row -= 1
            self.cursor_col = prev_len

    def word_backspace(self) -> None:
        """Delete back to the start of the previous word -- trailing spaces first, then the
        word itself. At the start of a row it joins lines, exactly like `backspace`."""
        if self.cursor_col == 0:
            self.backspace()
            return
        line = self._lines[self.cursor_row]
        col = self.cursor_col
        while col > 0 and line[col - 1] == " ":
            col -= 1
        while col > 0 and line[col - 1] != " ":
            col -= 1
        self._lines[self.cursor_row] = line[:col] + line[self.cursor_col :]
        self.cursor_col = col

    def move_left(self) -> None:
        if self.cursor_col > 0:
            self.cursor_col -= 1
        elif self.cursor_row > 0:
            self.cursor_row -= 1
            self.cursor_col = len(self._lines[self.cursor_row])

    def move_right(self) -> None:
        line = self._lines[self.cursor_row]
        if self.cursor_col < len(line):
            self.cursor_col += 1
        elif self.cursor_row < len(self._lines) - 1:
            self.cursor_row += 1
            self.cursor_col = 0

    def move_up(self) -> None:
        if self.cursor_row == 0:
            return
        self.cursor_row -= 1
        self.cursor_col = min(self.cursor_col, len(self._lines[self.cursor_row]))

    def move_down(self) -> None:
        if self.cursor_row >= len(self._lines) - 1:
            return
        self.cursor_row += 1
        self.cursor_col = min(self.cursor_col, len(self._lines[self.cursor_row]))

    def text(self) -> str:
        return "\n".join(self._lines)
