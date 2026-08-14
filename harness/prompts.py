"""Loading and rendering of the harness's versioned prompt files."""

from pathlib import Path
from string import Template

# `harness/prompts/` holds ONLY `.md` files: a real `.py` module wins over a same-named
# namespace-package directory, so adding any `.py` there — `__init__.py` especially — makes it a
# package that shadows this module and breaks every import.
_PROMPTS_DIR = Path(__file__).parent / "prompts"


class PromptError(Exception):
    """Raised for an unknown prompt name or a missing render variable."""


def _load(name: str) -> Template:
    if "/" in name or "\\" in name or ".." in name:
        raise PromptError(f"Unknown prompt {name!r}: invalid prompt name")
    path = _PROMPTS_DIR / f"{name}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptError(f"Unknown prompt {name!r}: no such file {path}") from exc
    return Template(text)


def required_variables(name: str) -> set[str]:
    """Return the set of `$variable` placeholders declared by the named prompt."""
    return set(_load(name).get_identifiers())


def render(name: str, **variables: object) -> str:
    """Load the named prompt and substitute `$variable` placeholders with the given values."""
    template = _load(name)
    try:
        return template.substitute(variables)
    except KeyError as exc:
        raise PromptError(f"Prompt {name!r} is missing required variable {exc.args[0]!r}") from exc
    except ValueError as exc:  # malformed placeholder in the file itself
        raise PromptError(f"Prompt {name!r} has an invalid placeholder: {exc}") from exc
