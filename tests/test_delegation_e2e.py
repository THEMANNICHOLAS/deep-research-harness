"""End-to-end regression: the full reader-delegation loop through a real deepagents graph.

New file, not folded into `tests/test_agent.py`: the scripted scenario composes DISTINCT
head/reader models, the fake crawler, `main()`'s own config/preflight patching, and a
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
_HEAD_MARKER = "HEAD-UNIQUE-MARKER-7f3ab2"

_DESCRIPTION = (
    "Objective: determine whether the widget line's early crash reports show a defect "
    f"pattern, reading {_URL}. Output format: prose findings with [Sn] markers. "
    "Tools: fetch_pages only, no search. Boundaries: technical specs only, not marketing "
    "claims."
)


def _task_call(call_id: str = "call_task") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "task",
                "args": {"description": _DESCRIPTION, "subagent_type": "reader"},
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
    """Drive one full `main()` run through the real graph: lead delegates, reader fetches via
    the fake crawler, the digest returns, verification runs, and the report is written.

    Hand-rolls what `patch_run` does (`load_config` + preflight skip) rather than reusing it:
    `patch_run` binds ONE model to every role, and this scenario needs role-distinct models.
    """
    config = make_config(head_model="head-test-model", subagent_model="reader-test-model")

    head_model = ScriptedChatModel(
        model="head-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script(
        [
            _task_call(),
            AIMessage(
                content=(
                    f"Widget defect reports are documented on the source page [S1]. {_HEAD_MARKER}"
                )
            ),
            verify_reply("supported", "The capture confirms the digest's claim."),
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
    patch_models_by_role({"head": head_model, "subagent": reader_model})

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
        "reader_model": reader_model,
        "captured": captured,
        "exit_code": exit_code,
    }


async def test_end_to_end_delegation_loop_digests_and_resolves_citations(
    make_config, patch_models_by_role, monkeypatch, install_crawler
):
    """R1/R4/R5: the lead delegates through `task`, the reader fetches via the shared
    `fetch_pages` instance against the fake crawler, the digest comes back, the lead
    synthesizes from it, and the written report resolves `[S1]` and discloses it as digested.
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


async def test_verification_reads_the_capture_file_not_the_reader_digest(
    make_config, patch_models_by_role, monkeypatch, install_crawler
):
    """R3: `verify_paragraphs` judges the claim against the CAPTURED page text, never the
    reader's digest -- even though the digest is all the lead itself ever saw.
    """
    result = await _run_delegation(make_config, patch_models_by_role, monkeypatch, install_crawler)

    head_model = result["head_model"]
    # Script order: task call, final synthesis, verify reply -- the verify call is the model's
    # LAST invocation.
    verify_messages = head_model._received_messages[-1]
    verify_text = _flatten(verify_messages)

    assert _CAPTURE_MARKER in verify_text
    assert _DIGEST_MARKER not in verify_text
