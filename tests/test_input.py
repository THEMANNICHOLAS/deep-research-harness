"""Behavioral tests for harness.input: raw-key decoding and pure line editing."""

from collections.abc import Callable

import pytest

from harness.input import KeyEvent, LineBuffer, decode_posix, decode_windows


def _reader(sequence: str) -> Callable[[], str]:
    """A fake `read_char` over a fixed string, returning "" once exhausted."""
    chars = iter(sequence)

    def read_char() -> str:
        return next(chars, "")

    return read_char


@pytest.mark.parametrize(
    ("byte_sequence", "expected"),
    [
        ("a", KeyEvent("char", "a")),
        ("Z", KeyEvent("char", "Z")),
        (" ", KeyEvent("char", " ")),
        ("\r", KeyEvent("enter", None)),
        ("\n", KeyEvent("newline", None)),
        ("\x7f", KeyEvent("backspace", None)),
        ("\x08", KeyEvent("backspace", None)),
        ("\x03", KeyEvent("interrupt", None)),
        ("\x1b[A", KeyEvent("up", None)),
        ("\x1b[B", KeyEvent("down", None)),
        ("\x1b[C", KeyEvent("right", None)),
        ("\x1b[D", KeyEvent("left", None)),
        ("\x1b", None),
        ("\x01", None),
        # An exhausted reader decodes to None rather than raising: `ord("")` is a
        # ValueError, and Phases 2 and 5 call this decoder directly (PR review, Phase 1).
        ("", None),
    ],
)
def test_decode_posix(byte_sequence: str, expected: KeyEvent | None):
    assert decode_posix(_reader(byte_sequence)) == expected


@pytest.mark.parametrize(
    ("byte_sequence", "expected"),
    [
        ("a", KeyEvent("char", "a")),
        ("\r", KeyEvent("enter", None)),
        ("\n", KeyEvent("newline", None)),
        ("\x08", KeyEvent("backspace", None)),
        ("\x03", KeyEvent("interrupt", None)),
        ("\xe0H", KeyEvent("up", None)),
        ("\xe0P", KeyEvent("down", None)),
        ("\xe0K", KeyEvent("left", None)),
        ("\xe0M", KeyEvent("right", None)),
        ("\x00H", KeyEvent("up", None)),
        ("\xe0S", None),
        # Control bytes are ignored, not inserted: `getwch()` returns Escape and Tab as bare
        # control characters outside the `\xe0`/`\x00` pair, and without a guard they reached
        # `LineBuffer` as text and corrupted the submitted string (PR #25 review).
        ("\x1b", None),
        ("\t", None),
        ("\x01", None),
        # An exhausted reader decodes to None, never to an empty-char event: both
        # decoders must agree on the "" EOF sentinel their callable contract names,
        # since Phases 2 and 5 consume them directly (PR review, Phase 1).
        ("", None),
    ],
)
def test_decode_windows(byte_sequence: str, expected: KeyEvent | None):
    assert decode_windows(_reader(byte_sequence)) == expected


def test_a_bare_escape_does_not_consume_the_next_keystroke():
    """PR #25 review, Major.

    On a raw fd `read_char` blocks, so the old unconditional lookahead ate whatever the user
    typed after a bare Escape -- typing Esc then "hi" rendered "i". `has_pending` reports
    that nothing followed the ESC, so the decoder gives up without reading.
    """
    read_char = _reader("\x1bhi")
    reads: list[str] = []

    def counting_read() -> str:
        ch = read_char()
        reads.append(ch)
        return ch

    assert decode_posix(counting_read, lambda: False) is None
    # The ESC and nothing else: "h" is still queued for the next decode.
    assert reads == ["\x1b"]
    assert decode_posix(counting_read, lambda: True) == KeyEvent("char", "h")


def test_an_escape_sequence_that_is_not_a_csi_pushes_its_byte_back():
    """PR #25 review, Major (Alt+key half).

    Alt+b sends ESC then "b" together, so a byte IS pending -- but it is not `[`, and the old
    code dropped it. The letter is now unread and decodes on the next call instead.
    """
    read_char = _reader("\x1bb")
    pushed: list[str] = []

    assert decode_posix(read_char, lambda: True, pushed.append) is None
    assert pushed == ["b"]


def test_the_escape_lookahead_stays_blocking_when_no_probe_is_supplied():
    """The pure-decoder default is unchanged, so `decode_posix(read_char)` keeps one meaning."""
    assert decode_posix(_reader("\x1b")) is None
    assert decode_posix(_reader("\x1b[A")) == KeyEvent("up", None)


def test_line_buffer_insert_into_empty_buffer():
    buf = LineBuffer()

    buf.insert("a")
    buf.insert("b")

    assert (buf.text(), buf.cursor_row, buf.cursor_col) == ("ab", 0, 2)


def test_line_buffer_insert_at_cursor_mid_line():
    buf = LineBuffer()
    for ch in "abc":
        buf.insert(ch)
    buf.move_left()
    buf.move_left()

    buf.insert("X")

    assert (buf.text(), buf.cursor_row, buf.cursor_col) == ("aXbc", 0, 2)


def test_line_buffer_backspace_mid_line():
    buf = LineBuffer()
    for ch in "abc":
        buf.insert(ch)

    buf.backspace()

    assert (buf.text(), buf.cursor_row, buf.cursor_col) == ("ab", 0, 2)


def test_line_buffer_backspace_at_start_of_buffer_is_noop():
    buf = LineBuffer()

    buf.backspace()

    assert (buf.text(), buf.cursor_row, buf.cursor_col) == ("", 0, 0)


def test_line_buffer_move_left_at_position_zero_is_noop():
    buf = LineBuffer()
    buf.insert("a")
    buf.move_left()

    buf.move_left()

    assert (buf.cursor_row, buf.cursor_col) == (0, 0)


def test_line_buffer_move_right_at_end_of_last_line_is_noop():
    buf = LineBuffer()
    buf.insert("a")

    buf.move_right()

    assert (buf.cursor_row, buf.cursor_col) == (0, 1)


def test_line_buffer_newline_splits_at_cursor():
    buf = LineBuffer()
    for ch in "abcd":
        buf.insert(ch)
    buf.move_left()
    buf.move_left()

    buf.newline()

    assert (buf.text(), buf.cursor_row, buf.cursor_col) == ("ab\ncd", 1, 0)


def test_line_buffer_backspace_at_start_of_row_joins_lines():
    buf = LineBuffer()
    for ch in "abcd":
        buf.insert(ch)
    buf.move_left()
    buf.move_left()
    buf.newline()

    buf.backspace()

    assert (buf.text(), buf.cursor_row, buf.cursor_col) == ("abcd", 0, 2)


def test_line_buffer_move_up_clamps_column():
    buf = LineBuffer()
    for ch in "ab":
        buf.insert(ch)
    buf.newline()
    for ch in "longline":
        buf.insert(ch)

    buf.move_up()

    assert (buf.cursor_row, buf.cursor_col) == (0, 2)


def test_line_buffer_move_down_clamps_column():
    buf = LineBuffer()
    for ch in "longline":
        buf.insert(ch)
    buf.newline()
    buf.insert("a")
    buf.insert("b")
    buf.move_up()

    buf.move_down()

    assert (buf.cursor_row, buf.cursor_col) == (1, 2)


def test_line_buffer_move_up_at_row_zero_is_noop():
    buf = LineBuffer()
    buf.insert("a")

    buf.move_up()

    assert (buf.cursor_row, buf.cursor_col) == (0, 1)


def test_line_buffer_move_down_at_last_row_is_noop():
    buf = LineBuffer()
    buf.insert("a")

    buf.move_down()

    assert (buf.cursor_row, buf.cursor_col) == (0, 1)


def test_line_buffer_move_left_at_col_zero_of_row_wraps_to_previous_line_end():
    buf = LineBuffer()
    buf.insert("a")
    buf.newline()
    buf.insert("b")

    buf.move_left()
    buf.move_left()

    assert (buf.cursor_row, buf.cursor_col) == (0, 1)


def test_line_buffer_move_right_at_end_of_row_wraps_to_next_line_start():
    buf = LineBuffer()
    buf.insert("a")
    buf.newline()
    buf.insert("b")
    buf.move_up()

    buf.move_right()

    assert (buf.cursor_row, buf.cursor_col) == (1, 0)


def test_line_buffer_text_joins_multiline_with_newline():
    buf = LineBuffer()
    buf.insert("a")
    buf.newline()
    buf.insert("b")

    assert buf.text() == "a\nb"


# --- Phase 5: idempotent terminal restore --------------------------------------------------


def test_restore_terminal_with_nothing_registered_is_a_noop():
    from harness.input import restore_terminal

    restore_terminal()  # must not raise


def test_restore_terminal_calls_the_registered_spy_exactly_once_and_is_idempotent():
    import harness.input as input_module

    calls: list[None] = []
    input_module._restore = lambda: calls.append(None)

    input_module.restore_terminal()
    input_module.restore_terminal()

    assert calls == [None]
