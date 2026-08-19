"""Behavioral tests for the `ask_user` clarification tool and its interrupt/resume protocol.

Every test driving a real graph does so via `build_agent`: nothing about deepagents is mocked,
only the model and — for the `__main__` tests — config loading, `preflight` and `_read_answer`.
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import harness.__main__ as main_module
from harness.agent import build_agent
from harness.sources import SourceRegistry
from harness.tools.ask_user import build_ask_user_tool
from tests.conftest import drain_stdout, patch_model, patch_run

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

    async def _fake_read_answer(prompt: str = "> ") -> str:
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
    out, lines = drain_stdout(capsys)
    assert "Metal or album?" in out
    assert "Which isotope?" in out
    assert lines[-1].strip().endswith(".md")

    # Three model calls: ask, ask again, answer. A one-round resume loop stops at two.
    assert model._call_count == 3
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
