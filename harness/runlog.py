"""Per-run log of degraded-coverage incidents (best-effort + disclose).

Tools record what failed mid-run — a dead search backend, a blocked fetch, a capture that
could not be written — and two consumers read it: `harness/__main__.py` echoes each incident
to the terminal as it appears, and `harness/report.py` lists them all under
`## Gaps and disclosures`. Toolpack-neutral: `kind` is a short slug owned by whoever records
it, never an enum this module must grow for each new tool.
"""

from pydantic import BaseModel, ConfigDict


class Incident(BaseModel):
    """One disclosed failure: a short machine-readable kind plus human-readable detail."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    detail: str


class RunLog:
    """Collects one run's incidents, in the order they were recorded.

    Every tool in a run must share ONE instance — a tool recording into its own private log
    is an incident nobody ever sees. `build_agent`/`build_tools` default a missing log to a
    fresh one only so tests that assert nothing about incidents stay unchanged; the real
    entrypoint (`__main__.main`) always passes the run's shared instance.
    """

    def __init__(self) -> None:
        self._incidents: list[Incident] = []

    def record(self, kind: str, detail: str) -> None:
        self._incidents.append(Incident(kind=kind, detail=detail))

    def incidents(self) -> list[Incident]:
        """Every incident so far, oldest first. A copy — callers may not mutate the log."""
        return list(self._incidents)
