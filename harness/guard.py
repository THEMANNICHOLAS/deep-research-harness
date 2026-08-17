"""Heuristic prompt-injection scanner over untrusted text.

`scan` checks text against five signal families — `instruction_override`,
`role_spoofing`, `ai_directed`, `obfuscation`, `exfil_markup` — and reports which of
them fired. Detection is syntactic-only: it catches recognizable attack phrasing, not
semantic steering, which is contained by R2/R4 (strict URL provenance, structural tool
isolation) rather than detected here. Rules exist only where a fixture in
tests/fixtures/injection/ fires them (see PLAN-prompt-injection-defense.md Phase 2) —
this module does not invent coverage beyond what is measured.
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

_FAMILY_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "instruction_override": [
        # attack_instruction_override_ignore.txt
        re.compile(r"ignore (all )?(previous|prior|above) instructions", re.IGNORECASE),
        # attack_instruction_override_disregard.txt
        re.compile(r"disregard (your |the )?system prompt", re.IGNORECASE),
        re.compile(r"your new instructions are", re.IGNORECASE),
    ],
    "role_spoofing": [
        # attack_role_spoofing_system_marker.txt ("SYSTEM:" line)
        re.compile(r"^\s*\[?system\]?\s*:", re.IGNORECASE | re.MULTILINE),
        # attack_role_spoofing_system_marker.txt ("[system]" marker without trailing colon)
        re.compile(r"\[system\]", re.IGNORECASE),
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
        # attack_obfuscation_zerowidth.txt — zero-width chars used to split flagged phrases
        re.compile(r"[​‌﻿]"),
        # attack_obfuscation_base64.txt
        re.compile(r"decode and execute", re.IGNORECASE),
    ],
    "exfil_markup": [
        # attack_exfil_markup_image.md / attack_exfil_markup_link.md — markdown image or
        # link whose URL carries an exfil-shaped query param
        re.compile(
            r"!?\[[^\]]*\]\(https?://[^)]+\?[^)]*(data|token|key|session)=",
            re.IGNORECASE,
        ),
    ],
}


# The same zero-width set the obfuscation rule matches (U+200B, U+200C, U+FEFF), plus C0
# control chars other than \n and \t — \r is stripped too, since captures are normalized
# markdown. Byte hygiene, not detection: this runs on survivor markdown regardless of
# whether `[guard] enabled` bypassed the scan (Phase 3 D3/D5).
_INVISIBLE_RE = re.compile(r"[​‌﻿\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_invisibles(text: str) -> str:
    """Strip zero-width chars and C0 control chars (except `\\n`/`\\t`) from `text`.

    Mechanical byte hygiene only — never called before `scan`, which must see raw text
    (see harness/tools/fetch.py's frozen pipeline order).
    """
    return _INVISIBLE_RE.sub("", text)


class ScanResult(BaseModel):
    """Verdict for one scanned text: whether to block, and which families fired."""

    model_config = ConfigDict(extra="forbid")

    blocked: bool
    signals: list[str]


def scan(text: str) -> ScanResult:
    """Scan `text` for injection signals across the five stable families.

    Every family runs; a family is added to `signals` at most once, in the stable
    family order, the first time any of its rules fires. Any signal firing blocks —
    no scoring thresholds or weights (see module docstring: false precision without
    measured rates).
    """
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
# too — the whole line, nothing else on it. Shared by `fence` (neutralizing forged fence
# lines inside content), `sanitize_for_report` (same, in a report) and tests.
FENCE_LINE_RE = re.compile(r"^<<<(END )?UNTRUSTED \S+>>>$", re.MULTILINE)

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
