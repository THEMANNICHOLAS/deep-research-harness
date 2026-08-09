"""Shared test fixtures for the harness suite."""

import pytest

from harness.config import (
    AgentSettings,
    BrowserSettings,
    FetchSettings,
    HarnessConfig,
    ProviderConfig,
    RoleConfig,
    SearchSettings,
)


@pytest.fixture
def make_config(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Return a factory building a valid HarnessConfig from pydantic models (no TOML).

    Defaults `agent.workspace_dir`/`reports_dir` to subdirectories of pytest's `tmp_path`
    rather than `AgentSettings()`'s repo-root-relative defaults, so a full test run never
    leaves `workspace/`/`reports/` behind in the repo. A caller-supplied `agent=` wins
    untouched.
    """

    def _make(
        *,
        page_timeout_ms: int = 15000,
        max_concurrency: int = 5,
        per_page_char_cap: int = 12000,
        max_urls_per_call: int = 4,
        base_url: str = "http://searx.test",
        default_max_results: int = 10,
        agent: AgentSettings | None = None,
    ) -> HarnessConfig:
        monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
        if agent is None:
            agent = AgentSettings(
                workspace_dir=tmp_path / "workspace", reports_dir=tmp_path / "reports"
            )
        return HarnessConfig(
            providers={
                "opencode": ProviderConfig(
                    base_url="https://example.test/v1", api_key_env="OPENCODE_API_KEY"
                )
            },
            roles={
                "head": RoleConfig(provider="opencode", model="test-model"),
                "subagent": RoleConfig(provider="opencode", model="test-model"),
            },
            browser=BrowserSettings(backend="playwright", cdp_url=None),
            fetch=FetchSettings(
                page_timeout_ms=page_timeout_ms,
                max_concurrency=max_concurrency,
                per_page_char_cap=per_page_char_cap,
                max_urls_per_call=max_urls_per_call,
            ),
            search=SearchSettings(base_url=base_url, default_max_results=default_max_results),
            agent=agent,
        )

    return _make
