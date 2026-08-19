"""Raw-key reading and pure line-editing — the seam shared by the welcome screen
(Phase 2) and the `ask_user` overlay (Phase 5) (D1).
"""

import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Literal

KeyKind = Literal[
    "char", "enter", "newline", "backspace", "left", "right", "up", "down", "interrupt"
]


@dataclass(frozen=True)
class KeyEvent:
    kind: KeyKind
    char: str | None = None


def decode_posix(read_char: Callable[[], str]) -> KeyEvent | None:
    """Decode one POSIX raw-mode key. `\\r` is Enter, `\\n` is Ctrl+J — do not collapse them."""
    c = read_char()
    if c == "":
        return None
    if c == "\r":
        return KeyEvent("enter")
    if c == "\n":
        return KeyEvent("newline")
    if c in ("\x7f", "\x08"):
        return KeyEvent("backspace")
    if c == "\x03":
        return KeyEvent("interrupt")
    if c == "\x1b":
        nxt = read_char()
        if nxt != "[":
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
    if c == "\x03":
        return KeyEvent("interrupt")
    if c in ("\xe0", "\x00"):
        nxt = read_char()
        return {
            "H": KeyEvent("up"),
            "P": KeyEvent("down"),
            "K": KeyEvent("left"),
            "M": KeyEvent("right"),
        }.get(nxt)
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
        import termios
        import tty

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        tty.setraw(fd)
        try:
            while True:  # pragma: no cover -- blocking terminal read loop, no unit-testable seam
                first = sys.stdin.read(1)
                if first == "":
                    break
                pending = [first]

                def read_char() -> str:
                    if pending:
                        return pending.pop(0)
                    return sys.stdin.read(1)

                event = decode_posix(read_char)
                if event is not None:
                    yield event
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)


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
