"""End-to-end regression: the full lead -> researcher -> reader delegation loop through a real
deepagents graph.

New file, not folded into `tests/test_agent.py`: the scripted scenario composes DISTINCT
head/researcher/reader models, the fake crawler, `main()`'s own config/preflight patching, and a
`write_report` spy all at once -- `test_agent.py`'s `noop_agent` fixture scripts one shared
model over every role, which this scenario cannot reuse. Implementor's call, per the plan.
"""

from langchain_core.messages import AIMessage
from pydantic import SecretStr

import harness.__main__ as main_module
from harness.sources import sources_dir
from tests.conftest import ScriptedChatModel, _FakeMarkdown, _FakeResult, verify_reply
from tests.test_verify import _flatten

_URL = "https://example.test/page"
_CAPTURE_MARKER = "CAPTURE-UNIQUE-MARKER-55d10e"
_DIGEST_MARKER = "DIGEST-UNIQUE-MARKER-91c44d"
_RESEARCHER_MARKER = "RESEARCHER-UNIQUE-MARKER-2b6c19"
_HEAD_MARKER = "HEAD-UNIQUE-MARKER-7f3ab2"

_RESEARCHER_DESCRIPTION = (
    "Objective: determine whether the widget line's early crash reports show a defect "
    "pattern. Output format: prose findings with [Sn] markers. Tools: search and reader "
    "delegation. Boundaries: technical specs only, not marketing claims."
)

_READER_DESCRIPTION = (
    f"Objective: read {_URL} for the widget defect pattern. Output format: prose findings "
    "with [Sn] markers. Tools: fetch_pages only, no search. Boundaries: technical specs "
    "only, not marketing claims."
)


def _task_call(description: str, subagent_type: str, call_id: str = "call_task") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "task",
                "args": {"description": description, "subagent_type": subagent_type},
                "id": call_id,
            }
        ],
    )


def _fetch_call(call_id: str = "call_fetch") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "fetch_pages", "args": {"urls": [_URL]}, "id": call_id}],
    )


async def _run_delegation(make_config, patch_models_by_role, monkeypatch, install_crawler):
    """Drive one full `main()` run through the real graph: the lead delegates to a researcher,
    the researcher delegates to a reader, the reader fetches via the fake crawler, the digest
    returns up through both tiers, verification runs, and the report is written.

    Hand-rolls what `patch_run` does (`load_config` + preflight skip) rather than reusing it:
    `patch_run` binds ONE model to every role, and this scenario needs role-distinct models.
    """
    config = make_config(
        head_model="head-test-model",
        researcher_model="researcher-test-model",
        reader_model="reader-test-model",
    )

    head_model = ScriptedChatModel(
        model="head-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script(
        [
            _task_call(_RESEARCHER_DESCRIPTION, "researcher"),
            AIMessage(
                content=(
                    f"Widget defect reports are documented on the source page [S1]. {_HEAD_MARKER}"
                )
            ),
            verify_reply("supported", "The capture confirms the digest's claim."),
        ]
    )
    researcher_model = ScriptedChatModel(
        model="researcher-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script(
        [
            _task_call(_READER_DESCRIPTION, "reader"),
            AIMessage(
                content=(
                    "The reader's digest confirms a widget defect pattern [S1]. "
                    f"{_RESEARCHER_MARKER}"
                )
            ),
        ]
    )
    reader_model = ScriptedChatModel(
        model="reader-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script(
        [
            _fetch_call(),
            AIMessage(
                content=(
                    "The example.test page describes the widget's defect history [S1]. "
                    f"{_DIGEST_MARKER}"
                )
            ),
        ]
    )
    # Verification runs on the "verifier" role (Phase 1 Step 4); this scenario's verify_reply
    # is scripted on `head_model` itself, so it is routed there too rather than a fourth model.
    patch_models_by_role(
        {
            "head": head_model,
            "researcher": researcher_model,
            "reader": reader_model,
            "verifier": head_model,
        }
    )

    install_crawler(
        [
            _FakeResult(
                _URL,
                markdown=_FakeMarkdown(
                    raw_markdown=f"Widget defect report body. {_CAPTURE_MARKER}",
                    fit_markdown=f"Widget defect report body. {_CAPTURE_MARKER}",
                ),
            )
        ]
    )

    monkeypatch.setattr(main_module, "load_config", lambda: config)

    async def _noop_preflight(cfg, role):
        return None

    # At the source module: `main` imports `preflight` at call time (heavy-import deferral).
    monkeypatch.setattr("harness.models.preflight", _noop_preflight)

    # Like `patch_run`, the search preflight is neutralized: it is a real HTTP probe against
    # `config.search.base_url`, and this scenario installs no search transport.
    async def _noop_search_preflight(cfg):
        return None

    monkeypatch.setattr(main_module, "preflight_search", _noop_search_preflight)

    captured: dict = {}
    real_write_report = main_module.write_report

    def _spy(outcome, cfg):
        path = real_write_report(outcome, cfg)
        captured["outcome"] = outcome
        captured["path"] = path
        return path

    monkeypatch.setattr(main_module, "write_report", _spy)

    exit_code = await main_module.main(["does the widget line show a defect pattern?"])

    return {
        "config": config,
        "head_model": head_model,
        "researcher_model": researcher_model,
        "reader_model": reader_model,
        "captured": captured,
        "exit_code": exit_code,
    }


async def test_end_to_end_delegation_loop_digests_and_resolves_citations(
    make_config, patch_models_by_role, monkeypatch, install_crawler
):
    """R1/R4/R5: the lead delegates through `task` to a researcher, which delegates through its
    OWN `task` to a reader; the reader fetches via the shared `fetch_pages` instance against the
    fake crawler, the digest comes back up through both tiers, the lead synthesizes from the
    researcher's report, and the written report resolves `[S1]` and discloses it as digested.
    """
    result = await _run_delegation(make_config, patch_models_by_role, monkeypatch, install_crawler)

    assert result["exit_code"] == 0
    config = result["config"]
    outcome = result["captured"]["outcome"]
    registry = outcome.registry
    body = result["captured"]["path"].read_text(encoding="utf-8")

    # R1/R4: the run completed, cited [S1], and the registry resolved it to the fake URL.
    assert "[S1]" in body
    assert _URL in body
    assert _HEAD_MARKER in body

    # R4 identity: S1 was minted by the READER's own fetch call, not a stand-in.
    source = registry.get("S1")
    assert source is not None
    assert source.read_mode == "digested"
    capture_path = sources_dir(config, registry) / "S1.md"
    assert capture_path.exists()
    assert _CAPTURE_MARKER in capture_path.read_text(encoding="utf-8")

    # R5: the read-mode disclosure names this run as fully digested. A single-source run
    # renders the all-digested SUMMARY line rather than a per-source [Sn] bullet -- see
    # `report.py`'s `_read_modes_section`, which lists individual bullets only in a MIXED-mode
    # run (untouched this phase; out of scope to widen).
    assert "read via reader digests" in body

    # Protocol sanity (risk #4): the reader model was genuinely invoked with the frozen
    # reader.md system prompt, not some stand-in text -- proof the scripted fetch call reached
    # the real per-subagent bind/routing seam rather than being satisfied by a stub.
    reader_model = result["reader_model"]
    assert reader_model._received_messages, "the reader model was never called"
    first_reader_request = reader_model._received_messages[0]
    assert any(
        "You are a reader in a cited-sources research harness" in str(m.content)
        for m in first_reader_request
    ), "the reader subagent never received the rendered reader.md system prompt"


_URL_B = "https://example.test/page-b"

_ANGLE_A_DESCRIPTION = _RESEARCHER_DESCRIPTION
_ANGLE_B_DESCRIPTION = (
    "Objective: check the housing supplier's recall history. Output format: prose findings "
    "with [Sn] markers. Tools: search and reader delegation. Boundaries: technical specs "
    "only, not marketing claims."
)
_READER_B_DESCRIPTION = (
    f"Objective: read {_URL_B} for the supplier recall history. Output format: prose findings "
    "with [Sn] markers. Tools: fetch_pages only, no search. Boundaries: technical specs "
    "only, not marketing claims."
)


def _fetch_call_for(url: str, call_id: str = "call_fetch") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "fetch_pages", "args": {"urls": [url]}, "id": call_id}],
    )


async def test_report_discloses_a_mixed_digested_and_unread_run(
    make_config, patch_models_by_role, monkeypatch, install_crawler
):
    """Step 4 TEST-FIRST item 1: a scripted 3-tier run with ONE digested source (angle A's
    reader digest reaches the researcher and the lead) and ONE unread source (angle B's reader
    fetches successfully then crashes, exhausting `ToolRetryMiddleware`'s one retry, leaving the
    source registered but never digested — mirrors
    `tests/test_agent.py::test_a_reader_crash_after_a_successful_fetch_leaves_the_source_unread`
    one tier up) -- the WRITTEN REPORT's `## Source reading` section must disclose BOTH modes,
    matching the registry's actual state.
    """
    config = make_config(
        head_model="head-test-model",
        researcher_model="researcher-test-model",
        reader_model="reader-test-model",
    )

    head_model = ScriptedChatModel(
        model="head-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script(
        [
            _task_call(_ANGLE_A_DESCRIPTION, "researcher", call_id="call_angle_a"),
            _task_call(_ANGLE_B_DESCRIPTION, "researcher", call_id="call_angle_b"),
            AIMessage(
                content="Widget defect reports are documented on the source page [S1]. "
                f"{_HEAD_MARKER}"
            ),
            verify_reply("supported", "The capture confirms the digest's claim."),
        ]
    )
    researcher_model = ScriptedChatModel(
        model="researcher-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script(
        [
            _task_call(_READER_DESCRIPTION, "reader", call_id="call_read_a"),
            AIMessage(content="The reader's digest confirms a widget defect pattern [S1]."),
            _task_call(_READER_B_DESCRIPTION, "reader", call_id="call_read_b"),
            AIMessage(content="The supplier recall angle could not be confirmed."),
        ]
    )
    reader_model = ScriptedChatModel(
        model="reader-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script(
        [
            _fetch_call_for(_URL),
            AIMessage(content="The example.test page describes the widget's defect history [S1]."),
            _fetch_call_for(_URL_B),
            # No further reply scripted: the second reader's next model call exhausts the
            # script, simulating a post-fetch crash. `ToolRetryMiddleware` retries the task
            # once; the fresh retry attempt's first model call is ALSO past the script's end,
            # so it crashes too, and the researcher receives a `READER FAILED` error message.
            # The fetched source (S2) stays registered but "unread".
        ]
    )
    patch_models_by_role(
        {
            "head": head_model,
            "researcher": researcher_model,
            "reader": reader_model,
            "verifier": head_model,
        }
    )

    install_crawler(
        [
            _FakeResult(
                _URL,
                markdown=_FakeMarkdown(
                    raw_markdown=f"Widget defect report body. {_CAPTURE_MARKER}",
                    fit_markdown=f"Widget defect report body. {_CAPTURE_MARKER}",
                ),
            ),
            _FakeResult(
                _URL_B,
                markdown=_FakeMarkdown(
                    raw_markdown="Supplier recall report body.",
                    fit_markdown="Supplier recall report body.",
                ),
            ),
        ]
    )

    monkeypatch.setattr(main_module, "load_config", lambda: config)

    async def _noop_preflight(cfg, role):
        return None

    monkeypatch.setattr("harness.models.preflight", _noop_preflight)

    async def _noop_search_preflight(cfg):
        return None

    monkeypatch.setattr(main_module, "preflight_search", _noop_search_preflight)

    captured: dict = {}
    real_write_report = main_module.write_report

    def _spy(outcome, cfg):
        path = real_write_report(outcome, cfg)
        captured["outcome"] = outcome
        captured["path"] = path
        return path

    monkeypatch.setattr(main_module, "write_report", _spy)

    exit_code = await main_module.main(["does the widget line show a defect pattern?"])

    assert exit_code == 0
    registry = captured["outcome"].registry
    body = captured["path"].read_text(encoding="utf-8")

    source_a = registry.get("S1")
    source_b = registry.get("S2")
    assert source_a is not None and source_a.read_mode == "digested"
    assert source_b is not None and source_b.read_mode == "unread"

    # The `## Source reading` rollup (harness/report.py's `_read_modes_section`, unchanged by
    # this step) discloses BOTH modes present in this mixed run -- the all-digested summary
    # branch (used only when every registered source is digested) must NOT fire here.
    assert "Digested via the reader:" in body
    assert "Not read at all (fetch never succeeded):" in body
    assert f"[{source_a.id}]" in body
    assert f"[{source_b.id}]" in body
    assert "sources were read via reader digests" not in body


async def test_verification_reads_the_capture_file_not_the_reader_digest(
    make_config, patch_models_by_role, monkeypatch, install_crawler
):
    """R3: `verify_paragraphs` judges the claim against the CAPTURED page text, never the
    reader's digest or the researcher's report -- even though the researcher's report is all
    the lead itself ever saw.
    """
    result = await _run_delegation(make_config, patch_models_by_role, monkeypatch, install_crawler)

    head_model = result["head_model"]
    # Script order: task call, final synthesis, verify reply -- the verify call is the model's
    # LAST invocation.
    verify_messages = head_model._received_messages[-1]
    verify_text = _flatten(verify_messages)

    assert _CAPTURE_MARKER in verify_text
    assert _DIGEST_MARKER not in verify_text
    assert _RESEARCHER_MARKER not in verify_text
