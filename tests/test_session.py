"""Behavioral tests for harness.session — the lead's dispatch/return/submit loop (D1/D2/D3).

Every test drives a real `Session` over a real deepagents-compiled lead graph: nothing about
deepagents is mocked, only the per-role models (and, in one test, `write_report`). The lead no
longer has `task` — it starts researchers through `dispatch_researcher`, receives each return
as its own `HumanMessage` turn, and ends research with `submit_report`.
"""

import asyncio
import time
from datetime import datetime
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.types import Interrupt
from pydantic import PrivateAttr, SecretStr

from harness.activity import ActivitySink
from harness.display import (
    AgentText,
    Alert,
    PlainRenderer,
    Question,
    ReportWritten,
    RunFinished,
    StageStarted,
    StageTracker,
)
from harness.runlog import RunLog
from harness.session import _SUBMIT_NOW, ResearcherReturn, Session, UserMessage
from harness.sources import SourceRegistry
from harness.tools.search import SearchUnavailableError
from tests.conftest import (
    ConcurrencyTrackingModel,
    RecordingRenderer,
    ScriptedChatModel,
    _dispatch_call,
    _FakeMarkdown,
    _FakeResult,
    _lead_model,
    _submit_call,
    approve_all,
)

# Long enough that a wedged test fails loudly instead of hanging the suite, short enough that a
# genuine failure is reported in seconds.
_WAIT_TIMEOUT = 10.0


async def _wait_for(predicate, description: str) -> None:
    """Spin until `predicate()` is true, or fail the test naming what never happened."""
    deadline = asyncio.get_running_loop().time() + _WAIT_TIMEOUT
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for: {description}")
        await asyncio.sleep(0.01)


def _typed(answer: str) -> Any:
    """An `answer_source` handing back one scripted line, as the composer or stdin would."""

    async def _answer() -> str:
        return answer

    return _answer


def _model(cls: type, name: str) -> Any:
    """A scripted model of `cls` bound to a throwaway (never-dialed) endpoint."""
    return cls(model=name, base_url="https://example.test/v1", api_key=SecretStr("x"))


class _GatedChatModel(ScriptedChatModel):
    """A `ScriptedChatModel` whose Nth call waits on `_gates[N]` before replying.

    The head-model half of "a return that arrives while a lead turn is in flight is delivered
    in the FOLLOWING turn": without a way to hold the lead inside a model call, the two
    researcher returns race and the test cannot tell batching from ordering.
    """

    _gates: dict[int, Any] = PrivateAttr(default_factory=dict)
    # Call indices that have ENTERED the model, which `_call_count` cannot say: a gated call
    # has started but not finished, and "the lead's next turn is in flight" is precisely that
    # window.
    _entered: list[int] = PrivateAttr(default_factory=list)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        index = self._call_count
        self._entered.append(index)
        gate = self._gates.get(index)
        if gate is not None:
            await gate.wait()
        return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)


class _KeyedResearcherModel(ScriptedChatModel):
    """Replies per researcher, keyed by a marker in that researcher's own brief.

    One compiled researcher graph (and so ONE researcher model) serves every dispatch, and the
    dispatches run concurrently — a positional script would hand researcher/1's reply to
    whichever task happened to reach the model first. Keying on the brief makes each
    researcher's reply, and its gate, deterministic.
    """

    _plans: dict[str, Any] = PrivateAttr(default_factory=dict)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self._received_messages.append(list(messages))
        text = " ".join(str(message.content) for message in messages)
        for key, (gate, reply) in self._plans.items():
            if key in text:
                if gate is not None:
                    await gate.wait()
                self._call_count += 1
                # An exception as the "reply" scripts a researcher that CRASHES, which the
                # roster must show as `failed` rather than `done` -- the same keying, so a
                # crashing and a reporting researcher can run side by side deterministically.
                if isinstance(reply, Exception):
                    raise reply
                return ChatResult(
                    generations=[ChatGeneration(message=AIMessage(content=reply, id=f"res-{key}"))]
                )
        raise AssertionError(f"no scripted researcher reply matched: {text!r}")


class _FailingChatModel(ScriptedChatModel):
    """A `ScriptedChatModel` whose Nth call raises, standing in for a provider outage.

    `_failures` is keyed by call index and consulted BEFORE the script, so a failing call
    consumes its index (and records its messages, like every other call) without reaching
    `_script`. Success calls delegate to the base class untouched.
    """

    _failures: dict[int, Exception] = PrivateAttr(default_factory=dict)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        failure = self._failures.get(self._call_count)
        if failure is not None:
            self._received_messages.append(list(messages))
            self._call_count += 1
            raise failure
        return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)


@pytest.fixture
def make_session(make_config, make_agent_settings):
    """Build a `Session` from exactly what `__main__.main` holds when it hands off today."""

    def _make(question: str = "Which widget is cheapest?", **config_overrides: Any) -> Session:
        registry = config_overrides.pop("registry", None) or SourceRegistry(run_id="test-run")
        # Overridable like `registry`/`config`: a test asserting on incidents from a FAILED run
        # has no `RunOutcome` to read them off, so it passes in the log it will inspect.
        run_log = config_overrides.pop("run_log", None) or RunLog()
        # Overridable for the same reason as `run_log`: the researcher roster (R8) lives in the
        # sink, so a test asserting on it needs the very sink the session was handed.
        sink = config_overrides.pop("sink", None)
        # Popped like `sink`: `interactive` is a `Session` argument, not an `AgentSettings`
        # field, so it must never reach `make_agent_settings`. False by default, which is what
        # every headless (non-TTY) run passes and what every pre-Phase-3 test assumes.
        interactive = config_overrides.pop("interactive", False)
        # Overridable like `sink`: a test asserting on what the run DISPLAYED needs the very
        # renderer the session emits into, not printed text it has to parse back. Popped
        # BEFORE the config is built, since everything left over is an `AgentSettings` field.
        renderer = config_overrides.pop("renderer", None) or PlainRenderer()
        # Popped like `renderer`, and for the same reason: the one thing `main` owns about a
        # clarifying question is where the answer TEXT comes from (the composer, or the stdin
        # bridge). The overlay, the pause, URL approval and digit resolution are `Session`'s.
        answer_source = config_overrides.pop("answer_source", None)
        config = config_overrides.pop("config", None) or make_config(
            agent=make_agent_settings(**config_overrides)
        )

        async def _never_asked() -> str:
            raise AssertionError("no clarifying question was expected in this test")

        return Session(
            config,
            registry,
            run_log,
            renderer,
            StageTracker(renderer),
            question,
            sink=sink,
            # `None`, not an unstarted `BrowserSession`: `main` owns that lifecycle and the
            # session only forwards it, so a test that fetches drives `install_crawler`'s
            # browser-free seam exactly as the pre-session fetch tests did.
            browser=None,
            answer_source=answer_source or _never_asked,
            started_at=datetime.now(),
            interactive=interactive,
        )

    return _make


@pytest.fixture
def three_models(patch_models_by_role):
    """Install a head/researcher/reader trio and return them."""

    def _install(head: Any, researcher: Any, reader: Any | None = None) -> None:
        patch_models_by_role(
            {
                "head": head,
                "researcher": researcher,
                "reader": reader or _model(ScriptedChatModel, "reader-test").script([]),
                "verifier": _model(ScriptedChatModel, "verifier-test").script([]),
            }
        )

    return _install


async def test_two_dispatches_in_one_turn_end_the_turn_immediately(make_session, three_models):
    """D1/D2: `dispatch_researcher` returns AT ONCE — the lead's turn ends with two "started"
    results and two researcher tasks still running, instead of blocking on them the way `task`
    did.
    """
    gate_a, gate_b = asyncio.Event(), asyncio.Event()
    head = _model(ScriptedChatModel, "head-test").script(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _dispatch_call("a", "call_a").tool_calls[0],
                    _dispatch_call("b", "call_b").tool_calls[0],
                ],
            ),
            AIMessage(content="Both angles are running."),
            _submit_call("Widgets cost $4.20 each."),
            AIMessage(content="Report submitted."),
        ]
    )
    researcher = _model(_KeyedResearcherModel, "researcher-test")
    researcher._plans = {
        "Investigate a": (gate_a, "Angle a findings."),
        "Investigate b": (gate_b, "Angle b findings."),
    }
    three_models(head, researcher)

    session = make_session()
    run = asyncio.create_task(session.run())
    try:
        await _wait_for(
            lambda: len(session.running) == 2 and head._call_count >= 2,
            "two researchers running after the lead's first turn",
        )

        # The turn ended while both researchers are still blocked on their gates: this is the
        # whole point of D1 — `task` could not have returned here.
        assert not gate_a.is_set() and not gate_b.is_set()
        assert sorted(session.running) == ["researcher/1", "researcher/2"]

        state = await session.agent.aget_state({"configurable": {"thread_id": session.thread_id}})
        # Keyed by tool_call_id, not by position: langchain gathers the two calls in one node
        # and the ToolMessages come back in completion order, which is not the contract — WHICH
        # researcher each call started is.
        results = {
            message.tool_call_id: str(message.content)
            for message in state.values["messages"]
            if isinstance(message, ToolMessage) and message.name == "dispatch_researcher"
        }
        assert results == {
            "call_a": "researcher/1 (a) started",
            "call_b": "researcher/2 (b) started",
        }
    finally:
        gate_a.set()
        gate_b.set()
        await run


async def test_each_return_is_its_own_lead_turn_carrying_the_roster(make_session, three_models):
    """D2/R2: one `ResearcherReturn` becomes one `HumanMessage` matching the contract, and a
    return that lands while a lead turn is in flight waits for the FOLLOWING turn rather than
    being appended to the running one.
    """
    gate_a, gate_b, head_gate = asyncio.Event(), asyncio.Event(), asyncio.Event()
    head = _model(_GatedChatModel, "head-test").script(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _dispatch_call("a", "call_a").tool_calls[0],
                    _dispatch_call("b", "call_b").tool_calls[0],
                ],
            ),
            AIMessage(content="Both angles are running."),
            AIMessage(content="Noted the first angle."),
            _submit_call("Widgets cost $4.20 each."),
            AIMessage(content="Report submitted."),
        ]
    )
    head._gates = {2: head_gate}  # the turn that narrates researcher/1's return
    researcher = _model(_KeyedResearcherModel, "researcher-test")
    researcher._plans = {
        "Investigate a": (gate_a, "Angle a findings.\nSecond line of a."),
        "Investigate b": (gate_b, "Angle b findings."),
    }
    three_models(head, researcher)

    sink = ActivitySink()
    session = make_session(sink=sink)
    run = asyncio.create_task(session.run())
    try:
        await _wait_for(lambda: len(session.running) == 2, "both researchers dispatched")

        gate_a.set()
        await _wait_for(lambda: len(head._entered) == 3, "the lead's return turn to start")
        # The lead is now held inside its third model call; release researcher 2 into the gap.
        gate_b.set()
        await _wait_for(
            lambda: [state.status for state in sink.researchers()] == ["done", "done"],
            "both returns",
        )
        head_gate.set()

        await _wait_for(lambda: head._call_count >= 4, "the second return's own lead turn")
    finally:
        gate_a.set()
        gate_b.set()
        head_gate.set()
        await run

    # Call 3's input carries researcher/1's return ALONE, ending with the roster line.
    first_return = head._received_messages[2][-1]
    assert isinstance(first_return, HumanMessage)
    lines = str(first_return.content).splitlines()
    assert lines[0] == "[researcher/1 — a] returned:"
    assert lines[1:3] == ["Angle a findings.", "Second line of a."]
    assert lines[-1] == "Roster: done researcher/1 · running researcher/2"
    assert "researcher/2" not in "\n".join(lines[:-1])

    # Call 4's input is researcher/2's return — the FOLLOWING turn, not the one in flight.
    second_return = head._received_messages[3][-1]
    assert isinstance(second_return, HumanMessage)
    second_lines = str(second_return.content).splitlines()
    assert second_lines[0] == "[researcher/2 — b] returned:"
    assert second_lines[1] == "Angle b findings."
    assert second_lines[-1] == "Roster: done researcher/1, researcher/2 · running none"


async def test_submit_report_ends_research_and_carries_the_answer_into_the_report(
    make_session, three_models, monkeypatch
):
    """D3: `RunOutcome.answer` is the `submit_report` ARGUMENT, not the last `AIMessage`, and
    `run()` hands back the very outcome that was written.
    """
    answer = "Acme widgets are cheapest at $4.20 per unit."
    head = _model(ScriptedChatModel, "head-test").script(
        [_submit_call(answer), AIMessage(content="Chatter after the report, not the answer.")]
    )
    three_models(head, _model(ScriptedChatModel, "researcher-test").script([]))

    written: list[Any] = []

    def _capture(outcome: Any, config: Any):
        written.append(outcome)
        return None

    monkeypatch.setattr("harness.session.write_report", _capture)

    session = make_session()
    outcome = await session.run()

    assert len(written) == 1
    assert written[0].answer == answer
    assert outcome is written[0]
    assert session.answer == answer


async def test_a_lead_that_never_submits_writes_no_report(make_session, three_models, monkeypatch):
    """D3/R5: no `submit_report` means no report at all — the run fails rather than salvaging
    whatever prose came last.

    Pinned to `interactive=False` (Phase 3): the nudge-then-fail path is the HEADLESS one. An
    interactive session has a composer to wait on instead, so it never nudges.
    """
    head = _model(ScriptedChatModel, "head-test").script(
        [AIMessage(content="I think that covers it."), AIMessage(content="Still nothing to add.")]
    )
    three_models(head, _model(ScriptedChatModel, "researcher-test").script([]))

    written: list[Any] = []
    monkeypatch.setattr("harness.session.write_report", lambda outcome, config: written.append(1))

    session = make_session(interactive=False)
    outcome = await session.run()

    assert outcome is None
    assert written == []
    # The nudge fired exactly once: two lead turns, not an unbounded retry loop.
    assert head._call_count == 2


async def test_dispatch_refuses_at_the_cap_and_once_research_is_closed(make_session):
    """D1's cap refusal and D3's closed refusal are contract strings, and neither starts a
    researcher. The cap itself is `[agent] max_researchers`, read from config rather than
    baked into the module, so a run can be tightened without a code change.
    """
    session = make_session(max_researchers=2)
    parked = asyncio.Event()
    for index in range(2):
        session.running[f"researcher/{index + 1}"] = asyncio.create_task(parked.wait())
    try:
        assert (
            session.dispatch("e", "objective", "format", "boundaries")
            == "refused: 2 researchers already running — wait for a return"
        )
        assert len(session.running) == 2
    finally:
        parked.set()
        await asyncio.gather(*session.running.values())

    # An empty roster, because `submit` itself refuses a non-empty one (3F F2) — the closed
    # refusal is reachable only from the state a real submit leaves behind.
    session.running.clear()
    assert session.submit("the final answer") == "report accepted — research is closed"
    assert (
        session.dispatch("e", "objective", "format", "boundaries")
        == "refused: research is closed — the report is written"
    )
    assert session.running == {}


async def test_the_compiled_researcher_graph_keeps_the_nested_reader_and_its_tools(
    make_config, three_models, make_agent_settings
):
    """Risk #1: compiling the researcher standalone with `create_deep_agent` must reproduce
    what `SubAgentMiddleware` built for it — the nested reader tier, the researcher's own
    `task`/`search_web`/`fetch_raw` toolset with `fetch_pages` reachable ONLY from the reader,
    and `task`'s own result contract (the last non-empty `AIMessage` text).
    """
    from harness.agent import build_researcher_graph
    from harness.session import _final_answer

    researcher = _model(ScriptedChatModel, "researcher-test").script(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "description": "Read https://a.test",
                            "subagent_type": "reader",
                        },
                        "id": "call_reader",
                    }
                ],
            ),
            AIMessage(content="Researcher report: the page confirms the defect [S1]."),
        ]
    )
    reader = _model(ScriptedChatModel, "reader-test").script(
        [AIMessage(content="Digest: confirmed defect pattern [S1].")]
    )
    three_models(_model(ScriptedChatModel, "head-test").script([]), researcher, reader)

    config = make_config(agent=make_agent_settings())
    graph = build_researcher_graph(config, SourceRegistry(run_id="test-run"))
    state = await graph.ainvoke({"messages": [HumanMessage(content="Objective: an angle")]})

    researcher_tools = {name for bind in researcher._bound_tool_names for name in bind}
    assert {"task", "search_web", "fetch_raw"} <= researcher_tools
    assert "fetch_pages" not in researcher_tools

    reader_tools = {name for bind in reader._bound_tool_names for name in bind}
    assert "fetch_pages" in reader_tools
    assert "task" not in reader_tools
    assert "search_web" not in reader_tools

    assert (
        _final_answer(state["messages"]) == "Researcher report: the page confirms the defect [S1]."
    )


class _RaisingChatModel(ScriptedChatModel):
    """Raises on every call, recording each invocation — drives the researcher retry path."""

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self._received_messages.append(list(messages))
        self._call_count += 1
        raise RuntimeError("boom")


async def test_a_researcher_crash_reaches_the_lead_as_findings_after_one_retry(
    make_session, three_models
):
    """The `task` tool's failure policy, reproduced where the researcher now runs (D1): a crash
    is retried exactly once, then reaches the lead as `RESEARCHER FAILED (...)` findings rather
    than killing the run, and the session records the incident.

    Moved from `tests/test_agent.py` when the lead lost `task`: the retry/error pair used to be
    `_task_dispatch_guard`'s on the lead tier, and is `Session._run_researcher`'s now.
    """
    head = _model(ScriptedChatModel, "head-test").script(
        [
            _dispatch_call("an angle"),
            AIMessage(content="Dispatched."),
            _submit_call("Answered despite the failed angle."),
            AIMessage(content="Report submitted."),
        ]
    )
    researcher = _model(_RaisingChatModel, "researcher-test")
    three_models(head, researcher)

    session = make_session()
    outcome = await session.run()

    assert researcher._call_count == 2  # the initial attempt plus exactly one retry
    findings = head._received_messages[2][-1]
    assert str(findings.content).startswith("[researcher/1 — an angle] returned:\n")
    assert "RESEARCHER FAILED (RuntimeError): boom" in str(findings.content)
    assert outcome is not None
    assert outcome.answer == "Answered despite the failed angle."
    assert [incident.kind for incident in outcome.incidents] == ["subagent_failed"]


async def test_lead_to_researcher_to_reader_digest_reaches_the_lead(
    make_session, three_models, install_crawler, make_config, make_agent_settings
):
    """The full 3-tier chain, scripted end to end — the lead dispatches a researcher, the
    researcher dispatches a reader, the reader's digest reaches the researcher, the
    researcher's own report reaches the LEAD as a return message, and the digested source is
    marked `digested` (R7's mechanism unbroken by the dispatch rewrite).

    Moved from `tests/test_agent.py`: the chain now starts at `dispatch_researcher`, so it can
    only be driven through a `Session`.
    """
    head = _model(ScriptedChatModel, "head-test").script(
        [
            _dispatch_call("the defect angle"),
            AIMessage(content="Dispatched."),
            _submit_call("Final answer citing the researcher's finding [S1]."),
            AIMessage(content="Report submitted."),
        ]
    )
    researcher = _model(ScriptedChatModel, "researcher-test").script(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "description": "Fetch and digest https://a.test",
                            "subagent_type": "reader",
                        },
                        "id": "call_reader",
                    }
                ],
            ),
            AIMessage(content="Researcher report: the page confirms the defect [S1]."),
        ]
    )
    reader = _model(ScriptedChatModel, "reader-test").script(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "fetch_pages", "args": {"urls": ["https://a.test"]}, "id": "call_f"}
                ],
            ),
            AIMessage(content="Digest: confirmed defect pattern [S1]."),
        ]
    )
    three_models(head, researcher, reader)
    install_crawler(
        [
            _FakeResult(
                "https://a.test",
                markdown=_FakeMarkdown(raw_markdown="Defect body", fit_markdown="Defect body"),
            )
        ]
    )

    registry = SourceRegistry(run_id="test-run")
    # Strict provenance (R2): this scenario never calls `search_web`, so the reader's directly
    # fetched URL must arrive pre-approved.
    approve_all(registry, ["https://a.test"])
    session = make_session(config=make_config(agent=make_agent_settings()), registry=registry)

    outcome = await session.run()

    assert outcome is not None
    assert outcome.answer == "Final answer citing the researcher's finding [S1]."
    returned = str(head._received_messages[2][-1].content)
    assert "Researcher report: the page confirms the defect [S1]." in returned
    source = registry.get("S1")
    assert source is not None
    assert source.read_mode == "digested"


async def test_two_researchers_dispatched_in_one_turn_run_concurrently(make_session, three_models):
    """Acceptance criterion: two researchers dispatched by ONE lead turn (a single `AIMessage`
    carrying two `dispatch_researcher` calls) actually run concurrently — peak in-flight
    researcher model calls > 1.
    """
    head = _model(ScriptedChatModel, "head-test").script(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _dispatch_call("a", "call_a").tool_calls[0],
                    _dispatch_call("b", "call_b").tool_calls[0],
                ],
            ),
            AIMessage(content="Both angles are running."),
            _submit_call("Both angles reported."),
            AIMessage(content="Report submitted."),
        ]
    )
    researcher = _model(ConcurrencyTrackingModel, "researcher-test").script(
        [AIMessage(content="Report A."), AIMessage(content="Report B.")]
    )
    # A real, non-zero yield — see `ConcurrencyTrackingModel` on why proving CONCURRENCY needs it.
    researcher._sleep_seconds = 0.05
    three_models(head, researcher)

    session = make_session()
    outcome = await session.run()

    assert outcome is not None
    assert researcher._peak_in_flight > 1
    assert researcher._call_count == 2


def test_researcher_return_is_the_frozen_event_shape():
    """The Phase 1 contract Phase 2's roster and Phase 5's TUI both read from."""
    event = ResearcherReturn(id="researcher/1", label="a", findings="text", elapsed_s=1.5)
    assert (event.id, event.label, event.findings, event.elapsed_s) == (
        "researcher/1",
        "a",
        "text",
        1.5,
    )


async def test_a_cap_reached_run_that_never_submits_names_the_bound_it_died_on(
    make_session, three_models
):
    """3F F1: a cut-short run with no `submit_report` writes no report, so `cut_short_detail` is
    the ONLY thing `__main__` has to print — it must name the bound, never be `None`.
    """
    gate = asyncio.Event()
    head = _model(ScriptedChatModel, "head-test").script(
        [
            _dispatch_call("an angle", "call_a"),
            # The synthesis pass's turn: narration, not a `submit_report` call.
            AIMessage(content="In summary, widgets are cheap."),
            AIMessage(content="Nothing more to add."),
        ]
    )
    researcher = _model(_KeyedResearcherModel, "researcher-test")
    researcher._plans = {"Investigate an angle": (gate, "Angle findings.")}
    three_models(head, researcher)

    session = make_session(max_rounds=1)
    try:
        outcome = await session.run()
    finally:
        gate.set()

    assert outcome is None
    assert session.cut_short == "round_cap"
    assert session.cut_short_detail is not None
    assert "submit_report" in session.cut_short_detail


async def test_submit_is_refused_while_researchers_are_still_running(make_session):
    """3F F2(a): `submit_report` in the NORMAL flow is refused while the roster is non-empty —
    an early submit would otherwise cancel live researchers and drop their angles silently.
    """
    session = make_session()
    parked = asyncio.Event()
    for index in (1, 2):
        session.running[f"researcher/{index}"] = asyncio.create_task(parked.wait())
    try:
        assert (
            session.submit("too early")
            == "refused: 2 researchers still running — wait for their returns"
        )
        assert session.answer is None
        assert all(not task.done() for task in session.running.values())
    finally:
        parked.set()
        await asyncio.gather(*session.running.values())


async def test_submit_during_a_forced_synthesis_pass_is_accepted_and_discloses_the_cancellation(
    make_session, three_models
):
    """3F F2(a)/(b): the bounded synthesis pass MUST be able to produce a report, so the running
    roster does not refuse it there — and each researcher cancelled by it is disclosed as its own
    incident rather than vanishing from the report.
    """
    gate = asyncio.Event()
    head = _model(ScriptedChatModel, "head-test").script(
        [
            _dispatch_call("an angle", "call_a"),
            _submit_call("Answered from what already came back."),
            AIMessage(content="Report submitted."),
        ]
    )
    researcher = _model(_KeyedResearcherModel, "researcher-test")
    researcher._plans = {"Investigate an angle": (gate, "Angle findings.")}
    three_models(head, researcher)

    session = make_session(max_rounds=1)
    try:
        outcome = await session.run()
    finally:
        gate.set()

    assert outcome is not None
    assert outcome.answer == "Answered from what already came back."
    assert session.running == {}
    cancelled = [
        incident for incident in outcome.incidents if incident.kind == "researcher_cancelled"
    ]
    assert len(cancelled) == 1
    assert "researcher/1" in cancelled[0].detail
    assert "an angle" in cancelled[0].detail


class _SearchAbortModel(ScriptedChatModel):
    """Raises the run's own abort condition, not a researcher fault — the F3 pass-through class."""

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self._received_messages.append(list(messages))
        self._call_count += 1
        raise SearchUnavailableError("SearXNG failed 3 times in a row")


async def test_a_search_abort_fails_the_run_without_blaming_the_researcher(
    make_session, three_models
):
    """3F F3: `SearchUnavailableError` is the RUN's abort condition (the documented
    three-consecutive-failures invariant), so it is neither retried nor recorded as a
    `subagent_failed` incident — the run just fails and writes nothing.
    """
    head = _model(ScriptedChatModel, "head-test").script(
        [
            _dispatch_call("an angle", "call_a"),
            AIMessage(content="Dispatched; waiting."),
            AIMessage(content="Still waiting."),
        ]
    )
    researcher = _model(_SearchAbortModel, "researcher-test")
    three_models(head, researcher)

    run_log = RunLog()
    session = make_session(run_log=run_log)
    outcome = await session.run()

    assert outcome is None
    assert session.cut_short == "error"
    assert "SearchUnavailableError" in (session.cut_short_detail or "")
    # No retry: a pass-through failure burns a whole researcher re-run for nothing.
    assert researcher._call_count == 1
    assert [incident.kind for incident in run_log.incidents()] == []


# --- Phase 2: budgets, roster data and run exits ------------------------------------------


async def test_the_wall_clock_arms_on_a_successful_dispatch_and_not_on_a_refusal(
    make_session, three_models
):
    """R6: the clock measures RESEARCH, so a dispatch the harness refused must leave it
    disarmed — only a researcher that actually started may begin the countdown.

    A regression pin on Phase 1's behaviour as much as a Phase 2 test: `_arm_clock` is called
    after both refusal branches have already returned, and nothing else says so.
    """
    gate_b = asyncio.Event()
    head = _model(ScriptedChatModel, "head-test").script(
        [
            _dispatch_call("pricing", "call_p"),
            AIMessage(content="Nothing started — the roster is full."),
            _dispatch_call("supply", "call_s"),
            AIMessage(content="The supply angle is running."),
            _submit_call("Widgets cost $4.20 each."),
            AIMessage(content="Report submitted."),
        ]
    )
    researcher = _model(_KeyedResearcherModel, "researcher-test")
    researcher._plans = {"Investigate supply": (gate_b, "Supply findings.")}
    three_models(head, researcher)

    session = make_session(max_researchers=2)
    parked = asyncio.Event()
    # The cap is already full when the run starts, so the lead's FIRST dispatch is refused.
    for index in (0, 1):
        session.running[f"parked/{index}"] = asyncio.create_task(parked.wait())

    run = asyncio.create_task(session.run())
    try:
        await _wait_for(lambda: head._call_count >= 2, "the refused dispatch's turn to end")
        assert session.clock_armed is False

        # Free one slot and wake the loop; this dispatch is accepted, and arms the clock.
        session.running.pop("parked/1")
        session.events.put_nowait(UserMessage("try that angle again"))
        await _wait_for(lambda: head._call_count >= 4, "the accepted dispatch's turn to end")
        assert session.clock_armed is True
    finally:
        session.running.pop("parked/0", None)
        parked.set()
        gate_b.set()
        await run


async def test_submit_report_disarms_the_clock_so_later_time_never_cuts_the_run_short(
    make_session, three_models
):
    """R6/D3: the clock spans first question → report written, and stops there. A slow turn
    AFTER the answer is submitted runs past the wall clock without cutting the run short — the
    report is kept and `cut_short` stays None.
    """
    answer = "Acme widgets are cheapest at $4.20 per unit."
    # Call 3 is the reply AFTER `submit_report`; 1.5s against a 1s clock means an armed clock
    # would certainly have fired inside it.
    head = _lead_model(
        replies=[
            _dispatch_call("pricing", "call_p"),
            AIMessage(content="Dispatched."),
            _submit_call(answer),
        ],
        answer=answer,
        slow_calls=frozenset({3}),
        delay_seconds=1.5,
    )
    researcher = _model(ScriptedChatModel, "researcher-test").script(
        [AIMessage(content="Pricing findings.")]
    )
    three_models(head, researcher)

    session = make_session(wall_clock_seconds=1)
    started = time.monotonic()
    run = asyncio.create_task(session.run())
    await _wait_for(lambda: session.clock_armed, "the clock to arm at the dispatch")
    outcome = await run
    elapsed = time.monotonic() - started

    assert outcome is not None
    assert outcome.answer == answer
    assert session.cut_short is None
    assert session.clock_armed is False
    # The deadline really did pass: without it, a disarm that never happened would still pass.
    assert elapsed > 1.0, f"the run finished in {elapsed}s — the 1s clock never came due"


async def test_ctrl_c_mid_run_writes_no_report_and_cancels_every_running_researcher(
    make_session, three_models, monkeypatch
):
    """R5: a quit before a report exists is a FAILED run — nothing written, `run()` returns
    None, and every researcher still in flight is cancelled AND awaited (Risk #2), each
    disclosed as its own incident. `test_agent.py` pins the exit code through `main()`; this
    pins the session that decides it.

    Delivered by cancelling the run task, not by raising `KeyboardInterrupt` inside a scripted
    model: a `KeyboardInterrupt` raised inside a langgraph node's own `asyncio.Task` is
    re-raised straight out of the event loop by `Task.__step` and never reaches the awaiting
    session at all. A real Ctrl+C has been observed to surface here as `CancelledError` (see
    `Session.run`'s abort clause), which is exactly what this delivers.
    """
    supply_gate, routing_gate = asyncio.Event(), asyncio.Event()
    head = _model(ScriptedChatModel, "head-test").script(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _dispatch_call("pricing", "call_p").tool_calls[0],
                    _dispatch_call("supply", "call_s").tool_calls[0],
                    _dispatch_call("routing", "call_r").tool_calls[0],
                ],
            ),
            AIMessage(content="All three angles are running."),
            AIMessage(content="Noted the pricing angle."),
        ]
    )
    researcher = _model(_KeyedResearcherModel, "researcher-test")
    researcher._plans = {
        "Investigate pricing": (None, "Pricing findings."),
        "Investigate supply": (supply_gate, "Supply findings."),
        "Investigate routing": (routing_gate, "Routing findings."),
    }
    three_models(head, researcher)

    written: list[Any] = []
    monkeypatch.setattr("harness.session.write_report", lambda outcome, config: written.append(1))

    sink = ActivitySink()
    run_log = RunLog()
    session = make_session(run_log=run_log, sink=sink)
    run = asyncio.create_task(session.run())
    try:
        await _wait_for(
            lambda: head._call_count >= 3 and len(session.running) == 2,
            "the first return narrated with two researchers still in flight",
        )
        # One turn of the loop, so the abort lands on the wait between turns rather than
        # mid-stream — where a Ctrl+C at a terminal spends nearly all of its time.
        await asyncio.sleep(0.05)
        run.cancel()
        outcome = await run
    finally:
        supply_gate.set()
        routing_gate.set()

    assert outcome is None
    assert written == []
    assert session.cut_short == "error"
    assert session.cut_short_detail == "user abort (Ctrl+C)"
    # Cancelled AND awaited: nothing is left running behind the failed run.
    assert session.running == {}
    cancelled = [
        incident for incident in run_log.incidents() if incident.kind == "researcher_cancelled"
    ]
    assert len(cancelled) == 2
    assert {incident.detail.split()[0] for incident in cancelled} == {
        "researcher/2",
        "researcher/3",
    }
    # A cancelled researcher never returned, so the roster shows it as failed, not done.
    assert [state.status for state in sink.researchers()] == ["done", "failed", "failed"]


async def test_the_roster_tracks_every_researcher_and_feeds_the_leads_roster_line(
    make_session, three_models
):
    """R2/R8: the sink is the roster's one source of truth — one researcher reporting, one
    crashing and one still running are three rows with the right ids, labels and times, and the
    `Roster:` line the next lead turn reads is built from exactly those rows.
    """
    supply_gate, routing_gate = asyncio.Event(), asyncio.Event()
    head = _model(ScriptedChatModel, "head-test").script(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _dispatch_call("pricing", "call_p").tool_calls[0],
                    _dispatch_call("supply", "call_s").tool_calls[0],
                    _dispatch_call("routing", "call_r").tool_calls[0],
                ],
            ),
            AIMessage(content="All three angles are running."),
            AIMessage(content="Noted the pricing angle."),
            AIMessage(content="Noted the supply angle."),
            _submit_call("Widgets cost $4.20 each."),
            AIMessage(content="Report submitted."),
        ]
    )
    researcher = _model(_KeyedResearcherModel, "researcher-test")
    researcher._plans = {
        "Investigate pricing": (None, "Pricing findings."),
        "Investigate supply": (supply_gate, RuntimeError("boom")),
        "Investigate routing": (routing_gate, "Routing findings."),
    }
    three_models(head, researcher)

    sink = ActivitySink()
    session = make_session(sink=sink)
    run = asyncio.create_task(session.run())
    try:
        await _wait_for(lambda: head._call_count >= 3, "the pricing return's own lead turn")
        supply_gate.set()
        await _wait_for(lambda: head._call_count >= 4, "the crashed supply return's lead turn")

        roster = sink.researchers()
        assert [(state.id, state.label, state.status) for state in roster] == [
            ("researcher/1", "pricing", "done"),
            ("researcher/2", "supply", "failed"),
            ("researcher/3", "routing", "running"),
        ]
        # Every row is stamped at dispatch; only the two that finished carry a finish time.
        assert [state.finished_at is None for state in roster] == [False, False, True]
        assert all(state.started_at <= (state.finished_at or state.started_at) for state in roster)
    finally:
        supply_gate.set()
        routing_gate.set()
        await run

    # The line the lead actually read: a FAILED researcher is done, not still running.
    supply_turn = head._received_messages[3][-1]
    assert isinstance(supply_turn, HumanMessage)
    assert str(supply_turn.content).splitlines()[-1] == (
        "Roster: done researcher/1, researcher/2 · running researcher/3"
    )


async def test_the_synthesis_margin_never_fires_after_submit_report(make_session):
    """Phase 2 3F Major: `submit_report` closes research for the reserve as well as the hard
    clock. A slow closing reply that crosses the margin must not stamp `synthesis_margin` onto a
    complete report, and a second submit must not overwrite the accepted answer.
    """
    session = make_session(wall_clock_seconds=100, synthesis_margin_seconds=50)
    assert session.submit("Final answer.") == "report accepted — research is closed"
    # Pretend research started deep inside the margin window, then stream one more update.
    session._research_started_at = asyncio.get_running_loop().time() - 90
    session._handle_node_update({"messages": [AIMessage(content="Closing remarks.")]})
    assert session.cut_short is None
    assert session._margin_hit is False
    assert session.submit("Overwrite.") == "refused: research is closed — the report is written"
    assert session.answer == "Final answer."


# --- Phase 3: the composer's queued messages and post-report chat --------------------------


async def test_a_message_typed_during_a_turn_lands_in_the_next_turn_after_the_returns(
    make_session, three_models, capsys
):
    """R1/R2: text typed while a lead turn is in flight is never lost and never interrupts the
    model call — it drains into the FOLLOWING turn, after any returns that arrived before it,
    inside the one `HumanMessage` that batch becomes.

    Two head gates hold the lead inside a turn on demand, which is what makes "typed DURING a
    turn" a deterministic state rather than a race.
    """
    gate_a, gate_b = asyncio.Event(), asyncio.Event()
    turn_one, turn_two = asyncio.Event(), asyncio.Event()
    head = _model(_GatedChatModel, "head-test").script(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _dispatch_call("a", "call_a").tool_calls[0],
                    _dispatch_call("b", "call_b").tool_calls[0],
                ],
            ),
            AIMessage(content="Both angles are running."),
            AIMessage(content="Noted the return and your redirect."),
            AIMessage(content="Noted your second note."),
            _submit_call("Widgets cost $4.20 each."),
            AIMessage(content="Report submitted."),
        ]
    )
    head._gates = {1: turn_one, 2: turn_two}
    researcher = _model(_KeyedResearcherModel, "researcher-test")
    researcher._plans = {
        "Investigate a": (gate_a, "Angle a findings."),
        "Investigate b": (gate_b, "Angle b findings."),
    }
    three_models(head, researcher)

    sink = ActivitySink()
    session = make_session(sink=sink, interactive=True)
    run = asyncio.create_task(session.run())
    try:
        await _wait_for(lambda: len(head._entered) == 2, "the lead held inside turn 1")
        gate_a.set()
        await _wait_for(
            lambda: any(state.status == "done" for state in sink.researchers()),
            "researcher/1's return to be queued",
        )
        # Typed while turn 1 is still inside its model call, AFTER the return was queued.
        session.events.put_nowait(UserMessage("skip the routing angle"))
        turn_one.set()

        await _wait_for(lambda: len(head._entered) == 3, "the batched turn to start")
        session.events.put_nowait(UserMessage("and check the tariff angle"))
        turn_two.set()
        await _wait_for(lambda: head._call_count >= 4, "the second message's own turn")

        gate_b.set()
        await _wait_for(lambda: session.answer is not None, "the lead's report")
        session.request_quit()
        await asyncio.wait_for(run, timeout=_WAIT_TIMEOUT)
    finally:
        for gate in (gate_a, gate_b, turn_one, turn_two):
            gate.set()
        run.cancel()

    batched = head._received_messages[2][-1]
    assert isinstance(batched, HumanMessage)
    lines = str(batched.content).splitlines()
    assert lines[0] == "[researcher/1 — a] returned:"
    assert lines[1] == "Angle a findings."
    # The typed line comes AFTER the return it arrived behind, in arrival order.
    assert lines.index("skip the routing angle") > lines.index("Angle a findings.")
    assert lines[-1] == "Roster: done researcher/1 · running researcher/2"
    assert [line for line in lines if line.startswith("Roster:")] == [lines[-1]]

    # The second message rode the NEXT turn, alone — not appended to the one already running.
    second = head._received_messages[3][-1]
    assert isinstance(second, HumanMessage)
    assert str(second.content) == (
        "and check the tariff angle\nRoster: done researcher/1 · running researcher/2"
    )

    # Nothing was delivered twice: each typed line is exactly one injected `HumanMessage`.
    final_turn = head._received_messages[-1]
    for typed in ("skip the routing angle", "and check the tariff angle"):
        delivered = [
            message
            for message in final_turn
            if isinstance(message, HumanMessage) and typed in str(message.content)
        ]
        assert len(delivered) == 1, f"{typed!r} reached the lead {len(delivered)} times"

    # Each consumed line is echoed to the transcript as it goes to the lead, once.
    printed = capsys.readouterr().out
    assert printed.count("> skip the routing angle") == 1
    assert printed.count("> and check the tariff angle") == 1


async def test_a_message_typed_while_idle_starts_a_turn_instead_of_nudging(
    make_session, three_models
):
    """R1: an interactive session with an empty roster and an empty queue WAITS for the
    developer — it must not fire the headless `_SUBMIT_NOW` nudge, which would end the run
    while the developer was still typing.
    """
    answer = "Acme widgets are cheapest at $4.20 per unit."
    head = _model(ScriptedChatModel, "head-test").script(
        [
            AIMessage(content="Planning the angles."),
            _submit_call(answer),
            AIMessage(content="Report submitted."),
        ]
    )
    three_models(head, _model(ScriptedChatModel, "researcher-test").script([]))

    session = make_session(interactive=True)
    run = asyncio.create_task(session.run())
    try:
        await _wait_for(lambda: head._call_count >= 1, "the lead's first turn")
        # Idle: nothing running, nothing queued, no report. The session stays open.
        await asyncio.sleep(0.2)
        assert not run.done(), "the idle interactive session ended instead of waiting"
        assert head._call_count == 1

        session.events.put_nowait(UserMessage("what do you have so far?"))
        await _wait_for(lambda: head._call_count >= 2, "the typed message's own turn")
        await _wait_for(lambda: session.answer is not None, "the lead's report")
        session.request_quit()
        outcome = await asyncio.wait_for(run, timeout=_WAIT_TIMEOUT)
    finally:
        run.cancel()

    assert outcome is not None
    typed_turn = head._received_messages[1][-1]
    assert isinstance(typed_turn, HumanMessage)
    assert str(typed_turn.content) == "what do you have so far?\nRoster: done none · running none"
    assert all(
        _SUBMIT_NOW not in str(message.content)
        for batch in head._received_messages
        for message in batch
    ), "the headless nudge reached an interactive lead"


async def test_after_the_report_chat_continues_and_research_stays_closed(
    make_session, three_models, monkeypatch, tmp_path
):
    """R5/D3: the report is written when the lead submits, and the session keeps answering on
    the same thread afterwards. `dispatch_researcher` refuses, the report is written exactly
    once, and quitting after it is a CLEAN exit that still returns the outcome.
    """
    answer = "Acme widgets are cheapest at $4.20 per unit."
    head = _model(ScriptedChatModel, "head-test").script(
        [
            _submit_call(answer),
            AIMessage(content="Report submitted."),
            _dispatch_call("tariffs", "call_t"),
            AIMessage(content="Research is closed — from [S1], the tariff is 12%."),
        ]
    )
    three_models(head, _model(ScriptedChatModel, "researcher-test").script([]))

    report_path = tmp_path / "report.md"
    written: list[Any] = []

    def _capture(outcome: Any, config: Any):
        written.append(outcome)
        return report_path

    monkeypatch.setattr("harness.session.write_report", _capture)

    renderer = RecordingRenderer()
    session = make_session(interactive=True, renderer=renderer)
    run = asyncio.create_task(session.run())
    try:
        await _wait_for(lambda: written, "the report to be written")
        session.events.put_nowait(UserMessage("summarise source S1"))
        await _wait_for(lambda: head._call_count >= 4, "the post-report turn")
        session.request_quit()
        outcome = await asyncio.wait_for(run, timeout=_WAIT_TIMEOUT)
    finally:
        run.cancel()

    assert outcome is not None
    assert outcome.answer == answer
    assert session.cut_short is None
    # One report for the session, however long the chat after it runs.
    assert len(written) == 1
    # The path reaches the transcript when the report LANDS, and the end-of-run summary comes
    # only after the chat — `RunFinished` stops the live region, so it cannot precede it (D3).
    kinds = [type(event).__name__ for event in renderer.events]
    assert ReportWritten(report_path) in renderer.events
    assert kinds.index("ReportWritten") < kinds.index("RunFinished")
    assert kinds.count("RunFinished") == 1
    # The post-report turn's prose reached the transcript too.
    assert any(
        isinstance(event, AgentText) and "Research is closed" in event.text
        for event in renderer.events
    )

    refusals = [
        str(message.content)
        for message in head._received_messages[-1]
        if isinstance(message, ToolMessage) and message.name == "dispatch_researcher"
    ]
    assert refusals, "the post-report dispatch never reached the tool"
    assert refusals[-1].startswith("refused:")
    # A refused dispatch must not re-open the research stage: `_finish` has already closed
    # every stage out, so an `advance("researching")` here starts a stage nothing will ever
    # complete — a live "researching" spinner over a run that is done researching.
    assert not [
        event
        for event in renderer.events
        if isinstance(event, StageStarted) and event.stage == "researching"
    ], "the refused post-report dispatch re-opened the researching stage"
    finished = next(event for event in renderer.events if isinstance(event, RunFinished))
    assert "researching" not in [stage for stage, _ in finished.stage_timings]


async def test_a_cancel_after_the_report_still_returns_the_written_outcome(
    make_session, three_models, monkeypatch, tmp_path
):
    """R5: Ctrl+C AFTER the report is a clean exit (0), not a failed run — the report is
    already on disk, so `run()` hands back the outcome rather than `None`.
    """
    answer = "Acme widgets are cheapest at $4.20 per unit."
    head = _model(ScriptedChatModel, "head-test").script(
        [_submit_call(answer), AIMessage(content="Report submitted.")]
    )
    three_models(head, _model(ScriptedChatModel, "researcher-test").script([]))

    written: list[Any] = []
    report_path = tmp_path / "report.md"

    def _capture(outcome: Any, config: Any):
        written.append(outcome)
        return report_path

    monkeypatch.setattr("harness.session.write_report", _capture)

    session = make_session(interactive=True)
    run = asyncio.create_task(session.run())
    await _wait_for(lambda: written, "the report to be written")
    # Long enough that the cancel lands on the post-report chat's own wait.
    await asyncio.sleep(0.1)
    run.cancel()
    outcome = await run

    assert outcome is not None
    assert outcome.answer == answer
    assert session.cut_short is None


async def test_quitting_before_the_report_fails_the_run_and_writes_nothing(
    make_session, three_models, monkeypatch
):
    """R5's other half, and the Phase 2 gate unchanged: a quit with no report yet is a FAILED
    run — `run()` returns None, `cut_short` is `error`, and nothing is written.

    The elapsed assertion is load-bearing: `wait_for`'s own timeout cancels the run task, which
    the abort clause handles identically, so every assertion below would pass on a
    `request_quit` that did nothing at all. Only the timing tells a quit from a hang.
    """
    parked = asyncio.Event()
    head = _model(ScriptedChatModel, "head-test").script(
        [_dispatch_call("pricing", "call_p"), AIMessage(content="The pricing angle is running.")]
    )
    researcher = _model(_KeyedResearcherModel, "researcher-test")
    researcher._plans = {"Investigate pricing": (parked, "Pricing findings.")}
    three_models(head, researcher)

    written: list[Any] = []
    monkeypatch.setattr("harness.session.write_report", lambda outcome, config: written.append(1))

    session = make_session(interactive=True)
    run = asyncio.create_task(session.run())
    try:
        await _wait_for(
            lambda: head._call_count >= 2 and len(session.running) == 1,
            "the lead waiting on its researcher",
        )
        await asyncio.sleep(0.05)
        quit_at = time.monotonic()
        session.request_quit()
        outcome = await asyncio.wait_for(run, timeout=_WAIT_TIMEOUT)
        elapsed = time.monotonic() - quit_at
    finally:
        parked.set()
        run.cancel()

    assert elapsed < 1.0, f"the quit took {elapsed}s — it waited instead of aborting the turn"
    assert outcome is None
    assert written == []
    assert session.cut_short == "error"
    assert session.cut_short_detail == "user abort (Ctrl+C)"
    assert session.running == {}


async def test_receive_user_message_queues_a_user_message(make_session):
    """3F Major 3, session half: the composer's entry point wraps the line as a `UserMessage`
    on the event queue — the contract the keyboard→queue bridge depends on.
    """
    session = make_session()

    session.receive_user_message("check the tariff angle")

    event = session.events.get_nowait()
    assert isinstance(event, UserMessage)
    assert event.text == "check the tariff angle"


async def test_a_failed_chat_turn_discloses_and_keeps_the_chat_open(
    make_session, three_models, monkeypatch, tmp_path
):
    """3F Major 1: a provider error during a post-report turn must not escape `run()` —
    that would skip the end-of-run summary and turn a run whose report is already on disk
    into exit 1 or a traceback, instead of R5's clean exit. The failure is disclosed as an
    alert, `cut_short` stays None (the run succeeded), and the NEXT chat line still gets
    its turn.
    """
    answer = "Acme widgets are cheapest at $4.20 per unit."
    head = _model(_FailingChatModel, "head-test").script(
        [
            _submit_call(answer),
            AIMessage(content="Report submitted."),
            AIMessage(content="unused — this call fails at the model"),
            AIMessage(content="From [S1]: the tariff is 12%."),
        ]
    )
    head._failures = {2: httpx.ConnectError("502 behind the gateway")}
    three_models(head, _model(ScriptedChatModel, "researcher-test").script([]))

    written: list[Any] = []
    report_path = tmp_path / "report.md"

    def _capture(outcome: Any, config: Any):
        written.append(outcome)
        return report_path

    monkeypatch.setattr("harness.session.write_report", _capture)

    renderer = RecordingRenderer()
    session = make_session(interactive=True, renderer=renderer)
    run = asyncio.create_task(session.run())
    try:
        await _wait_for(lambda: written, "the report to be written")
        session.events.put_nowait(UserMessage("what is the tariff?"))
        await _wait_for(lambda: len(head._received_messages) >= 3, "the failing chat turn")
        session.events.put_nowait(UserMessage("and in words?"))
        await _wait_for(lambda: head._call_count >= 4, "the retried chat turn")
        session.request_quit()
        outcome = await asyncio.wait_for(run, timeout=_WAIT_TIMEOUT)
    finally:
        run.cancel()

    assert outcome is not None
    assert outcome.answer == answer
    assert session.cut_short is None
    assert len(written) == 1
    # The failure was disclosed as an alert, and the summary still fired after the chat.
    kinds = [type(event).__name__ for event in renderer.events]
    assert any(
        isinstance(event, Alert) and "chat turn failed" in event.text for event in renderer.events
    )
    assert kinds.count("RunFinished") == 1
    assert kinds.index("Alert") < kinds.index("RunFinished")
    # The retried line reached the model, and its prose reached the transcript.
    assert any(
        isinstance(event, AgentText) and "tariff is 12%" in event.text for event in renderer.events
    )


async def test_a_keyboard_interrupt_during_a_chat_turn_is_a_clean_quit(
    make_session, three_models, monkeypatch, tmp_path
):
    """Round 2, item 1: Ctrl+C during a post-report turn is a QUIT, not a failed turn.

    `_chat_turn` catches `Exception`, which `KeyboardInterrupt` is not, so unless `_chat_loop`
    names it beside `CancelledError` it escapes `run()` altogether: no `RunFinished`, no
    returned outcome, and a traceback out of a run whose report is already on disk (R5). It
    must also NOT be swallowed as a disclosed chat-turn failure — a Ctrl+C is a quit.

    Raised from the turn's own stream pass, not from the scripted model and not from a real
    signal. A `KeyboardInterrupt` raised inside ANY asyncio task — which is what a langgraph
    node is — is re-raised by the task machinery into the event loop itself, so it escapes
    `asyncio.run` no matter who awaits the task, and under pytest that ends the whole session
    rather than the test (the Phase 2 handoff note). This is also the faithful delivery point:
    a real Ctrl+C surfaces on the session's OWN coroutine (inside a model call it has been
    observed as `CancelledError` instead — see `run()`'s abort clause).
    """
    answer = "Acme widgets are cheapest at $4.20 per unit."
    head = _model(ScriptedChatModel, "head-test").script(
        [_submit_call(answer), AIMessage(content="Report submitted.")]
    )
    three_models(head, _model(ScriptedChatModel, "researcher-test").script([]))

    written: list[Any] = []
    report_path = tmp_path / "report.md"

    def _capture(outcome: Any, config: Any):
        written.append(outcome)
        return report_path

    monkeypatch.setattr("harness.session.write_report", _capture)

    renderer = RecordingRenderer()
    session = make_session(interactive=True, renderer=renderer)
    real_stream_pass = session._stream_pass

    async def _interrupted_stream_pass(stream_input: Any) -> Any:
        # Only once the report exists: the research phase runs on the real graph, and the
        # interrupt lands on the chat turn the developer typed into.
        if session.answer is not None:
            raise KeyboardInterrupt
        return await real_stream_pass(stream_input)

    monkeypatch.setattr(session, "_stream_pass", _interrupted_stream_pass)

    run = asyncio.create_task(session.run())
    try:
        await _wait_for(lambda: written, "the report to be written")
        session.events.put_nowait(UserMessage("what is the tariff?"))
        outcome = await asyncio.wait_for(run, timeout=_WAIT_TIMEOUT)
    finally:
        run.cancel()

    assert outcome is not None
    assert outcome.answer == answer
    assert session.cut_short is None
    kinds = [type(event).__name__ for event in renderer.events]
    assert kinds.count("RunFinished") == 1
    assert not [
        event
        for event in renderer.events
        if isinstance(event, Alert) and "chat turn failed" in event.text
    ], "the interrupt was disclosed as a failed turn instead of ending the chat"


async def test_a_clarifying_answer_is_trimmed_and_any_url_in_it_becomes_fetchable(make_session):
    """Moved here with the code it covers (Phase 4). An answer is user-supplied text exactly
    like the opening question, so a URL pasted into it must be approved — the natural reply to
    "which page do you mean?" IS that URL, and without approval every later fetch of it is
    provenance_rejected. Trimming belongs here too, not to the stdin bridge that read it.
    """
    registry = SourceRegistry()
    session = make_session(
        registry=registry,
        answer_source=_typed("  this one: https://example.test/docs/page  "),
    )
    interrupt = Interrupt(value={"action_requests": [{"args": {"question": "Which page?"}}]})

    decisions = await session._collect_answers(interrupt)

    url = "https://example.test/docs/page"
    assert decisions == [{"type": "respond", "message": f"this one: {url}"}]
    assert registry.is_approved(url)


async def test_a_clarifying_question_can_arrive_without_a_question_argument(make_session):
    """Exercises the `description` and `str(args)` fallbacks of `args["question"] or
    description or str(args)`: nothing guarantees deepagents keeps putting the prompt under
    `args`, and those fallbacks are all that stand between a schema change and an empty prompt
    at the terminal. Driven through `_collect_answers` directly, since no real model can be
    scripted into that shape.
    """
    renderer = RecordingRenderer()
    session = make_session(renderer=renderer, answer_source=_typed("answered"))
    interrupt = Interrupt(
        value={
            "action_requests": [
                {"name": "ask_user", "args": {}, "description": "Metal or album?"},
                {"name": "ask_user", "args": {"topic": "isotope"}},
            ]
        }
    )

    decisions = await session._collect_answers(interrupt)

    asked = [event.text for event in renderer.events if isinstance(event, Question)]
    assert asked[0] == "Metal or album?", "the description fallback never fired"
    assert "isotope" in asked[1], "the str(args) fallback never fired"
    assert [decision["message"] for decision in decisions] == ["answered", "answered"]


async def test_only_the_first_four_choices_are_offered_and_resolvable(make_session):
    """R4 caps `ask_user` at four choices, but the interrupt path never runs the tool's
    `args_schema` — `HumanInTheLoopMiddleware` answers with a `respond` decision and SKIPS
    tool execution — so the schema's `max_length=4` is only a model-facing hint and the
    session must clamp what a misbehaving lead actually sent. A fifth-or-later choice is
    neither rendered nor digit-resolvable: "5" against six sent choices is free text.
    """
    renderer = RecordingRenderer()
    session = make_session(renderer=renderer, answer_source=_typed("5"))
    interrupt = Interrupt(
        value={
            "action_requests": [
                {"args": {"question": "Pick one", "choices": ["a", "b", "c", "d", "e", "f"]}}
            ]
        }
    )

    decisions = await session._collect_answers(interrupt)

    question = next(event for event in renderer.events if isinstance(event, Question))
    assert question.choices == ("a", "b", "c", "d"), "the fifth and sixth choices were offered"
    assert decisions == [{"type": "respond", "message": "5"}], (
        "a digit past the clamped range resolved to a hidden choice"
    )


async def test_the_stage_returns_to_researching_after_a_mid_research_answer(
    make_session, three_models
):
    """Phase 4 made mid-research questions legal, but the `clarifying` stage advance was only
    ever left behind: nothing re-entered `researching` until the NEXT dispatch, so the live
    header read "clarifying" while researchers kept running and `stage_timings` recorded a
    truncated `researching`. Answered-with-researchers-still-running must advance back.
    """
    gate = asyncio.Event()
    head = _model(ScriptedChatModel, "head-test").script(
        [
            AIMessage(content="", tool_calls=[_dispatch_call("a", "call_a").tool_calls[0]]),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ask_user",
                        "args": {"question": "Broad or deep?"},
                        "id": "call_ask",
                    }
                ],
            ),
            AIMessage(content="Going deep."),
            _submit_call("Widgets cost $4.20 each."),
            AIMessage(content="Report submitted."),
        ]
    )
    researcher = _model(_KeyedResearcherModel, "researcher-test")
    researcher._plans = {"Investigate a": (gate, "Angle a findings.")}
    three_models(head, researcher)

    asked = asyncio.Event()

    async def _answer() -> str:
        asked.set()
        return "deep"

    renderer = RecordingRenderer()
    session = make_session(answer_source=_answer, renderer=renderer)
    run = asyncio.create_task(session.run())
    try:
        await _wait_for(asked.is_set, "the lead's mid-research clarifying question")
        assert session.running, "the researcher had already ended before the question was asked"
        # The answer resolves WHILE the researcher is still gated: the stage must go back to
        # researching, not sit on clarifying until the next dispatch.
        await _wait_for(
            lambda: any(
                isinstance(event, StageStarted) and event.stage == "researching"
                for event in renderer.events[
                    next(i for i, e in enumerate(renderer.events) if isinstance(e, Question)) :
                ]
            ),
            "the stage to return to researching after the answer",
        )
    finally:
        gate.set()
        outcome = await asyncio.wait_for(run, timeout=_WAIT_TIMEOUT)
        run.cancel()

    assert outcome is not None


async def test_a_mid_run_question_resolves_a_digit_while_researchers_keep_running(
    make_session, three_models
):
    """R4: the lead may ask at any point, not only before research starts. The pending
    question must not stall a researcher already running -- the whole reason the interrupt is
    handled inside the turn rather than around the loop -- and a digit inside the offered
    range must reach the model as the CHOICE it numbers, never as the digit itself.
    """
    gate = asyncio.Event()
    head = _model(ScriptedChatModel, "head-test").script(
        [
            AIMessage(content="", tool_calls=[_dispatch_call("a", "call_a").tool_calls[0]]),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ask_user",
                        "args": {"question": "Which region?", "choices": ["EU", "US"]},
                        "id": "call_ask",
                    }
                ],
            ),
            AIMessage(content="Focusing on US."),
            _submit_call("Widgets cost $4.20 each."),
            AIMessage(content="Report submitted."),
        ]
    )
    researcher = _model(_KeyedResearcherModel, "researcher-test")
    researcher._plans = {"Investigate a": (gate, "Angle a findings.")}
    three_models(head, researcher)

    asked, release = asyncio.Event(), asyncio.Event()

    async def _answer() -> str:
        asked.set()
        await release.wait()
        return "2"

    renderer = RecordingRenderer()
    session = make_session(answer_source=_answer, renderer=renderer)
    run = asyncio.create_task(session.run())
    try:
        await _wait_for(asked.is_set, "the lead's mid-run clarifying question")
        assert session.running, "the researcher had already ended before the question was asked"
        # The property under test: the researcher runs to completion WHILE the answer is
        # still outstanding. A question that blocked the loop would never let this settle.
        gate.set()
        await _wait_for(
            lambda: not session.running,
            "the researcher to finish while the question was still pending",
        )
        release.set()
        outcome = await asyncio.wait_for(run, timeout=_WAIT_TIMEOUT)
    finally:
        gate.set()
        release.set()
        run.cancel()

    assert outcome is not None
    state = await session.agent.aget_state({"configurable": {"thread_id": session.thread_id}})
    answered = [
        str(message.content)
        for message in state.values["messages"]
        if isinstance(message, ToolMessage) and message.name == "ask_user"
    ]
    assert answered == ["US"], "the digit reached the model instead of the choice it numbered"
    assert [event for event in renderer.events if isinstance(event, Question)] == [
        Question("Which region?", choices=("EU", "US"))
    ]


async def test_a_post_report_question_does_not_reopen_the_clarifying_stage(
    make_session, three_models, monkeypatch, tmp_path
):
    """Round 3, item 4: the dispatch guard's twin. `ask_user` stays on the lead toolset all
    session, so a post-report clarifying question would `advance("clarifying")` after
    `_finish` ran `_tracker.finish()` — a stage nothing completes, and a stray row in
    `RunFinished.stage_timings`.
    """
    answer = "Acme widgets are cheapest at $4.20 per unit."
    head = _model(ScriptedChatModel, "head-test").script(
        [_submit_call(answer), AIMessage(content="Noted."), AIMessage(content="Done.")]
    )
    three_models(head, _model(ScriptedChatModel, "researcher-test").script([]))

    written: list[Any] = []
    monkeypatch.setattr(
        "harness.session.write_report",
        lambda outcome, config: (written.append(outcome), tmp_path / "report.md")[1],
    )

    renderer = RecordingRenderer()
    session = make_session(interactive=True, renderer=renderer)
    real_stream_pass = session._stream_pass
    asked = {"done": False}

    async def _questioning_stream_pass(stream_input: Any) -> Any:
        # One synthetic interrupt on the first post-report turn; the resume Command and
        # every research-phase pass run on the real graph.
        if session.answer is not None and not asked["done"]:
            asked["done"] = True
            return {"__interrupt__": [object()]}
        return await real_stream_pass(stream_input)

    async def _answer(interrupt: Any) -> list[dict[str, Any]]:
        return [{"type": "respond", "args": "the cheap one"}]

    monkeypatch.setattr(session, "_stream_pass", _questioning_stream_pass)
    monkeypatch.setattr(session, "_collect_answers", _answer)

    run = asyncio.create_task(session.run())
    try:
        await _wait_for(lambda: written, "the report to be written")
        session.events.put_nowait(UserMessage("which one should I buy?"))
        await _wait_for(lambda: asked["done"], "the post-report question")
        session.request_quit()
        outcome = await asyncio.wait_for(run, timeout=_WAIT_TIMEOUT)
    finally:
        run.cancel()

    assert outcome is not None
    post_report_stages = [
        event
        for event in renderer.events
        if isinstance(event, StageStarted) and event.stage == "clarifying"
    ]
    assert not post_report_stages, "a post-report question re-opened the clarifying stage"
    finished = next(event for event in renderer.events if isinstance(event, RunFinished))
    assert "clarifying" not in [stage for stage, _ in finished.stage_timings]


async def test_chat_turns_do_not_count_toward_the_round_cap(
    make_session, three_models, monkeypatch, tmp_path
):
    """3F Major 2: post-report turns are uncapped (R5/R6). Counting them would push
    `_rounds_used` past `max_rounds`, fire the synthesis pass on a SUCCESSFUL run, stamp
    `round_cap` onto it, and leave `_overrun` breaking every later chat turn mid-stream.
    The margin check carries the same `answer is None` guard.
    """
    answer = "Acme widgets are cheapest at $4.20 per unit."
    head = _model(ScriptedChatModel, "head-test").script(
        [
            _submit_call(answer),  # the one counted round (the answer is None here)
            AIMessage(content="Report submitted."),  # post-submit wrap-up: already uncounted
            AIMessage(content="chat one"),  # chat turn 1 — uncounted
            AIMessage(content="chat two"),  # chat turn 2 — would be round 3+: overrun
        ]
    )
    three_models(head, _model(ScriptedChatModel, "researcher-test").script([]))

    written: list[Any] = []
    report_path = tmp_path / "report.md"

    def _capture(outcome: Any, config: Any):
        written.append(outcome)
        return report_path

    monkeypatch.setattr("harness.session.write_report", _capture)

    session = make_session(max_rounds=3, interactive=True)
    run = asyncio.create_task(session.run())
    try:
        await _wait_for(lambda: written, "the report to be written")
        session.events.put_nowait(UserMessage("question one"))
        await _wait_for(lambda: head._call_count >= 3, "the first chat turn")
        session.events.put_nowait(UserMessage("question two"))
        await _wait_for(lambda: head._call_count >= 4, "the second chat turn")
        session.request_quit()
        outcome = await asyncio.wait_for(run, timeout=_WAIT_TIMEOUT)
    finally:
        run.cancel()

    assert outcome is not None
    assert session.cut_short is None
    # Only the submit turn itself counted: the wrap-up call and every chat turn land after
    # the answer exists, and none of them may push `_rounds_used` toward the cap.
    assert session._rounds_used == 1
    assert len(written) == 1


async def test_a_quit_after_run_returned_cancels_nothing(make_session, three_models):
    """3F Minor d: `request_quit` may only cancel the run while `run()` is in flight.

    In production `run()` is awaited by main()'s own task, so a stale `_run_task` would let a
    quit key landing in the gap after run() returned cancel main()'s `finally` in the middle
    of `browser.close()`. This test awaits `run()` DIRECTLY, so a stale cancel delivers
    `CancelledError` into the test itself and fails it at the await below.
    """
    head = _model(ScriptedChatModel, "head-test").script(
        [AIMessage(content="Nothing to add."), AIMessage(content="Still nothing to add.")]
    )
    three_models(head, _model(ScriptedChatModel, "researcher-test").script([]))

    session = make_session(interactive=False)
    outcome = await session.run()

    assert outcome is None
    assert session.cut_short == "error"
    session.request_quit()
    await asyncio.sleep(0)
    assert session._quit is True
