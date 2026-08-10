# PLAN: CI Pipeline (self-hosted GitHub Actions runner)

**Status:** Phases 1-4 complete; Final verification blocked on merging PR #2
**Created:** 2026-08-09
**Type:** Single plan

## Intent

**True goal:** Every pull request against `main` and every push to `main` is automatically
verified against the project's four quality gates plus a coverage floor, executing on
hardware the developer owns — so nothing red reaches `main`, and CI is positioned to reach
homelab-only services (such as the local SearXNG instance) in a later session.

**Binding outcomes:** (IDs stable forever)

- **R1** — A pull request targeting `main`, and a push to `main`, each trigger an automated
  check run whose pass/fail result is visible on the pull request.
- **R2** — The run fails if any of these fail: `pytest`, `ruff check`, `ruff format --check`,
  `mypy`.
- **R3** — The run fails if line coverage of the `harness` package is below 90%.
  - Coverage measures `harness/` only; `tests/` is excluded from the measured base.
- **R4** — Every CI job executes on the self-hosted runner `CI-Runner` on the Proxmox VM.
  No GitHub-hosted runner is used.
- **R5** — The runner survives a VM reboot unattended, and its configuration is recorded
  well enough to rebuild the VM from scratch.
  - The runner is already provisioned as a systemd service with default labels; this
    outcome is satisfied by verification plus a written record, not by new setup work.
- **R6** — A failed run is diagnosable from the GitHub web UI alone: which gate failed and
  why, without SSH access to the runner.
- **R7** — Runs are isolated from each other. Leftover state from a prior run (virtualenv,
  caches, working tree, stray processes) cannot change a later run's result.
  - A new push to the same pull request cancels the in-progress run for that ref.

**Preferences (NEGOTIABLE — this session or any future one may trim these on cost grounds
without re-asking):**

- Fast feedback via a warm `uv` cache on the runner rather than a cold dependency install
  every run.
- A single workflow file rather than several, unless a real reason to split appears.
- The GitHub job summary is readable at a glance — the failing gate obvious without opening
  raw logs.

**Non-goals:**

- Continuous deployment, releases, or any publish step.
- Security scanning, dependency scanning, secret scanning, Dependabot.
- Docker image build or push to a registry. (Named as a future direction by the developer;
  deliberately not this session.)
- Multi-version Python matrix — one Python version only.
- Configuring GitHub branch protection / required status checks. Workflows only; enforcement
  is the developer's later call.
- A GitHub-hosted runner fallback when the self-hosted runner is unavailable.
- Live or network-dependent integration tests. The suite stays offline and fixture-based
  this session.

**Constraints & assumptions:**

- Repository is **private**: `github.com/THEMANNICHOLAS/deep-research-harness`.
- Python >= 3.11, dependencies managed with `uv`. Toolchain is ruff (lint + format), mypy,
  pytest.
- One self-hosted Linux runner named `CI-Runner`, default labels only
  (`self-hosted`, `Linux`, `X64`), running as a systemd service on a Proxmox VM.
- Developer's workstation is Windows; the runner is Linux. `.gitattributes` exists in the
  repo.
- PR #1 (`feat/harness-substrate` -> `main`) is MERGED. `origin/main` carries the full
  37-file tree. Implementation branches from `origin/main` as `feat/ci-pipeline`.
  (The planning worktree's own branch holds only `README.md` — do not implement there.)
- First `uv sync` on the bare VM will pull `crawl4ai==0.9.2` and its tree: slow and large
  on run one, fast afterwards from `~/.cache/uv`. Accepted, no mitigation planned.
- When the runner is offline or busy, jobs queue and the pull request stays pending. This is
  accepted behaviour, not a failure to design around.
- **Confirmed by exploration:** the test suite is fully offline and fixture-based, so CI
  needs no secrets or API keys. `tests/test_fetch.py` monkeypatches `AsyncWebCrawler`;
  `tests/test_search.py` routes httpx through `httpx.MockTransport`. Nothing requires Docker,
  SearXNG, or a real browser.

**Open questions:**

- ~~Is `mypy` configured?~~ **Resolved:** yes. `pyproject.toml` has `[tool.mypy]` with
  `python_version = "3.12"`, and `mypy` is in the `dev` dependency group. `CLAUDE.md`'s
  Commands table and Quality Gate section both list `uv run mypy .`, matching `README.md`
  and `docs/guides/setup.md`.
- ~~Is `pytest-cov` already a dependency?~~ **Resolved:** no. Neither `pytest-cov` nor
  `coverage` is present, and no `[tool.coverage.*]` section exists. R3 requires adding it.
- **Still open:** actual line coverage of `harness/` today, relative to the 90% bar in R3.
  Every module has a dedicated test file and none is untested, so 90% looks plausible — but
  it is unmeasured. Phase 3 measures it and STOPS if it falls short, rather than shipping a
  permanently-red gate.

## Codebase Map

All facts below confirmed by reading files on `origin/main`.

- **Existing CI artifacts:** NONE. `.github/` contains only `pull_request_template.md`. No
  workflow, Makefile, `tox.ini`, `noxfile.py`, or pre-commit config anywhere in the tree.
- **Package layout:** `[tool.uv] package = false` — flat layout, not an installed
  distribution. `harness` is imported from the repo root, so `--cov=harness` /
  `source = ["harness"]` resolves against the checkout directory.
- **`pyproject.toml` today:**
  - `requires-python = ">=3.11"`
  - `dependencies`: `pydantic>=2.9`, `langchain-core>=0.3`, `crawl4ai==0.9.2` (pinned
    exactly; comment notes 0.9.x is a breaking series), `httpx>=0.27`
  - `[dependency-groups] dev = ["ruff", "mypy", "pytest", "pytest-asyncio"]` — **no
    `pytest-cov`, no `coverage`**
  - `[tool.pytest.ini_options]`: only `asyncio_mode = "auto"` and `testpaths = ["tests"]`.
    No `addopts`.
  - `[tool.mypy]`: `python_version = "3.12"`, `warn_unused_ignores = true`,
    `warn_redundant_casts = true`
  - `[tool.ruff]`: `target-version = "py311"`, `line-length = 100`;
    `[tool.ruff.lint] select = ["E", "F", "I", "UP"]`
  - No `[tool.coverage.*]` section exists.
- **Source modules** (`harness/`, ~742 lines total): `config.py` (136), `prompts.py` (41),
  `sources.py` (148), `tools/__init__.py` (16), `tools/fetch.py` (255), `tools/search.py`
  (145), `__init__.py` (1). Every one has a dedicated test file; none is untested.
- **Tests:** pytest + `pytest-asyncio`, in `tests/`. `tests/conftest.py` provides one
  fixture, `make_config`, and sets `OPENCODE_API_KEY` to a fake value via
  `monkeypatch.setenv`. No custom markers.
  - `tests/test_config.py::test_shipped_harness_toml_loads_with_its_todo_placeholders`
    reads the real committed `harness.toml` from disk. `harness.toml` contains three literal
    `"TODO"` placeholders (`providers.opencode.base_url`, `roles.head.model`,
    `roles.subagent.model`); the test asserts they load. This is by design — CI will pass.
  - `harness/config.py` demands env vars only inside `load_config()`, never at import time.
- **`uv.lock` is tracked** (not gitignored), so `uv sync --locked` is available and will
  fail if `pyproject.toml` is edited without re-locking.
- **`.gitignore`** ignores `.venv/`, `.mypy_cache/`, `.ruff_cache/`, `.pytest_cache/`,
  `.env`/`.env.*` (keeps `!.env.example`).
- **`.gitattributes`**: `* text=auto`, `*.sh text eol=lf`, `*.bat`/`*.ps1 text eol=crlf` —
  `.py` files normalize to LF on the Linux runner.
- **Commands as they work today** (identical in `README.md`, `CLAUDE.md`, and
  `docs/guides/setup.md`): `uv run pytest`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy .`
- **Docs:** `docs/INDEX.md` (documentation map), `docs/guides/setup.md` (the exemplar for a
  new operational guide), `docs/backlog.md` (no CI/testing entries today),
  `docs/decisions.md` (records the mypy-targets-3.12 decision and its numpy-stubs reason).

## Non-Goals

Inherits every `## Intent` non-goal — not re-listed. The design conversation additionally
ruled out:

- Splitting gates across multiple jobs, or any job-level parallelism or matrix. On a
  single runner these serialize and cost an extra checkout + sync each (D1).
- `actions/setup-python`. `uv` manages and downloads interpreters itself; adding it is a
  redundant action dependency (D3).
- A standalone `.coveragerc` or `setup.cfg`. All tool config stays in `pyproject.toml` (D4).
- Reporting coverage to an external service (Codecov and similar).
- Provisioning a second runner, or any change to the existing runner's registration.

## Design Decisions

### D1: Job topology — one job, sequential steps
- **Chosen:** A single job (`quality`) with one step per gate.
- **Rejected:** Separate `lint` / `typecheck` / `test` jobs — a single self-hosted runner
  processes one job at a time, so parallel jobs *serialize* while each pays its own
  checkout and `uv sync`. Strictly slower for zero concurrency gain.
- **Consequences:** Every later phase adds steps to this one job, never a second job.
  Revisit only if a second runner is added.

### D2: Gate failure behaviour — run all, then fail
- **Chosen:** Each gate step carries `if: ${{ !cancelled() }}`, so one run reports every
  failing gate. `!cancelled()` rather than `always()` so a cancelled run stops promptly
  (which matters for R7's cancel-in-progress).
- **Rejected:** Default fail-fast — cheapest on runner time, but surfaces one problem per
  push, forcing fix-push-wait cycles.
- **Consequences:** A doomed run costs a few extra seconds. Step ordering is cosmetic, not
  semantic.

### D3: Python version pinning — committed `.python-version`
- **Chosen:** `.python-version` containing `3.12`, read automatically by uv both locally
  and in CI.
- **Rejected:** Pinning only inside the workflow (CI deterministic, workstation still free
  to drift); `actions/setup-python` (redundant — uv manages interpreters).
- **Consequences:** 3.12 matches the existing `[tool.mypy] python_version` decision
  (`docs/decisions.md`: numpy stubs reached via crawl4ai use PEP 695 `type` syntax that is
  a syntax error under 3.11). `requires-python = ">=3.11"` is left untouched — the floor
  stays 3.11; only the *selected* interpreter is pinned.

### D4: Coverage threshold placement — config in pyproject, gate flag in CI
- **Chosen:** `[tool.coverage.run] source = ["harness"]` in `pyproject.toml`;
  `--cov-fail-under=90` passed only by the CI step.
- **Rejected:** `--cov-fail-under` in pytest `addopts` or `[tool.coverage.report]` — perfect
  local/CI parity, but then running a single test file locally fails the coverage check,
  which gets old fast.
- **Consequences:** Local `uv run pytest` stays fast and ungated. The 90 lives in exactly
  one place (the workflow); anyone changing it changes CI, deliberately.

### D5: uv delivery — `astral-sh/setup-uv`, SHA-pinned
- **Chosen:** The workflow installs uv via `astral-sh/setup-uv` pinned to a full commit SHA.
- **Rejected:** System-wide uv on the VM — no per-run fetch, but uv's version becomes an
  undocumented property of the box, and a rebuilt VM can silently differ.
- **Consequences:** Immune to the systemd PATH trap (the runner service does not source
  `~/.bashrc`, where a default uv install lands in `~/.local/bin`). A rebuilt VM needs only
  the runner installed. Bumping uv is a deliberate SHA change. Self-hosted runner minutes
  are free, so this costs nothing but a fetch.

### D6: Lock enforcement — `uv sync --locked`
- **Chosen:** `uv sync --locked`, which fails when `uv.lock` is out of date with
  `pyproject.toml`.
- **Rejected:** Plain `uv sync`, which would silently re-resolve and hide lock drift.
- **Consequences:** Free extra gate. Phase 3 MUST commit a regenerated `uv.lock` alongside
  the `pytest-cov` addition or CI breaks (see #4).

### D7: Runner documentation — new `docs/guides/ci.md`
- **Chosen:** A dedicated guide for runner install, systemd unit, labels, work directory,
  and rebuild steps.
- **Rejected:** Appending to `docs/guides/setup.md` — would mix developer-workstation setup
  with server infrastructure.
- **Consequences:** `docs/INDEX.md` gains an entry.

### D8: Implementation base — `feat/ci-pipeline` off `origin/main`
- **Chosen:** Branch from `origin/main`, which carries the full tree after PR #1 merged.
- **Rejected:** Branching from `feat/harness-substrate` (already merged, now redundant).
- **Consequences:** CI arrives as its own reviewable PR, and `ci.yml` is exercised by that
  very PR — the tracer bullet verifies itself. **Do not implement in the planning worktree**,
  whose branch holds only `README.md`.

## Requirements Coverage

| ID | Requirement | MoSCoW | Covered by |
|----|-------------|--------|------------|
| R1 | PR + push-to-main trigger a visible check run | MUST | Phase 1 (PR trigger), Phase 2 (push trigger) |
| R2 | Run fails on pytest / ruff check / ruff format / mypy | MUST | Phase 1 (ruff check), Phase 2 (remaining three) |
| R3 | Run fails below 90% line coverage of `harness/` | MUST | Phase 3 |
| R4 | All jobs execute on `CI-Runner`, never GitHub-hosted | MUST | Phase 1 |
| R5 | ~~Runner survives reboot; config recorded for rebuild~~ **DROPPED** | ~~MUST~~ | **NOT DELIVERED** — descoped 2026-08-09, see `## Reconciliations` |
| R6 | Failure diagnosable from the GitHub UI alone | MUST | Phase 2 |
| R7 | Runs isolated; new push cancels in-progress run | MUST | Phase 2 |

## Progress
- [x] Phase 1: Tracer bullet — prove CI-Runner executes a job
- [x] Phase 2: Full gate set
- [x] Phase 3: Coverage gate
- [x] Phase 4: Record the runner — **descoped to a one-line note; R5 dropped**
- [~] Final verification — all items pass EXCEPT the `push: main` trigger, which needs
      PR #2 merged (developer is reviewing first)

## Phases

### Phase 1: Tracer bullet — prove CI-Runner executes a job

**Risk:** flagged (!#1)
**Test-first:** N/A — this phase produces a workflow file and a version pin; there is no
code surface to unit-test. A real PR run on the runner is the gate.
**Goal:** Prove `CI-Runner` can execute a GitHub Actions job end to end — checkout, uv
install, locked sync, one lint gate — before any further gates are built on that assumption.
**Requirements:** R4; R1 (PR trigger half), R2 (ruff check only)

**Assumes:**
- `CI-Runner` is registered to `THEMANNICHOLAS/deep-research-harness`, online, and its
  systemd service is enabled.
- The runner host has `git` and outbound HTTPS to `github.com` and `pypi.org`.

**Files:**
- `.github/workflows/ci.yml` — new. Reason: the deliverable; no workflow exists anywhere
  in the tree.
- `.python-version` — new. Reason: pins the interpreter uv selects so runner and
  workstation agree (D3). One line.

**Reuse:**
- `none — new surface`. Confirmed: no workflow, Makefile, tox/nox, or pre-commit config
  exists to extend.

**Contracts:**
- Workflow path `.github/workflows/ci.yml`; workflow `name: CI`; single job id `quality`.
  Phases 2 and 3 add steps to THIS job — never a second job (D1).
- `runs-on: [self-hosted, Linux, X64]` — matches `CI-Runner`'s default labels (R4).
- `.python-version` content: `3.12`
- `astral-sh/setup-uv` is referenced by full commit SHA with the human-readable version in
  a trailing comment (D5).

**Out of scope:**
- The other three gates (`ruff format --check`, `mypy`, `pytest`) — Phase 2.
- `push: main` trigger, `concurrency`, `timeout-minutes` — Phase 2.
- Any `pyproject.toml` or `uv.lock` change, and anything coverage-related — Phase 3.
- Any file under `harness/` or `tests/`.
- Branch protection / required status checks — plan non-goal.

**Manual verification:**
- [x] Push `feat/ci-pipeline` and open a PR to `main`; the Actions run's job page names
      `CI-Runner` as the executor — not a GitHub-hosted runner.
      *PR #2; run 31342810201 job `quality` → `runner_name: CI-Runner`.*
- [x] `uv sync --locked` succeeds with no lockfile-drift error. *`Resolved 117 packages in 0.71ms`.*
- [x] The sync log shows Python 3.12 selected, confirming `.python-version` took effect.
      *`Using CPython 3.12.3 interpreter at: /usr/bin/python3.12`.*
- [x] **Prove the gate can fail:** push a commit adding an unused import, confirm the run
      goes red at the `ruff check` step, then revert and confirm green. A gate that has
      never failed is unverified.
      *Run 31342882589 (`58e3560`) failed at step 5 `ruff check`; revert `65905b3` →
      run 31342921361 green in 7s. Probe was `ci_probe.py` at repo root, not under
      `harness/`/`tests/` (out of scope).*

**Steps:**
1. Create branch `feat/ci-pipeline` from `origin/main`.
2. Add `.python-version` containing `3.12`.
3. Resolve the current `astral-sh/setup-uv` release and record its full commit SHA.
4. Write `.github/workflows/ci.yml`: `on.pull_request.branches: [main]`; job `quality` per
   Contracts; steps checkout -> setup-uv (SHA-pinned) -> `uv sync --locked` ->
   `uv run ruff check .`.
5. Push, open the PR, and work the Manual verification list.

**Acceptance criteria:**
- [x] The check run appears on the PR and was executed by `CI-Runner`.
- [x] A deliberate lint violation turns the run red; removing it turns it green.
- [x] No `uv: command not found` — the systemd PATH trap is confirmed bypassed (D5).
      *`Successfully installed uv version 0.12.3` — setup-uv delivered uv per-run.*

**Diff budget:** ~30-45 lines across 2 new files.

### Phase 2: Full gate set

**Risk:** flagged (!#2)
**Test-first:** N/A — workflow configuration only; no code surface. Manual verification on
a live PR is the gate.
**Goal:** `ci.yml` runs all four quality gates, reports every failure in a single run, and
triggers on both PRs to `main` and pushes to `main`.
**Requirements:** R1, R2, R6, R7

**Assumes:**
- Phase 1 is green — the runner demonstrably executes jobs.

**Files:**
- `.github/workflows/ci.yml` — modify: three added gate steps, `push` trigger, a
  `concurrency` block, and `timeout-minutes`.

**Reuse:**
- Extend the `quality` job from Phase 1 — do NOT add a second job or a matrix (D1).
- Pattern to mirror: Phase 1's `ruff check` step — same `name:` + single-line `run:` shape.

**Contracts:**
- The four gate commands are frozen and MUST match `CLAUDE.md` and `docs/guides/setup.md`
  character-for-character: `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy .`, `uv run pytest`. (Phase 3 appends coverage flags to the last one; the
  others never change.)
- `concurrency.group` keys on the ref so runs for different PRs never cancel each other.

**Out of scope:**
- ~~Coverage flags or any `pyproject.toml` change — Phase 3.~~ **AMENDED — see
  `## Reconciliations` (2026-08-09).** Coverage flags remain Phase 3, but ONE `pyproject.toml`
  change is now in scope for Phase 2: `[tool.uv] required-version`, pinning the uv binary.
- **Editing `harness/`, `tests/`, `[tool.mypy]`, or `[tool.ruff]` to make a gate pass.** If
  mypy or ruff fails on existing code in the clean Linux/3.12 environment, STOP and surface
  it — loosening a tool's config to get green is drift, not a fix (see #2).
- Splitting into multiple jobs, adding a matrix, or adding a second workflow file.
- `docs/` updates — Phase 4.

**Manual verification:**
- [x] Push a commit that violates formatting AND introduces a type error; **one** run
      reports both steps as failed, not just the first (D2, R6).
      *Run 31343717168 (`cb3f992`): `ruff check` success, `ruff format --check` FAILURE,
      `mypy` FAILURE, `pytest` success — both failures in one run, and `pytest` still ran
      downstream of them.*
- [x] Push again to the same PR while a run is in progress; the superseded run shows as
      cancelled (R7). *Run 31343905762 (`d1870a0`) → `conclusion: cancelled` when `dc2b807`
      was pushed; the superseding run 31343912854 then completed green in 23s. Took a
      scripted push to land inside the window — runs finish in ~23s, so two pushes issued
      from separate commands are either too fast (no run created yet) or too slow.*
- [x] Each failing step's name identifies which gate failed from the run summary alone,
      without opening raw logs (R6). *Step names are the gate commands verbatim —
      `ruff format --check`, `mypy`.*
- [x] All four gates green on clean `HEAD`. *Run 31343617566 (`1935293`), 24s.*

**Steps:**
1. Add `ruff format --check`, `mypy`, and `pytest` steps, each with `if: ${{ !cancelled() }}`.
2. Add `on.push.branches: [main]`.
3. Add the `concurrency` block with `cancel-in-progress: true`.
4. Add `timeout-minutes` to the job so a wedged run fails loudly instead of holding the
   only runner indefinitely.
5. Work the Manual verification list on the live PR.

**Acceptance criteria:**
- [x] A single run surfaces every failing gate, not only the first. *Run 31343717168:
      `ruff format --check` and `mypy` both FAILURE, `pytest` still ran and passed.*
- [x] A superseded run on the same PR is cancelled automatically. *Run 31343905762.*
- [ ] The `push: main` trigger fires after the PR merges (verify at Final verification).
- [x] No tool configuration was loosened to achieve green. *All four gates passed on the
      clean Linux/3.12 runner unmodified — `[tool.mypy]`, `[tool.ruff]`, `harness/` and
      `tests/` were never touched. Risk #2 did not materialize.*

**Diff budget:** ~25-40 lines, 1 file modified.

### Phase 3: Coverage gate

**Risk:** flagged (!#3, !#4)
**Test-first:** N/A — this phase adds tooling and configuration, not behaviour. The
coverage measurement itself is the gate, and writing tests here would corrupt the very
number being measured.
**Goal:** Coverage of `harness/` is measured in CI and the run fails below 90% — or, if
actual coverage falls short, the shortfall is surfaced for an R3 decision rather than
absorbed.
**Requirements:** R3

**Assumes:**
- Phase 2 is green — all four gates run and can fail.

**Files:**
- `pyproject.toml` — modify: add `pytest-cov` to `[dependency-groups] dev`; add
  `[tool.coverage.run]` / `[tool.coverage.report]`.
- `uv.lock` — modify: regenerated by `uv lock`. Machine-generated; must be committed (D6).
- `.github/workflows/ci.yml` — modify: the pytest step gains coverage flags.

**Reuse:**
- Extend the existing `[dependency-groups] dev` list and add coverage config alongside the
  existing `[tool.mypy]` / `[tool.ruff]` / `[tool.pytest.ini_options]` sections — do NOT
  create `.coveragerc` or `setup.cfg` (D4).
- Pattern to mirror: `pyproject.toml`'s existing tool sections — all config in one file.

**Contracts:**
- `[tool.coverage.run] source = ["harness"]` — the measured base. `tests/` is excluded
  (R3). This is the only place the measured scope is defined.
- CI pytest step: `uv run pytest --cov --cov-report=term-missing --cov-fail-under=90`. The
  threshold lives in the workflow, NOT in `pyproject.toml` (D4), so local `uv run pytest`
  stays ungated.

**Out of scope:**
- **Writing tests solely to inflate the number, or lowering 90 to match reality.** If
  measured coverage is under 90%, STOP at step 4 and surface it — R3 is renegotiable by the
  developer, not by the implementor (see #3).
- Branch coverage / `--cov-branch` — R3 specifies line coverage only.
- External coverage reporting (Codecov and similar) — plan non-goal.
- Any change under `harness/`.

**Manual verification:**
- [x] Run `uv run pytest --cov --cov-report=term-missing` locally and record the actual
      percentage, with the date, in `## Discoveries`. *99% (376 stmts, 4 missed) — see the
      2026-08-09 Phase 3 entry there for the per-module table.*
- [x] **Prove the gate can fail:** temporarily set `--cov-fail-under=100`, confirm the CI
      run goes red at the pytest step, then restore 90 and confirm green.
      *Run 31344747771 (`f7f23b2`) failed at `pytest` with "ERROR: Coverage failure: total of
      99 is less than fail-under=100" — the other three gates stayed green and the 96 tests
      themselves passed, so the failure was the threshold alone. Restored to 90.*
- [x] `uv sync --locked` still succeeds in CI — proves the regenerated `uv.lock` was
      actually committed (#4). *Run 31344687936: "Resolved 120 packages" (was 117).*
- [x] Plain `uv run pytest` locally still runs with no coverage gate (D4). *96 passed in
      1.22s, no coverage output.*

**Steps:**
1. Add `pytest-cov` to the `dev` dependency group.
2. Run `uv lock` and commit the regenerated `uv.lock` in the same commit — omitting it
   breaks `uv sync --locked` (#4).
3. Add `[tool.coverage.run] source = ["harness"]` and a `[tool.coverage.report]` section.
4. Measure locally. **If under 90%, STOP and surface to the developer — do not proceed.**
5. Add the coverage flags to the CI pytest step.
6. Work the Manual verification list.

**Acceptance criteria:**
- [x] Actual measured coverage, with date, recorded in `## Discoveries`. *99%, 2026-08-09.*
- [x] `--cov-fail-under=100` turns the run red; 90 turns it green. *Runs 31344747771 (red)
      and 31344687936 / the restore run (green).*
- [x] `uv sync --locked` passes on the runner after the dependency change. *Run 31344687936.*
- [x] The 90 appears in exactly one place in the repo (the workflow file). *Verified by grep:
      both hits are in `.github/workflows/ci.yml` (the flag and its adjacent comment).*

**Diff budget:** ~15-25 lines across `pyproject.toml` and `ci.yml`, plus a regenerated
`uv.lock` (machine-generated, not counted).

### Phase 4: Record the runner

**Risk:** none
**Test-first:** N/A — documentation only; nothing executable is produced.

> **DESCOPED 2026-08-09 — see `## Reconciliations`.** Everything struck through below was
> replaced by a single line in `docs/INDEX.md` noting that CI runs on a self-hosted GitHub
> Actions runner with default tags. **R5 is not delivered.**

~~**Goal:** `docs/guides/ci.md` records the runner's real configuration well enough to rebuild
the VM from scratch.~~
**Goal (amended):** `docs/INDEX.md` states that CI runs on a self-hosted runner with the
default tags. Nothing about the runner host is recorded.
~~**Requirements:** R5~~ **Requirements (amended):** none.

**Files:**
- ~~`docs/guides/ci.md` — new. Reason: R5's written record; `docs/guides/` is the established
  home for operational guides and `setup.md` is its exemplar (D7).~~ **Not created.**
- `docs/INDEX.md` — modify: add a `**CI:**` bullet to `## Project Context`. No
  Documentation-Map row, since no guide exists to link.

**Reuse:**
- Pattern to mirror: `docs/guides/setup.md` — same heading structure and command-block
  style. Follow the project doc rule: 1-3 sentences per entry, reference files as
  `@path/to/file`, literal commands only (no code blocks beyond commands).
- Link to `docs/guides/setup.md` for developer-workstation setup rather than restating it.

**Out of scope:**
- Editing `CLAUDE.md`, `README.md`, or `docs/guides/setup.md`. All three already document
  the four gate commands accurately; CI does not change them.
- Documenting deployment, Docker, or registry work — plan non-goal, even though the
  developer named it as a future direction.
- Changing the runner's registration, labels, or service configuration. This phase
  OBSERVES and RECORDS; it does not reconfigure.

**Manual verification (amended):**
- ~~[ ] `systemctl is-enabled <runner-service>` on the VM returns `enabled`~~ **Dropped —
  never run; reboot survival is unverified.**
- ~~[ ] A cold reader can follow `docs/guides/ci.md` to re-register a runner~~ **Dropped —
  no guide exists.**
- [x] `docs/INDEX.md` names the runner and its default tags.

**Steps (amended):**
1. ~~Capture the runner's configuration on the VM.~~ **Dropped.**
2. ~~Write `docs/guides/ci.md`.~~ **Dropped.**
3. Add a `**CI:**` bullet to `docs/INDEX.md` `## Project Context`.

**Acceptance criteria (amended):**
- [x] `docs/INDEX.md` states that CI runs on a self-hosted GitHub Actions runner with the
      default tags, and points at the Reconciliations entry explaining why the runner's own
      configuration is not recorded.

**Diff budget:** ~5 lines, 1 file modified (was ~45-65 across 2 files).

## Verification

- [x] Open the `feat/ci-pipeline` PR against `main`; the check run executes on `CI-Runner`
      and reports green. *PR #2, run 31342810201 onward.*
- [x] All four gate commands pass on the runner, matching local results:
      `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`,
      `uv run pytest`. *Run 31343617566; coverage identical on Linux and Windows (99%).*
- [x] Coverage gate active: `uv run pytest --cov --cov-report=term-missing
      --cov-fail-under=90` passes in CI. *Run 31344687936.*
- [x] Each gate has been observed FAILING at least once and recovering (Phases 1, 2, 3
      Manual verification) — no gate ships unproven. *`ruff check` 31342882589;
      `ruff format --check` + `mypy` 31343717168; `pytest` 31344747771.*
      **Caveat:** the `pytest` step was driven red by the coverage threshold
      (`--cov-fail-under=100`), never by a genuinely failing test. The step is proven able
      to fail; a red *test* has not been observed in CI.
- [ ] **OUTSTANDING** — After merging the PR, the `push: main` trigger fires and the run on
      `main` is green (R1's second half). *Deliberately not done: developer chose to review
      PR #2 before merging (2026-08-09). The `push:` trigger is present and syntactically
      exercised, but has never actually fired. This is also Phase 2's one unticked
      acceptance criterion.*
- ~~[ ] `docs/guides/ci.md` exists, is linked from `docs/INDEX.md`, and records observed
      values.~~ **Dropped 2026-08-09 — see `## Reconciliations`. Replaced by: `docs/INDEX.md`
      names the self-hosted runner and its default tags.**

## Notes

- **The tracer bullet verifies itself.** The `feat/ci-pipeline` PR is the first thing
  `ci.yml` runs against, so Phase 1's workflow is exercised by the very PR that introduces
  it. No separate throwaway PR is needed.
- **`.python-version` pins the selected interpreter, not the floor.** Leave
  `requires-python = ">=3.11"` alone; the project still supports 3.11, CI just doesn't
  exercise it (matrix is a plan non-goal).
- **Workspace cleaning is mostly free.** `actions/checkout` runs `git clean -ffdx` on an
  existing workspace, wiping `.venv/` and the tool caches every run, while uv's package
  cache at `~/.cache/uv` sits outside the workspace and survives. That satisfies most of R7
  and the warm-cache preference with no extra workflow steps. It does NOT kill stray
  processes — acceptable here because the suite is fully offline and spawns no servers.
- **`harness.toml`'s `TODO` placeholders are not a CI problem.** A test asserts they load
  successfully by design. Do not "fix" them.
- If the runner is offline when a PR opens, the job simply queues and the PR stays pending.
  This is accepted behaviour (Intent constraint), not something to work around.

## Risks

#1. **The runner may not be able to execute a job at all** — a self-hosted runner running as
    a systemd service does not source `~/.bashrc`, so a default `uv` install in
    `~/.local/bin` is invisible to it; `git` may be missing; outbound HTTPS may be blocked.
    D5 (`astral-sh/setup-uv`) is the chosen mitigation for the PATH half. Phase 1 exists
    solely to retire this risk before three more gates are built on top of it. If Phase 1
    fails, diagnose on the VM before touching the workflow further — do not add fallback
    logic or a GitHub-hosted runner (plan non-goal).

#2. **`mypy` or `ruff` may fail on existing code in the clean Linux/3.12 CI environment**
    even though they pass on the developer's Windows workstation — different interpreter
    selection, different transitive stub versions (the numpy/PEP-695 issue recorded in
    `docs/decisions.md` is precisely this class of problem), and LF vs CRLF normalization.
    The temptation is to loosen `[tool.mypy]`/`[tool.ruff]` or edit `harness/` to get green.
    Phase 2 forbids both: STOP and surface instead. A real pre-existing defect found by CI
    is CI working, and fixing it is separate work.

#3. **Actual coverage may be below the 90% bar in R3.** It is unmeasured — every module has
    a dedicated test file and none is untested, so 90% looks plausible, but `harness/tools/
    fetch.py` alone is 255 lines and nothing proves its coverage clears the bar. Two wrong
    responses: quietly lower the threshold, or pad the suite with assertion-free tests to
    inflate the number. Phase 3 step 4 stops instead and hands the decision back — R3 is
    explicitly renegotiable by the developer.

#4. **Forgetting to re-lock in Phase 3 breaks CI.** Adding `pytest-cov` to `pyproject.toml`
    without running `uv lock` and committing the regenerated `uv.lock` makes
    `uv sync --locked` fail on every subsequent run — including runs unrelated to coverage.
    The failure message points at the lockfile, not at the missing step, so it reads as
    mysterious. Phase 3 step 2 makes the re-lock explicit and same-commit.

## Reconciliations

<!-- Drift amendments written by /implement during execution. Append-only. Outdated phase
text above is struck through (~~...~~) but preserved; entries here are the authoritative
correction. Empty at plan creation. -->

### 2026-08-09 — Phase 4 descoped; R5 is NOT delivered by this plan

**What contradicted the plan:** Phase 4 (and D7) called for a new `docs/guides/ci.md`
recording the runner's observed configuration — systemd unit name, work directory, labels,
runner version, OS version, and `systemctl is-enabled` output — to satisfy R5. Its single
acceptance criterion required every value to be OBSERVED on the VM, which needed either SSH
access to the runner host or a temporary CI probe step.

**Developer decision (2026-08-09):** recording the runner is not needed. Note only that CI
runs on a self-hosted GitHub Actions runner with default tags.

**Authoritative correction:** Phase 4 produces NO `docs/guides/ci.md` and D7 does not apply.
It adds one line to `docs/INDEX.md` naming the runner and its default labels. `docs/INDEX.md`
gains no Documentation-Map row, because no guide is created.

**Consequence — R5 is explicitly NOT satisfied.** Both halves fall away: no configuration is
recorded well enough to rebuild the VM from scratch, and reboot survival was never verified
(`systemctl is-enabled` was never run — the runner's systemd unit name remains unobserved).
R1-R4, R6 and R7 are unaffected and remain delivered. If the VM is ever rebuilt, the runner
must be re-registered from GitHub's own documentation, and the uv pin
(`required-version = "==0.12.3"`) plus the setup-uv SHA are then the only project-side facts
needed — both live in the repo. Logged to `docs/backlog.md` for a future session.

### 2026-08-09 — Phase 2 scope amended to permit one `pyproject.toml` change

**What contradicted the plan:** Phase 1 proved D5's consequence false — SHA-pinning
`astral-sh/setup-uv` does not pin the uv binary, which falls back to latest (see
`## Discoveries`). The fix belongs in `[tool.uv] required-version` in `pyproject.toml`, the
file setup-uv already probes. But Phase 2's `**Out of scope:**` read "any `pyproject.toml`
change — Phase 3", so the fix had nowhere to land in Phase 2.

**Authoritative correction:** Phase 2 MAY edit `pyproject.toml` for the single purpose of
adding `[tool.uv] required-version = "==0.12.3"`. Everything else about `pyproject.toml` —
`pytest-cov`, `[tool.coverage.*]`, coverage flags — remains Phase 3. Phase 3's re-lock
obligation (#4) is unchanged and now also covers any lock impact from this pin.

**Developer decision (2026-08-09):** approved, with the standing instruction that **all
requirements in this project be pinned exactly (`==`), never `>=`** — a `>=` floor still lets
the resolved version float, which is the reproducibility gap this pin exists to close.
Converting the remaining `>=` dependencies (`pydantic`, `langchain-core`, `httpx`, and the
unpinned `dev` group) is NOT part of this plan — logged for a future session.

## Discoveries

<!-- Non-contradictory findings logged by /implement during execution (act / defer / drop).
Append-only, empty at plan creation. -->

### 2026-08-09 — Phase 1 — `ruff check` step needs the D2 guard retrofitted (DEFERRED to Phase 2)

Phase 1's `ruff check` step ships without `if: ${{ !cancelled() }}`. D2 requires the guard on
every gate step, but Phase 2's Steps say only to add it to the three *new* steps — so as
written, Phase 2 would leave the first gate unguarded and a `ruff check` failure would skip
the other three, defeating D2 and R6. Harmless while `quality` has a single gate.
**Phase 2 must apply `if: ${{ !cancelled() }}` to all FOUR gate steps, not just the three it
adds.** Developer decision: defer to Phase 2.

### 2026-08-09 — Phase 3 — measured coverage of `harness/` is 99% (R3 bar cleared)

Measured on 2026-08-09 with `uv run pytest --cov --cov-report=term-missing` on Windows,
CPython 3.12.13, pytest-cov 7.1.0 / coverage 7.15.4, against `[tool.coverage.run]
source = ["harness"]`:

| Module | Stmts | Miss | Cover |
|---|---|---|---|
| `harness/__init__.py` | 0 | 0 | 100% |
| `harness/config.py` | 80 | 0 | 100% |
| `harness/prompts.py` | 23 | 0 | 100% |
| `harness/sources.py` | 72 | 0 | 100% |
| `harness/tools/__init__.py` | 7 | 0 | 100% |
| `harness/tools/fetch.py` | 123 | 3 | 98% (missing 197-209) |
| `harness/tools/search.py` | 71 | 1 | 99% (missing 53) |
| **TOTAL** | **376** | **4** | **99%** (98.94%) |

**Risk #3 did not materialize** — 99% against a 90% bar, so nothing was renegotiated, no
threshold was lowered, and no assertion-free tests were added to inflate the figure. The
margin is ~34 statements: coverage would have to lose that many before the gate bites.
Only four statements are unexercised, both regions being error/edge paths.

### 2026-08-09 — Phase 1 — SHA-pinning setup-uv does not pin uv itself (ACT in Phase 2)

Run 31342810201 logged: *"Could not determine uv version from uv.toml or pyproject.toml.
Falling back to latest"*, then fetched uv **0.12.3**. The workstation runs uv **0.11.6**. So
D5's consequence — "Bumping uv is a deliberate SHA change" — is **false as implemented**: the
SHA pins the *action*, not the *uv binary*, which floats to latest on every run. Phase 1's
Contract (SHA + trailing version comment) was met exactly; the gap is in D5's rationale, and it
weakens R5's rebuild-the-VM story. Developer decision: pin uv via `required-version` under
`[tool.uv]` in `pyproject.toml` — the file setup-uv already probes, and the only option that
also constrains the workstation — folded into **Phase 2**. Exact specifier TBD there
(see the open note in the Phase 1 handoff entry).

## Phase Handoff Log

<!-- Written by /implement at each 3G phase gate (Done / Learned / Drift / Watch-next per
phase). Append-only, empty at plan creation. MUST remain the LAST section of this file:
/implement's Step 2 reads the plan up to this heading plus only the log's final entry, so
never add a section below it. -->

### 2026-08-09 — Phase 1: Tracer bullet
- Done: `.github/workflows/ci.yml` (job `quality`, PR-to-main trigger, checkout → setup-uv →
  `uv sync --locked` → `uv run ruff check .`) and `.python-version` = 3.12. Both actions
  SHA-pinned (checkout v7.0.1 `3d3c42e5`, setup-uv v9.0.0 `c771a70e`) — the checkout pin was a
  developer addition beyond the plan's Contracts. Commit `c206bc1`; PR #2 opened.
  Risk #1 is RETIRED: `CI-Runner` executed all three runs, uv installed per-run, git 2.43.0
  present, outbound HTTPS fine.
- Learned: (a) setup-uv does NOT pin the uv binary — it logs "Could not determine uv version
  from uv.toml or pyproject.toml. Falling back to latest" and fetched uv 0.12.3, while the
  workstation runs 0.11.6. D5's consequence "bumping uv is a deliberate SHA change" is false as
  written; see `## Discoveries`. (b) The runner's `~/.cache/uv` was already warm — the feared
  slow first `crawl4ai` sync never materialized; whole job was 14s, 7s once warm. (c) The
  runner resolves 3.12 to the VM's system `/usr/bin/python3.12` (3.12.3), not a uv-downloaded
  build; workstation downloaded 3.12.13. Same minor, different patch — fine for now, worth
  knowing if a 3.12.x-specific issue ever appears. (d) All four gates already pass locally on
  3.12, so Phase 2's risk #2 is partly de-risked — Linux-vs-Windows remains the untested half.
- Drift: none. (One `## Discoveries` entry deferred to Phase 2: the `ruff check` step still
  needs `if: ${{ !cancelled() }}`.)
- Watch-next: Phase 2 MUST add `if: ${{ !cancelled() }}` to all FOUR gate steps — the existing
  `ruff check` step included, not just the three new ones. Phase 2's Steps as written only
  cover the new three, which would leave D2/R6 unsatisfied.

### 2026-08-09 — Phase 2: Full gate set
- Done: all four gates now run in the one `quality` job, each guarded by
  `!cancelled() && steps.sync.outcome == 'success'`; `push: main` trigger; `concurrency`
  keyed on `github.ref` with `cancel-in-progress` limited to pull requests;
  `timeout-minutes: 15`. `[tool.uv] required-version = "==0.12.3"` pins the uv binary
  (scope amendment — see `## Reconciliations`). Commits `1935293` + this one.
  **Risk #2 is RETIRED** — mypy and ruff both pass on the clean Linux/3.12 runner with no
  config loosened and nothing under `harness/`/`tests/` touched.
- Learned: (a) The uv pin works end to end — CI now logs "Found version for uv in
  .../pyproject.toml: 0.12.3" instead of "Falling back to latest". `uv lock --check` still
  passes, so `required-version` needs NO re-lock; and a deliberately mismatched value makes
  uv refuse to run, so the constraint is live, not decorative. Local uv was upgraded
  0.11.6 → 0.12.3 to satisfy it — **any clone now needs exactly 0.12.3**. (b) Two review
  fixes beyond the plan's Steps: `!cancelled()` overrides the implicit `success()`, so
  without the added sync condition a lock-drift failure would show four GREEN gates over a
  red sync (`uv run` auto-syncs without `--locked`); and `cancel-in-progress` had to be
  scoped to pull requests so two quick merges cannot leave a `main` commit verdict-less.
  (c) Runs complete in ~23s, which makes R7's cancellation hard to observe — two pushes
  issued as separate commands are either too fast (GitHub creates no run for the first) or
  too slow (it already finished). It took a single script that polls for run creation then
  pushes immediately. Reuse that approach if R7 ever needs re-proving.
- Drift: one — Phase 2's `**Out of scope:**` ban on `pyproject.toml` changes was amended by
  developer decision to admit `[tool.uv] required-version`. See `## Reconciliations`
  (2026-08-09). Five empty R7-probe commits were dropped from the branch afterwards
  (`git reset --hard 1960573` + force-push, developer-approved); the Actions runs they
  produced remain valid evidence.
- Watch-next: Phase 4's `docs/guides/ci.md` MUST document the `required-version = "==0.12.3"`
  pin alongside the setup-uv SHA — a clone with any other uv version hard-errors on every uv
  command, and nothing currently tells a reader that. Also note Phase 3 now inherits a
  `pyproject.toml` that already has a `[tool.uv]` block; its re-lock obligation (#4) is
  unchanged and still applies to the `pytest-cov` addition.

### 2026-08-09 — Phase 3: Coverage gate
- Done: `pytest-cov==7.1.0` in the dev group, `[tool.coverage.run] source = ["harness"]` +
  `[tool.coverage.report] show_missing = true`, regenerated `uv.lock` in the SAME commit,
  and `--cov --cov-report=term-missing --cov-fail-under=90` on the CI pytest step. `.coverage`
  added to `.gitignore`. Commits `86a5205`, `f7f23b2` (probe), and the restore.
  **Both flagged risks retired:** #3 — measured coverage is 99% against a 90% bar, so nothing
  was renegotiated and no padding tests were written; #4 — the re-lock happened in-commit and
  CI proved it by resolving 120 packages (was 117) under `uv sync --locked`.
- Learned: (a) Linux and Windows report identical coverage (99%, 376 stmts, 4 missed), so the
  gate is not platform-sensitive; the margin is ~34 statements before 90% is at risk.
  (b) `uv lock` pulled in `tomli` under a `python_full_version <= '3.11'` marker — coverage
  needs it to read TOML config on a 3.11 clone; harmless, but it explains why the lock grew by
  3 packages for one dependency. (c) The `implement-commit-guard.sh` hook is genuinely wired in
  this repo — it blocked the probe commit until the 3G window was opened, which is worth
  knowing before assuming a commit failure is a git problem.
- Drift: none. Two review fixes applied within scope: `.coverage` added to `.gitignore` (the
  only tool artifact from this phase the file did not already cover), and a `pyproject.toml`
  comment reworded so the literal threshold appears only in the workflow, restoring this
  phase's own "90 in exactly one place" criterion.
- Watch-next: Phase 4 is documentation-only and needs facts OBSERVED on the VM over SSH —
  systemd unit name, work directory, labels, runner version, `systemctl is-enabled` output.
  The runner is `CI-Runner`, id 2, runner version 2.336.0, labels `self-hosted, Linux, X64`,
  work dir `/home/sting/actions-runner/_work` (seen in CI logs), user `sting`, and the VM has
  system Python 3.12.3 at `/usr/bin/python3.12` and git 2.43.0 — but the systemd unit name and
  `is-enabled` state have NOT been observed yet and must not be guessed. Also carry forward
  from Phase 2: `ci.md` must document the `required-version = "==0.12.3"` pin.

### 2026-08-09 — Phase 4: Record the runner (descoped)
- Done: one `**CI:**` bullet added to `docs/INDEX.md` `## Project Context`, naming the
  self-hosted runner and its default tags and pointing at the Reconciliations entry. No
  `docs/guides/ci.md` was created; D7 does not apply.
- Learned: the runner's configuration was never observable from this session — the CI logs
  give the runner name, version, labels, work directory, user, git and Python versions, but
  the systemd unit name and `systemctl is-enabled` output need either SSH to the VM or a
  temporary read-only step in the workflow. Neither was run.
- Drift: yes, and it drops a MUST — see `## Reconciliations` (2026-08-09, Phase 4).
  **R5 is NOT delivered**: nothing records the runner well enough to rebuild the VM, and
  reboot survival is unverified. Logged to `docs/backlog.md`. The Phase 2 carry-forward about
  documenting the uv pin in `ci.md` is void along with the guide — the pin is still described
  in a comment at its definition site in `pyproject.toml`.
- Watch-next: Final verification is the only thing left — merge PR #2 and confirm the
  `push: main` trigger fires and goes green. That is Phase 2's one still-unticked acceptance
  criterion.
