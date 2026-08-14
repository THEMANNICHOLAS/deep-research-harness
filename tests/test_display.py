"""Behavioral tests for harness.display: events, PlainRenderer, and StageTracker."""

from contextlib import AbstractContextManager
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage

import harness.__main__ as main_module
from harness.display import (
    Activity,
    DisplayEvent,
    PlainRenderer,
    StageCompleted,
    StageStarted,
    StageTracker,
    build_renderer,
)
from tests.conftest import drain_stdout, install_search_transport, patch_run


@pytest.mark.parametrize(
    ("event", "expected_line"),
    [
        (StageStarted("researching"), "researching..."),
        (StageCompleted("researching", 12.34), "researching done (12.3s)"),
        (Activity("[pending] Find sources"), "  [pending] Find sources"),
    ],
)
def test_plain_renderer_emits_the_expected_line(capsys, event: DisplayEvent, expected_line: str):
    renderer = PlainRenderer()

    renderer.emit(event)

    out, lines = drain_stdout(capsys)
    assert lines == [expected_line]


def test_plain_renderer_suspend_is_a_no_op_context_manager(capsys):
    renderer = PlainRenderer()

    with renderer.suspend():
        pass

    out, _ = drain_stdout(capsys)
    assert out == ""


def test_plain_renderer_close_is_a_no_op():
    renderer = PlainRenderer()

    renderer.close()  # must not raise


def test_build_renderer_returns_a_plain_renderer():
    assert isinstance(build_renderer(), PlainRenderer)


class _RecordingRenderer:
    """A fake `Renderer` that records emitted events instead of printing them."""

    def __init__(self) -> None:
        self.events: list[DisplayEvent] = []

    def emit(self, event: DisplayEvent) -> None:
        self.events.append(event)

    def suspend(self) -> AbstractContextManager[None]:
        raise NotImplementedError

    def close(self) -> None:
        pass


def _fake_clock(times: list[float]):
    values = iter(times)

    def _clock() -> float:
        return next(values)

    return _clock


def test_stage_tracker_advancing_to_the_same_stage_twice_emits_one_stage_started():
    renderer = _RecordingRenderer()
    tracker = StageTracker(renderer, clock=_fake_clock([0.0, 1.0]))

    tracker.advance("researching")
    tracker.advance("researching")

    assert renderer.events == [StageStarted("researching")]


def test_stage_tracker_advancing_to_a_new_stage_completes_the_old_one_first():
    renderer = _RecordingRenderer()
    tracker = StageTracker(renderer, clock=_fake_clock([0.0, 5.0]))

    tracker.advance("researching")
    tracker.advance("verifying")

    assert renderer.events == [
        StageStarted("researching"),
        StageCompleted("researching", 5.0),
        StageStarted("verifying"),
    ]


def test_stage_tracker_finish_with_no_current_stage_emits_nothing():
    renderer = _RecordingRenderer()
    tracker = StageTracker(renderer, clock=_fake_clock([]))

    tracker.finish()

    assert renderer.events == []


def test_stage_tracker_finish_emits_completion_and_is_safe_to_call_twice():
    renderer = _RecordingRenderer()
    tracker = StageTracker(renderer, clock=_fake_clock([0.0, 3.5]))

    tracker.advance("writing")
    tracker.finish()
    tracker.finish()

    assert renderer.events == [
        StageStarted("writing"),
        StageCompleted("writing", 3.5),
    ]


def _install_stub_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route `search_web` to a no-network empty-results stub (ordering caveat: see helper)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"query": "x", "results": []})

    install_search_transport(monkeypatch, handler)


# --- Graph-driven tests (drive main() with a scripted model) ---------------------------


async def test_a_direct_answer_skips_the_clarifying_and_researching_lines(
    make_config, monkeypatch, scripted_model, capsys
):
    config = make_config()
    ping = AIMessage(content="pong")
    final = AIMessage(
        content="Final answer.",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    model = scripted_model([ping, final])
    patch_run(monkeypatch, config, model)

    await main_module.main(["a question with no tool calls"])

    out, lines = drain_stdout(capsys)
    assert "clarifying..." not in out
    assert "researching..." not in out
    assert lines[-1].strip().endswith(".md")


async def test_a_research_call_and_todo_produce_activity_lines(
    make_config, monkeypatch, scripted_model, capsys
):
    config = make_config()
    ping = AIMessage(content="pong")
    plan_and_search: list[Any] = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_todos",
                    "args": {"todos": [{"content": "Search for the answer", "status": "pending"}]},
                    "id": "call_todo",
                },
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "search_web",
                    "args": {"query": "the answer"},
                    "id": "call_search",
                }
            ],
        ),
    ]
    final = AIMessage(
        content="Final answer.",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    model = scripted_model([ping, *plan_and_search, final])
    patch_run(monkeypatch, config, model)
    _install_stub_search(monkeypatch)

    await main_module.main(["a question needing research"])

    out, lines = drain_stdout(capsys)
    assert '  search_web: "the answer"' in out
    assert "  [pending] Search for the answer" in out
    assert lines[-1].strip().endswith(".md")
