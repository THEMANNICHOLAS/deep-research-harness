"""Behavioral tests for harness.activity: the per-run tool-activity sink (Phase 6, D-A)."""

import pytest

from harness.activity import ActivitySink, active_reader, brief_summary, reader_scope


def _clock_from(*values: float):
    """A clock returning each of `values` in order, then repeating the last forever."""
    remaining = list(values)

    def _clock() -> float:
        if remaining:
            return remaining.pop(0)
        return values[-1]

    return _clock


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Read S3", "Read S3"),
        ("", ""),
        ("   \n\n  ", ""),
        # First sentence only: the rest of a multi-sentence delegation prompt is dropped.
        ("Read the OpenAI post. Extract every claim with its marker.", "Read the OpenAI post."),
        ("Any URLs left? Then fetch them next.", "Any URLs left?"),
        # First line only: a multi-line prompt's body never reaches a one-line row.
        ("Read these pages:\n1. https://a.example\n2. https://b.example", "Read these pages:"),
        # A first sentence past the cap is truncated with an ellipsis, not shown whole.
        ("x" * 100, "x" * 80 + "…"),
    ],
)
def test_brief_summary_reduces_a_task_description_to_one_short_line(text, expected):
    assert brief_summary(text) == expected


def test_start_reader_stores_the_brief_summarized_not_the_full_description():
    """PR #25 review: the reader strip showed the model's whole multi-hundred-word delegation
    prompt (saved only by render-time ellipsis in ONE consumer); the sink now summarizes at
    the point of record so every consumer sees a one-line brief."""
    sink = ActivitySink()
    long_description = "Read the Anthropic engineering posts. " + "Extract everything. " * 30

    reader_id = sink.start_reader(long_description)

    (reader,) = sink.readers()
    assert reader.id == reader_id
    assert reader.brief == "Read the Anthropic engineering posts."


def test_start_call_marks_a_repeat_call_id_as_retry():
    sink = ActivitySink(clock=_clock_from(0.0, 0.0))

    first = sink.start_call("call_1", "task", "researcher -- Angle A")
    second = sink.start_call("call_1", "task", "researcher -- Angle A")

    assert first is False
    assert second is True
    records = sink.records()
    assert len(records) == 2
    assert records[0].retry is False
    assert records[1].retry is True


def test_finish_call_carries_the_retry_flag_and_computes_elapsed_from_the_clock():
    # 0.0: original start. 5.0: the retried start. 8.0: the finish.
    sink = ActivitySink(clock=_clock_from(0.0, 5.0, 8.0))

    sink.start_call("call_1", "task", "researcher -- Angle A")
    sink.start_call("call_1", "task", "researcher -- Angle A")  # retry, restamps the start
    sink.finish_call("call_1", "done")

    finished = [r for r in sink.records() if r.result_summary is not None]
    assert len(finished) == 1
    assert finished[0].retry is True
    # Elapsed measures from the RETRY's own start (5.0), not the original start (0.0) -- a
    # stub that always measured from the first start, or that hardcoded some elapsed value,
    # would fail this.
    assert finished[0].elapsed_seconds == pytest.approx(3.0)


def test_start_reader_assigns_distinct_sequential_ids():
    sink = ActivitySink()

    first = sink.start_reader("Angle A")
    second = sink.start_reader("Angle B")

    assert first == "reader/1"
    assert second == "reader/2"
    assert first != second


def test_finish_reader_failed_marks_the_row_done_with_a_failed_status():
    sink = ActivitySink()
    reader_id = sink.start_reader("Angle A")

    sink.finish_reader(reader_id, failed=True)

    readers = sink.readers()
    assert len(readers) == 1
    assert readers[0].done is True
    assert "failed" in readers[0].status_text


def test_live_reader_count_and_readers_reflect_start_and_finish_transitions():
    sink = ActivitySink()

    reader_a = sink.start_reader("Angle A")
    reader_b = sink.start_reader("Angle B")
    assert sink.live_reader_count() == 2

    sink.finish_reader(reader_a, failed=False)
    assert sink.live_reader_count() == 1
    readers = sink.readers()
    assert len(readers) == 2
    done_ids = {r.id for r in readers if r.done}
    live_ids = {r.id for r in readers if not r.done}
    assert done_ids == {reader_a}
    assert live_ids == {reader_b}

    sink.finish_reader(reader_b, failed=False)
    assert sink.live_reader_count() == 0


def test_reader_scope_sets_and_restores_active_reader_on_normal_exit():
    assert active_reader() is None

    with reader_scope("reader/1"):
        assert active_reader() == "reader/1"
        # Nested, to prove "restores the PRIOR value", not just "clears to None".
        with reader_scope("reader/2"):
            assert active_reader() == "reader/2"
        assert active_reader() == "reader/1"

    assert active_reader() is None


def test_reopen_reader_brings_a_failed_row_back_to_live():
    """Fix-pass item 3: a retried dispatch reuses the same reader id, but the failed first
    attempt already marked it done -- without `reopen_reader`, the whole retry would read as
    finished and `live_reader_count()` would undercount."""
    sink = ActivitySink()
    reader_id = sink.start_reader("Angle A")
    sink.finish_reader(reader_id, failed=True)
    assert sink.live_reader_count() == 0

    sink.reopen_reader(reader_id)

    assert sink.live_reader_count() == 1
    readers = sink.readers()
    assert len(readers) == 1
    assert readers[0].done is False
    assert "failed" not in readers[0].status_text


def test_reopen_reader_restarts_the_rows_elapsed_clock():
    """PR #25 review, Minor: the row's elapsed time must measure THIS attempt.

    Leaving the original stamp in place made a just-retried reader display the failed
    attempt's accumulated time, so a reader that had only just restarted read as stuck --
    and disagreed with `start_call`, which re-stamps per attempt for the tool log.
    """
    # Dispatch at t=0, fail at t=100, retry at t=100, then report at t=103.
    sink = ActivitySink(clock=_clock_from(0.0, 100.0, 100.0, 103.0))
    reader_id = sink.start_reader("Angle A")
    sink.finish_reader(reader_id, failed=True)

    sink.reopen_reader(reader_id)
    sink.note_reader_tool(reader_id, "fetch_pages")

    # 3s into the retry, not 103s since the original dispatch.
    assert sink.readers()[0].status_text == "fetch_pages · 3s"


def test_on_change_fires_as_the_last_action_of_every_mutating_method():
    """Fix-pass item 1: the sink PUSHES rather than being drained -- every mutation must call
    `on_change` after its own state update is visible, not before."""
    calls: list[tuple[int, tuple[str, ...]]] = []

    def _on_change() -> None:
        # Both live views in one probe: the reader count and the researcher roster (R8), so
        # every mutating method is enumerated here rather than in a second near-identical test.
        calls.append(
            (sink.live_reader_count(), tuple(state.status for state in sink.researchers()))
        )

    sink = ActivitySink(on_change=_on_change)

    sink.start_call("call_1", "search_web", "a query")
    assert len(calls) == 1
    sink.finish_call("call_1", "done")
    assert len(calls) == 2

    reader_id = sink.start_reader("Angle A")
    assert calls[-1][0] == 1  # the new reader is already visible when `on_change` fires
    sink.note_reader_tool(reader_id, "fetch_pages")
    assert len(calls) == 4
    sink.finish_reader(reader_id, failed=False)
    assert calls[-1][0] == 0  # the finish is already visible too
    sink.reopen_reader(reader_id)
    assert calls[-1][0] == 1
    assert len(calls) == 6

    sink.start_researcher("researcher/1", "pricing")
    assert calls[-1][1] == ("running",)  # the new researcher is already visible too
    sink.finish_researcher("researcher/1", failed=True)
    assert calls[-1][1] == ("failed",)
    assert len(calls) == 8


def test_researchers_records_the_roster_in_start_order_with_its_own_clock():
    """R8's data: the lead's researchers, in dispatch order, each with the times the roster
    view renders elapsed from -- the sink's injected clock, exactly like the reader rows."""
    sink = ActivitySink(clock=_clock_from(0.0, 1.0, 2.0))

    sink.start_researcher("researcher/1", "pricing")
    sink.start_researcher("researcher/2", "supply chain")
    sink.finish_researcher("researcher/1", failed=False)

    researchers = sink.researchers()
    # A list, not a tuple: the Contracts entry says `list[ResearcherState]`.
    assert isinstance(researchers, list)
    assert [state.id for state in researchers] == ["researcher/1", "researcher/2"]
    assert [state.label for state in researchers] == ["pricing", "supply chain"]
    assert [state.status for state in researchers] == ["done", "running"]
    assert researchers[0].started_at == 0.0
    assert researchers[0].finished_at == 2.0
    assert researchers[1].started_at == 1.0
    # Still running: nothing has finished it, so it carries no finish time to render.
    assert researchers[1].finished_at is None


def test_finish_researcher_failed_marks_the_row_failed_not_done():
    """The roster distinguishes a researcher that reported from one that crashed (or was
    cancelled) -- `done` and `failed` are both finished, and the report's disclosure differs."""
    sink = ActivitySink()
    sink.start_researcher("researcher/1", "pricing")

    sink.finish_researcher("researcher/1", failed=True)

    (state,) = sink.researchers()
    assert state.status == "failed"
    assert state.finished_at is not None


def test_finish_researcher_ignores_an_id_the_sink_never_started():
    """`Session._cancel_running` finishes whatever is on its roster, which -- in tests, and
    after a torn-down run -- can hold a task the sink never saw start; mirrors
    `finish_reader`'s own missing-id guard rather than raising."""
    sink = ActivitySink()

    sink.finish_researcher("researcher/9", failed=True)

    assert sink.researchers() == []


def test_reader_scope_restores_active_reader_on_exception():
    with pytest.raises(RuntimeError):
        with reader_scope("reader/1"):
            assert active_reader() == "reader/1"
            raise RuntimeError("boom")

    assert active_reader() is None
