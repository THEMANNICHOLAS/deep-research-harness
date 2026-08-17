"""Shared test fixtures for the harness suite."""

import pytest

from harness.config import (
    FetchSettings,
    HarnessConfig,
    ProviderConfig,
    RoleConfig,
    SearchSettings,
)


@pytest.fixture
def make_config(monkeypatch: pytest.MonkeyPatch):
    """Return a factory building a valid HarnessConfig from pydantic models (no TOML)."""

    def _make(
        *,
        page_timeout_ms: int = 15000,
        http_concurrency: int = 5,
        http_deadline_ms: int = 3000,
        max_retries: int = 2,
        per_page_char_cap: int = 12000,
        max_urls_per_call: int = 5,
        base_url: str = "http://searx.test",
        default_max_results: int = 10,
    ) -> HarnessConfig:
        monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
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
            fetch=FetchSettings(
                page_timeout_ms=page_timeout_ms,
                http_concurrency=http_concurrency,
                http_deadline_ms=http_deadline_ms,
                max_retries=max_retries,
                per_page_char_cap=per_page_char_cap,
                max_urls_per_call=max_urls_per_call,
            ),
            search=SearchSettings(base_url=base_url, default_max_results=default_max_results),
        )

    return _make
