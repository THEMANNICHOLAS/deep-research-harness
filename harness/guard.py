"""Heuristic prompt-injection scanner over untrusted text.

`scan` checks text against five signal families — `instruction_override`,
`role_spoofing`, `ai_directed`, `obfuscation`, `exfil_markup` — and reports which of
them fired. Detection is syntactic-only: it catches recognizable attack phrasing, not
semantic steering, which is contained by R2/R4 (strict URL provenance, structural tool
isolation) rather than detected here. Rules exist only where a fixture in
tests/fixtures/injection/ fires them (see PLAN-prompt-injection-defense.md Phase 2) —
this module does not invent coverage beyond what is measured.

Two families are shape-qualified rather than marker-only (PLAN-research-throughput D2/R3):
`role_spoofing`'s `system` markers require directive context — an instruction aimed at the
reader on the same or the next line — so a YAML `system:` key, an INI `[system]` header and a
`System: Ubuntu 22.04` spec line survive; `exfil_markup` requires the zero-click image form
`![..](..?token=)` or a query value carrying template syntax, so an ordinary docs link with
`?apikey=` in its query string survives.
"""

import re
import secrets

from pydantic import BaseModel, ConfigDict

# Stable family order (Phase 2 `## Contracts`, frozen) — also the order `signals`
# lists fired families in. Every regex below is justified by a fixture in
# tests/fixtures/injection/ that it must fire on (see that dir's README); none are
# speculative additions beyond what the fixture set measures.
FAMILY_ORDER = [
    "instruction_override",
    "role_spoofing",
    "ai_directed",
    "obfuscation",
    "exfil_markup",
]

# The zero-width set (U+200B, U+200C, U+FEFF) consumed by `_INVISIBLE_RE` (stripping) alone
# now (Phase 2 D5): `scan` strips invisibles before matching, so a separate detection rule over
# this same set would never fire — stripping is what defeats zero-width obfuscation, not a
# regex over its presence. One constant either way, since the sets already drifted once (ZWJ,
# added and removed per the plan's Discoveries log).
_ZERO_WIDTH_CHARS = "​‌﻿"

# Second-person or bare-imperative instruction vocabulary. A `system` marker only counts as
# role spoofing when one of these follows it on the same or the next line (PLAN-research-throughput
# D2): the marker alone is also how config files, spec sheets and YAML keys are written, and
# dropping those pages was the false positive this qualifier removes.
_DIRECTIVE = (
    r"(?:you are|you must|you will|your task|ignore|disregard|override|never|do not|don'?t"
    r"|act as|comply)"
)

# The directive-context window shared by BOTH role_spoofing `system` markers (D2): the
# directive sits on the marker's own line, or after one line hop plus at most three
# blank-line skips. ONE composed constant, not a suffix hand-pasted into each pattern —
# the "same or next line" recall floor is policy (risk #3), and a pasted suffix let a
# tuning of the hop land in one copy while the other silently kept the old floor. Every
# quantifier is bounded (issue #43 #3): an unbounded same-line reach let a page of
# `[system]` markers with no directive restart an O(remaining-line) lazy scan at EVERY
# occurrence — measured quadratic, minutes of synchronous CPU inside `scan()` on the one
# shared event loop. The 500-char hop and 400-char reach cover the fixture shapes with
# wide margin; the blank-line cap keeps a run of blank lines from being re-walked per
# occurrence (block-level markdown separates paragraphs with one).
_DIRECTIVE_WINDOW = r"(?:[^\n]{0,500}\n)?(?:[ \t]*\n){0,3}[^\n]{0,400}?\b" + _DIRECTIVE + r"\b"

_FAMILY_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "instruction_override": [
        # attack_instruction_override_ignore.txt
        re.compile(r"ignore (all )?(previous|prior|above) instructions", re.IGNORECASE),
        # attack_instruction_override_disregard.txt
        re.compile(r"disregard (your |the )?system prompt", re.IGNORECASE),
        re.compile(r"your new instructions are", re.IGNORECASE),
    ],
    "role_spoofing": [
        # attack_role_spoofing_system_marker.txt ("SYSTEM: You must comply...", same line) and
        # attack_role_spoofing_system_directive.txt (bare "System:", directive on the next line).
        # The directive window (same line, one hop, bounded blank-line skip) is the shared
        # `_DIRECTIVE_WINDOW` constant above — its bounds are the quadratic-scan fix, and a
        # directive two TEXT lines later is out of reach: the accepted recall floor
        # (PLAN-research-throughput risk #3), pinned by a test. `^[ \t]*`, not `^\s*`: `\s`
        # spans newlines and rescans a long whitespace run from every line start (quadratic
        # on an all-newline page).
        re.compile(
            r"^[ \t]*\[?system\]?[ \t]*:" + _DIRECTIVE_WINDOW,
            re.IGNORECASE | re.MULTILINE,
        ),
        # attack_role_spoofing_system_marker.txt ("[system] Override all prior safety
        # guidelines...", a marker without a trailing colon); benign_config_ini_system.txt is
        # the same marker as an INI section header and must not fire. Unanchored, so the
        # window's bounds are what keep every occurrence's rescan bounded.
        re.compile(
            r"\[system\]" + _DIRECTIVE_WINDOW,
            re.IGNORECASE,
        ),
        # attack_role_spoofing_dan.txt
        re.compile(r"you are now dan\b", re.IGNORECASE),
        # attack_role_spoofing_chat_template.txt
        re.compile(r"<\|im_start\|>\s*system", re.IGNORECASE),
    ],
    "ai_directed": [
        # attack_ai_directed_attention.txt
        re.compile(r"attention ai assistant", re.IGNORECASE),
        # attack_ai_directed_if_llm.txt
        re.compile(r"if you are an? (llm|language model)", re.IGNORECASE),
    ],
    "obfuscation": [
        # attack_obfuscation_base64.txt
        re.compile(r"decode and execute", re.IGNORECASE),
    ],
    "exfil_markup": [
        # attack_exfil_markup_image.md — markdown IMAGE whose URL carries an exfil-shaped
        # query param; the `!` is what makes it zero-click, and is now mandatory. The path
        # segment excludes `?` so `\?` needs no backtracking, and the keyword hunt is bounded
        # (`{0,1000}?`): the previous unbounded `[^)]+\?[^)]*` shape backtracked quadratically
        # on a long hostile URL — measured 23s at 20k chars, 396s at 80k, stalling the one
        # event loop every researcher shares (issue #43 #1). Newlines are excluded
        # throughout: `[^)]` spanned them.
        re.compile(
            r"!\[[^\]]*\]\(https?://[^)\n?]+\?[^)\n]{0,1000}?(?:data|token|key|session)=",
            re.IGNORECASE,
        ),
        # attack_exfil_markup_template_query.md — a plain markdown link counts when its query
        # carries template syntax ({{..}}, ${..}, %7B) for the model to fill in, under ANY
        # param: D2's rule is "template-syntax query value", and requiring it under one of
        # the keyword param names let `?token=API_KEY&notes={{conversation_summary}}` pass
        # (issue #43 #4) — the old pattern only saw the template when it sat in the keyword
        # param's own value. A docs link with a literal `?apikey=YOUR_KEY` is
        # benign_docs_apikey_link.md and must not fire. The path caps at 2000 and the
        # template hunt at 1000, on the same reasoning as the image rule (and a >400-char
        # padding prefix used to slip past the old 400-char path cap): the fetch path scans
        # the cap-truncated body the model sees, so a bounded per-start cost over a capped
        # page is a bounded total on the one shared event loop.
        re.compile(
            r"\[[^\]]*\]\(https?://[^)\n?]{0,2000}?\?[^)\n]{0,1000}?(?:\{\{|\$\{|%7B)",
            re.IGNORECASE,
        ),
    ],
}


# `_ZERO_WIDTH_CHARS`, plus C0 control chars other than \n and \t — \r is stripped too, since
# captures are normalized markdown (and a surviving \r let a CRLF-terminated fence-shaped line
# slip past `FENCE_LINE_RE`'s `$`).
# Byte hygiene, not detection: this runs on survivor markdown regardless of whether
# `[guard] enabled` bypassed the scan (Phase 3 D3/D5).
_INVISIBLE_RE = re.compile(f"[{_ZERO_WIDTH_CHARS}\x00-\x08\x0b-\x1f\x7f]")


def strip_invisibles(text: str) -> str:
    """Strip zero-width chars and C0 control chars (except `\\n`/`\\t`) from `text`.

    Mechanical byte hygiene. `scan` calls this itself, first, so callers may strip before or
    after calling `scan` with no effect on the verdict (Phase 2 D5) — order no longer matters.
    """
    return _INVISIBLE_RE.sub("", text)


class ScanResult(BaseModel):
    """Verdict for one scanned text: whether to block, and which families fired."""

    model_config = ConfigDict(extra="forbid")

    blocked: bool
    signals: list[str]


def scan(text: str) -> ScanResult:
    """Scan `text` for injection signals across the five stable families.

    The verdict is computed on invisible-stripped text (Phase 2 D5): `text` is passed through
    `strip_invisibles` before any family matches, so zero-width character presence alone can
    never block a page. An attack phrase split by zero-width characters to dodge substring
    matching reassembles under stripping and is caught by whichever family its plain text
    belongs to (e.g. `attack_instruction_override_zerowidth.txt` fires `instruction_override`,
    not a presence-only obfuscation rule — the fixture is filed under the family it actually
    fires). This replaces block-on-presence, which produced measured
    false positives on ordinary technical pages (anthropic.com/engineering, docs.langchain.com,
    openai.com prose/markup/KaTeX) in run `2026-08-20-172105`.

    Every family runs; a family is added to `signals` at most once, in the stable
    family order, the first time any of its rules fires. Any signal firing blocks —
    no scoring thresholds or weights (see module docstring: false precision without
    measured rates).
    """
    text = strip_invisibles(text)
    signals = [
        family
        for family in FAMILY_ORDER
        if any(pattern.search(text) for pattern in _FAMILY_PATTERNS[family])
    ]
    return ScanResult(blocked=bool(signals), signals=signals)


def guard_blocked_detail(url: str, signals: list[str]) -> str:
    """One incident line for a page or result dropped by the guard: URL plus fired families.

    Moved here from fetch.py/search.py's private, identical copies (Phase 5, deferred Phase 3
    simplify) so both call sites share one definition instead of two hand-pasted ones.
    """
    return f"{url}: blocked by guard ({', '.join(signals)})"


# Matches any boundary-SHAPED line, genuine or forged: `fence` emits hex tokens, but the
# token part here is any non-space run, so an attacker's `<<<END UNTRUSTED xyz>>>` matches
# too — the whole line, nothing else on it. The `\r?` matters: MULTILINE `$` matches before
# `\n` only, so without it a CRLF-terminated forged fence line (PDF text and `fence`d
# non-fetched content are never newline-normalized upstream) escaped neutralization.
# Shared by `fence` (neutralizing forged fence lines inside content), `sanitize_for_report`
# (same, in a report) and tests.
FENCE_LINE_RE = re.compile(r"^<<<(END )?UNTRUSTED \S+>>>\r?$", re.MULTILINE)

# What a neutralized fence-shaped line becomes: two angle brackets, not three, so it can never
# re-match `FENCE_LINE_RE` on a second pass (idempotence).
_FENCE_NEUTRALIZED = "<<UNTRUSTED-MARKER-REMOVED>>"


def fence(text: str) -> str:
    """Wrap `text` in a random-boundary fence marking it as untrusted content (D1 spotlighting).

    The boundary token (`secrets.token_hex(8)`, so a fresh 16-hex-char value every call) is
    stripped from `text` FIRST: a payload that happens to contain this call's exact token could
    otherwise forge a matching closing boundary of its own and escape containment. Since the
    token is drawn fresh per call, an attacker can never know it in advance to plant it — this
    guards only the residual case where genuine content coincidentally contains a prior call's
    token.

    Any fence-SHAPED line in the content is neutralized too, whatever its token: the fence's
    consumer is a model, not an exact-match parser, so a visually valid `<<<END UNTRUSTED ...>>>`
    line with the wrong token could still read as a closing boundary. Fence-shaped lines in
    genuine page text are adversarial-only (same collision argument as the token itself).
    """
    token = secrets.token_hex(8)
    stripped = text.replace(token, "")
    stripped = FENCE_LINE_RE.sub(_FENCE_NEUTRALIZED, stripped)
    return f"<<<UNTRUSTED {token}>>>\n{stripped}\n<<<END UNTRUSTED {token}>>>"


def sanitize_for_report(text: str) -> str:
    """Strip zero-width/control chars and neutralize fence-like and chat-marker sequences.

    Idempotent (D7/R3): running this on its own output changes nothing, which is what makes it
    safe as report.py's `_render_body`'s single funnel — every report byte passes through here
    before disk, so a hostile string embedded anywhere in the assembled body can neither forge a
    `fence` boundary nor a chat role marker once written. `<|` becomes `< |` (a space breaks the
    two-char sequence apart, so no `<|` substring survives to re-match on a second pass); a
    fence-shaped line becomes `_FENCE_NEUTRALIZED`, which by construction cannot match
    `FENCE_LINE_RE` itself.
    """
    cleaned = strip_invisibles(text)
    cleaned = FENCE_LINE_RE.sub(_FENCE_NEUTRALIZED, cleaned)
    cleaned = cleaned.replace("<|", "< |")
    return cleaned
