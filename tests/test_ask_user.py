"""Behavioral tests for the `ask_user` clarification tool and its interrupt/resume protocol.

Every test driving a real graph does so via `build_agent`: nothing about deepagents is mocked,
only the model and — for the `__main__` tests — config loading, `preflight` and `_read_answer`.
"""

import asyncio
import threading
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import harness.__main__ as main_module
from harness.agent import build_agent
from harness.display import PlainRenderer
from harness.input import KeyEvent
from harness.sources import SourceRegistry
from harness.tools.ask_user import build_ask_user_tool
from tests.conftest import (
    _dispatch_call,
    _submit_call,
    drain_stdout,
    install_search_transport,
    patch_model,
    patch_run,
    patch_run_by_role,
)

_THREAD = {"configurable": {"thread_id": "test-thread"}}


def _ask(question: str, call_id: str = "call_1") -> dict:
    """One `ask_user` tool call, in the shape a real model emits."""
    return {"name": "ask_user", "args": {"question": question}, "id": call_id}


def _patch_main(monkeypatch, config, model, answers=None):
    """Patch everything `main()` reaches outside the graph, and script the answers.

    `answers` is consumed in order by the patched `_read_answer`; `None` means the test expects NO
    question, so any call fails it. Returns the queue, so a test can assert every answer was used.
    """
    patch_run(monkeypatch, config, model, skip_preflight=True)

    queued = list(answers or [])

    # `*_args, **_kwargs`, not `prompt: str = "> "` (Phase 5, step 12): `_read_answer` gains a
    # leading `renderer` positional argument, and this fake must accept whatever shape the call
    # site passes without caring — the assertions in every test below stay exactly as they are.
    async def _fake_read_answer(*_args: object, **_kwargs: object) -> str:
        if answers is None:
            raise AssertionError("_read_answer was called, but this run should never ask")
        if not queued:
            raise AssertionError("_read_answer was called more times than the test scripted")
        return queued.pop(0)

    monkeypatch.setattr(main_module, "_read_answer", _fake_read_answer)
    return queued


def _ask_user_results(request) -> list[ToolMessage]:
    """Every `ask_user` tool result in one recorded model request, in order."""
    return [m for m in request if isinstance(m, ToolMessage) and m.name == "ask_user"]


async def test_an_ask_user_call_interrupts_the_run_instead_of_completing(
    make_config, monkeypatch, scripted_model
):
    config = make_config()
    model = scripted_model([AIMessage(content="", tool_calls=[_ask("Metal or album?")])])
    patch_model(monkeypatch, model)

    graph = build_agent(config, SourceRegistry())
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="Tell me about mercury")]}, config=_THREAD
    )

    assert "__interrupt__" in result
    action_request = result["__interrupt__"][0].value["action_requests"][0]
    assert action_request["name"] == "ask_user"
    assert action_request["args"]["question"] == "Metal or album?"

    # The script holds one reply, so a run that carried on would have overrun it.
    assert model._call_count == 1


async def test_the_question_reaches_stdout_and_the_answer_resumes_the_run(
    make_config, monkeypatch, scripted_model, capsys
):
    model = scripted_model(
        [
            AIMessage(content="", tool_calls=[_ask("Metal or album?")]),
            _submit_call("Final answer.").model_copy(
                update={
                    "usage_metadata": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                    }
                }
            ),
            AIMessage(content="Report submitted."),
        ]
    )
    queued = _patch_main(monkeypatch, make_config(), model, ["I mean the chemical element."])

    exit_code = await main_module.main(["Tell me about mercury"])

    assert exit_code == 0
    assert queued == []
    out, lines = drain_stdout(capsys)
    assert "Metal or album?" in out
    # The report path stays the last line of stdout (R1).
    assert lines[-1].strip().endswith(".md")

    results = _ask_user_results(model._received_messages[1])
    assert [m.content for m in results] == ["I mean the chemical element."]


async def test_a_second_clarification_round_asks_again_and_resumes_again(
    make_config, monkeypatch, scripted_model, capsys
):
    """The resume path is a LOOP, not a single `if`: with two sequential `ask_user` rounds, a
    one-shot resume leaves the second question unasked, failing both the stdout assertion and the
    second tool result.
    """
    model = scripted_model(
        [
            AIMessage(content="", tool_calls=[_ask("Metal or album?", "call_1")]),
            AIMessage(content="", tool_calls=[_ask("Which isotope?", "call_2")]),
            _submit_call("Final answer.").model_copy(
                update={
                    "usage_metadata": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                    }
                }
            ),
            AIMessage(content="Report submitted."),
        ]
    )
    queued = _patch_main(monkeypatch, make_config(), model, ["The metal.", "Mercury-202."])

    exit_code = await main_module.main(["Tell me about mercury"])

    assert exit_code == 0
    assert queued == [], "the second round never asked"
    out, lines = drain_stdout(capsys)
    assert "Metal or album?" in out
    assert "Which isotope?" in out
    assert lines[-1].strip().endswith(".md")

    # Four model calls: ask, ask again, submit_report, and the turn that closes it. A
    # one-round resume loop stops at two.
    assert model._call_count == 4
    # Both answers arrived as their own round's tool result, in order.
    assert [m.content for m in _ask_user_results(model._received_messages[2])] == [
        "The metal.",
        "Mercury-202.",
    ]


async def test_two_questions_in_one_interrupt_get_one_answer_each(
    make_config, monkeypatch, scripted_model, capsys
):
    """One decision per action request, correctly paired: two `ask_user` calls in a single
    `AIMessage` arrive as ONE interrupt with two action requests, and a short or mis-ordered
    decisions list raises `ValueError` inside the middleware.
    """
    model = scripted_model(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _ask("Metal or album?", "call_1"),
                    _ask("Which isotope?", "call_2"),
                ],
            ),
            _submit_call("Final answer.").model_copy(
                update={
                    "usage_metadata": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                    }
                }
            ),
            AIMessage(content="Report submitted."),
        ]
    )
    queued = _patch_main(monkeypatch, make_config(), model, ["The metal.", "Mercury-202."])

    exit_code = await main_module.main(["Tell me about mercury"])

    assert exit_code == 0
    assert queued == []
    out, _ = drain_stdout(capsys)
    assert "Metal or album?" in out
    assert "Which isotope?" in out

    results = _ask_user_results(model._received_messages[1])
    # Paired by tool_call_id, not merely present: a swapped decisions list fails here.
    assert {m.tool_call_id: m.content for m in results} == {
        "call_1": "The metal.",
        "call_2": "Mercury-202.",
    }
    # The equality above also proves the tool body never ran: `respond` skips execution, and the
    # body raises rather than returning a stand-in, so any execution would have failed the run.
    assert len(results) == 2


async def test_an_empty_answer_is_disclosed_rather_than_sent_as_silence(
    make_config, monkeypatch, scripted_model
):
    """A bare Enter must not reach the model as an empty tool result."""
    model = scripted_model(
        [
            AIMessage(content="", tool_calls=[_ask("Metal or album?")]),
            _submit_call("Final answer.").model_copy(
                update={
                    "usage_metadata": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                    }
                }
            ),
            AIMessage(content="Report submitted."),
        ]
    )
    _patch_main(monkeypatch, make_config(), model, ["   "])

    assert await main_module.main(["Tell me about mercury"]) == 0

    results = _ask_user_results(model._received_messages[1])
    assert [m.content for m in results] == [main_module._NO_ANSWER_GIVEN]


async def test_a_run_that_never_asks_completes_without_interruption(
    make_config, monkeypatch, scripted_model, capsys
):
    model = scripted_model(
        [
            _submit_call("Final answer.").model_copy(
                update={
                    "usage_metadata": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                    }
                }
            ),
            AIMessage(content="Report submitted."),
        ]
    )
    _patch_main(monkeypatch, make_config(), model, answers=None)

    exit_code = await main_module.main(["What is the capital of France?"])

    assert exit_code == 0
    _, lines = drain_stdout(capsys)
    assert lines
    assert lines[-1].strip().endswith(".md")


async def test_a_proposed_fetch_pages_call_does_not_interrupt(
    make_config, monkeypatch, scripted_model
):
    config = make_config()
    model = scripted_model(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "fetch_pages",
                        "args": {"urls": ["https://example.test/a"]},
                        "id": "f1",
                    }
                ],
            ),
            AIMessage(content="Final answer."),
        ]
    )
    patch_model(monkeypatch, model)

    async def _spy(urls, cfg, reg):
        return "", []

    monkeypatch.setattr("harness.tools.fetch._fetch", _spy)

    graph = build_agent(config, SourceRegistry())
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="research this")]},
        config={"configurable": {"thread_id": "test-thread-2"}},
    )

    assert "__interrupt__" not in result
    assert model._call_count == 2


def test_the_ask_user_tool_is_shaped_like_the_other_harness_tools(make_config):
    tool = build_ask_user_tool(make_config())

    assert tool.name == "ask_user"
    assert isinstance(tool.description, str)
    assert tool.description
    assert tool.response_format == "content_and_artifact"
    assert "question" in tool.args_schema.model_json_schema()["properties"]


# --- Phase 5: in-place overlay, the two clocks (risk #2) -----------------------------------


async def test_wall_clock_fires_while_a_question_is_pending(
    make_config, make_agent_settings, monkeypatch, scripted_model, tmp_path, capsys
):
    """The risk #2 test. The wall clock (`asyncio.timeout` in `Session.run`) must keep running
    while
    `ask_user` is awaiting an answer — nothing about answering a question may pause, reschedule,
    or extend it. `_read_answer` is patched with a fake that outlives the bound but never blocks
    the event loop (a plain `asyncio.sleep`), isolating exactly that property: the outer timeout
    scope around `_answer_questions` must still fire on schedule with the question pending
    inside it, not only after `_read_answer` eventually returns on its own.
    """
    agent = make_agent_settings(wall_clock_seconds=1)
    config = make_config(agent=agent)

    search_call = AIMessage(
        content="",
        tool_calls=[{"name": "search_web", "args": {"query": "widgets"}, "id": "call_search"}],
    )
    researcher_report = AIMessage(content="Researcher report (no citations yet).")
    ask = AIMessage(content="", tool_calls=[_ask("Narrower scope?", "call_ask")])
    # Distinct models per tier (D1): the lead's turn no longer waits on its researcher, so the
    # two tiers' calls interleave and one shared script would be consumed out of order.
    head_model = scripted_model([_dispatch_call("widgets"), ask])
    researcher_model = scripted_model([search_call, researcher_report])
    patch_run_by_role(monkeypatch, config, {"head": head_model, "researcher": researcher_model})

    async def _fast_search(request):
        import httpx

        return httpx.Response(200, json={"query": "widgets", "results": []})

    install_search_transport(monkeypatch, _fast_search)

    # Without this flag the test passes vacuously: if the scripted graph stops reaching the
    # `ask_user` interrupt, the wall clock still fires and `elapsed` is still small, while the
    # test no longer exercises "fired WHILE a question was pending" at all.
    asked: list[None] = []

    async def _slow_answer(*_args: object, **_kwargs: object) -> str:
        asked.append(None)
        await asyncio.sleep(3)
        return "Narrower."

    monkeypatch.setattr(main_module, "_read_answer", _slow_answer)

    started = time.monotonic()
    exit_code = await main_module.main(["Research widgets"])
    elapsed = time.monotonic() - started

    captured = capsys.readouterr()
    assert asked, "the run never reached the ask_user interrupt -- this test proved nothing"
    assert exit_code == 1
    assert "wall clock" in captured.err, captured.err
    # Well under the full 3s sleep: the timeout must have fired near the 1s bound WHILE the
    # question was pending, not merely after `_read_answer` returned on its own.
    assert elapsed < 2.5, f"run took {elapsed}s -- the wall clock did not fire while pending"


async def test_ctrl_c_while_the_overlay_is_open_restores_the_terminal(monkeypatch):
    """R6: an interrupt key while `_read_answer`'s TTY branch is reading must propagate as
    `KeyboardInterrupt` (so `main()`'s existing Ctrl+C teardown runs) and must restore the
    terminal on the way out, via `harness.input.restore_terminal` (step 1) rather than a raw
    mode left dangling on a parked daemon thread.
    """
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    # Stands in for real terminal input on the (should-be-unused, TTY branch) daemon thread of
    # the non-TTY fallback path, so this test cannot hang waiting on the real stdin if the
    # implementation has not yet branched on `isatty()`.
    def _unreachable_input() -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _unreachable_input)

    def _fake_read_keys():
        yield KeyEvent("interrupt")

    monkeypatch.setattr(main_module, "read_keys", _fake_read_keys)

    import harness.input as input_module

    restore_calls: list[None] = []
    monkeypatch.setattr(input_module, "_restore", lambda: restore_calls.append(None), raising=False)

    with pytest.raises(KeyboardInterrupt):
        await main_module._read_answer(PlainRenderer())

    assert restore_calls == [None], "the terminal restore did not run"


async def test_the_overlay_key_loop_leaves_the_event_loop_free(monkeypatch):
    """Risk #2's actual content, and the property no other test pins: the TTY key path must read
    keys OFF the loop thread.

    The wall-clock tripwire above patches `_read_answer` away wholesale, and the Ctrl+C test's
    fake key source yields immediately -- both stay GREEN against a rewrite that iterated
    `read_keys()` synchronously on the loop thread, which would stop `asyncio.timeout` from ever
    firing while a question is pending. That is the regression this test exists to catch.

    The fake key source BLOCKS without yielding, standing in for a human who has not typed yet.
    Read on a worker thread, the loop stays free and `wait_for` times out -- what we assert.
    Read on the loop thread, the loop is wedged, the timer cannot fire, and the coroutine instead
    runs to completion and returns an answer, so `pytest.raises` fails. The watchdog releases the
    block either way, so a blocking implementation fails the suite rather than hanging it.
    """
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    # Belt-and-braces, as in the Ctrl+C test: if the implementation ever stopped branching on
    # `isatty()`, this makes the non-TTY path fail loudly instead of parking on real stdin.
    def _unreachable_input() -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _unreachable_input)

    blocked = threading.Event()

    def _fake_read_keys():
        blocked.wait()
        yield KeyEvent("enter")

    monkeypatch.setattr(main_module, "read_keys", _fake_read_keys)

    watchdog = threading.Timer(2.0, blocked.set)
    watchdog.start()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(main_module._read_answer(PlainRenderer()), 0.2)
    finally:
        watchdog.cancel()
        blocked.set()


async def test_read_answer_non_tty_falls_back_to_the_input_bridge(monkeypatch):
    """Non-TTY runs (CI, piped/scripted invocations) must keep taking the `input()` bridge
    byte-for-byte, never the raw-mode key loop — so the eight existing `ask_user` tests above
    pass by design, not by accident of the test environment.
    """
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    input_calls: list[None] = []

    def _fake_input() -> str:
        input_calls.append(None)
        return "an answer"

    monkeypatch.setattr("builtins.input", _fake_input)

    read_keys_calls: list[None] = []

    def _fake_read_keys():
        read_keys_calls.append(None)
        yield KeyEvent("interrupt")

    monkeypatch.setattr(main_module, "read_keys", _fake_read_keys)

    answer = await main_module._read_answer(PlainRenderer())

    assert answer == "an answer"
    assert input_calls == [None]
    assert read_keys_calls == [], "the raw-mode key loop ran on a non-TTY path"
