"""Behavioral tests for the `ask_user` clarification tool and its interrupt/resume protocol.

Every test that drives a real graph does so via `build_agent` — nothing about deepagents
itself is mocked, only the model (`harness.agent.build_chat_model`, patched per the module
that imports it, never a network call), and, for the `__main__` tests, config loading,
`preflight`, and `_read_answer`.
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import harness.__main__ as main_module
from harness.agent import build_agent
from harness.sources import SourceRegistry
from harness.tools.ask_user import build_ask_user_tool

_THREAD = {"configurable": {"thread_id": "test-thread"}}


def _patch_model(monkeypatch, model):
    monkeypatch.setattr("harness.agent.build_chat_model", lambda config, role: model)


def _ask(question: str, call_id: str = "call_1") -> dict:
    """One `ask_user` tool call, in the shape a real model emits."""
    return {"name": "ask_user", "args": {"question": question}, "id": call_id}


def _patch_main(monkeypatch, config, model, answers=None):
    """Patch everything `main()` reaches outside the graph, and script the answers.

    `answers` is consumed in order by the patched `_read_answer`. Passing `None` means the
    test expects NO question: any call fails it. Returns the queue, so a test can assert
    every scripted answer was actually consumed.
    """
    monkeypatch.setattr(main_module, "load_config", lambda: config)

    async def _noop_preflight(cfg, role):
        return None

    monkeypatch.setattr(main_module, "preflight", _noop_preflight)
    _patch_model(monkeypatch, model)

    queued = list(answers or [])

    async def _fake_read_answer(prompt: str = "> ") -> str:
        if answers is None:
            raise AssertionError("_read_answer was called, but this run should never ask")
        if not queued:
            raise AssertionError("_read_answer was called more times than the test scripted")
        return queued.pop(0)

    monkeypatch.setattr(main_module, "_read_answer", _fake_read_answer)
    return queued


def _drain_stdout(capsys) -> tuple[str, list[str]]:
    """Return stdout and its non-empty lines. `readouterr` drains, so call this once."""
    out = capsys.readouterr().out
    return out, [line for line in out.splitlines() if line.strip()]


def _ask_user_results(request) -> list[ToolMessage]:
    """Every `ask_user` tool result in one recorded model request, in order."""
    return [m for m in request if isinstance(m, ToolMessage) and m.name == "ask_user"]


async def test_an_ask_user_call_interrupts_the_run_instead_of_completing(
    make_config, monkeypatch, scripted_model
):
    config = make_config()
    model = scripted_model([AIMessage(content="", tool_calls=[_ask("Metal or album?")])])
    _patch_model(monkeypatch, model)

    graph = build_agent(config, SourceRegistry())
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="Tell me about mercury")]}, config=_THREAD
    )

    assert "__interrupt__" in result
    action_request = result["__interrupt__"][0].value["action_requests"][0]
    assert action_request["name"] == "ask_user"
    assert action_request["args"]["question"] == "Metal or album?"

    # The run stopped at the interrupt: the script holds one reply, so a run that carried
    # on would have asked the model a second time and overrun the script.
    assert model._call_count == 1


async def test_the_question_reaches_stdout_and_the_answer_resumes_the_run(
    make_config, monkeypatch, scripted_model, capsys
):
    model = scripted_model(
        [
            AIMessage(content="", tool_calls=[_ask("Metal or album?")]),
            AIMessage(
                content="Final answer.",
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            ),
        ]
    )
    queued = _patch_main(monkeypatch, make_config(), model, ["I mean the chemical element."])

    exit_code = await main_module.main(["Tell me about mercury"])

    assert exit_code == 0
    assert queued == []
    out, lines = _drain_stdout(capsys)
    assert "Metal or album?" in out
    # The report path stays the last line of stdout (frozen — R1).
    assert lines[-1].strip().endswith(".md")

    results = _ask_user_results(model._received_messages[1])
    assert [m.content for m in results] == ["I mean the chemical element."]


async def test_a_second_clarification_round_asks_again_and_resumes_again(
    make_config, monkeypatch, scripted_model, capsys
):
    """The resume path is a LOOP, not a single `if` (3F Minor).

    Two sequential `ask_user` rounds: a one-shot resume would leave the second question
    unasked and unanswered, so both the stdout assertion and the second tool result fail.
    """
    model = scripted_model(
        [
            AIMessage(content="", tool_calls=[_ask("Metal or album?", "call_1")]),
            AIMessage(content="", tool_calls=[_ask("Which isotope?", "call_2")]),
            AIMessage(
                content="Final answer.",
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            ),
        ]
    )
    queued = _patch_main(monkeypatch, make_config(), model, ["The metal.", "Mercury-202."])

    exit_code = await main_module.main(["Tell me about mercury"])

    assert exit_code == 0
    assert queued == [], "the second round never asked"
    out, lines = _drain_stdout(capsys)
    assert "Metal or album?" in out
    assert "Which isotope?" in out
    assert lines[-1].strip().endswith(".md")

    # Three model calls: ask, ask again, answer. A one-round resume loop stops at two.
    assert model._call_count == 3
    # Both answers arrived, each as its own round's tool result: the third model call sees
    # the first answer and the second, in order.
    assert [m.content for m in _ask_user_results(model._received_messages[2])] == [
        "The metal.",
        "Mercury-202.",
    ]


async def test_two_questions_in_one_interrupt_get_one_answer_each(
    make_config, monkeypatch, scripted_model, capsys
):
    """One decision per action request, correctly paired.

    Two `ask_user` calls in a single `AIMessage` arrive as ONE interrupt carrying two action
    requests. Returning a short or mis-ordered decisions list raises `ValueError` inside the
    middleware, and executing the tool as well as responding would duplicate the results.
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
            AIMessage(
                content="Final answer.",
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            ),
        ]
    )
    queued = _patch_main(monkeypatch, make_config(), model, ["The metal.", "Mercury-202."])

    exit_code = await main_module.main(["Tell me about mercury"])

    assert exit_code == 0
    assert queued == []
    out, _ = _drain_stdout(capsys)
    assert "Metal or album?" in out
    assert "Which isotope?" in out

    results = _ask_user_results(model._received_messages[1])
    # Paired by tool_call_id, not merely present: a swapped decisions list fails here.
    assert {m.tool_call_id: m.content for m in results} == {
        "call_1": "The metal.",
        "call_2": "Mercury-202.",
    }
    # The tool body never ran alongside the human's answer — `respond` skips execution, so
    # its "no answer captured" fallback must appear nowhere.
    assert all("No answer was captured" not in str(m.content) for m in results)
    assert len(results) == 2


async def test_an_empty_answer_is_disclosed_rather_than_sent_as_silence(
    make_config, monkeypatch, scripted_model
):
    """A bare Enter must not reach the model as an empty tool result (3F Minor)."""
    model = scripted_model(
        [
            AIMessage(content="", tool_calls=[_ask("Metal or album?")]),
            AIMessage(
                content="Final answer.",
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            ),
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
            AIMessage(
                content="Final answer.",
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )
        ]
    )
    _patch_main(monkeypatch, make_config(), model, answers=None)

    exit_code = await main_module.main(["What is the capital of France?"])

    assert exit_code == 0
    _, lines = _drain_stdout(capsys)
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
    _patch_model(monkeypatch, model)

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
