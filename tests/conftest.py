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
def make_config(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Return a factory building a valid HarnessConfig from pydantic models (no TOML)."""

    def _make(
        *,
        page_timeout_ms: int = 15000,
        http_concurrency: int = 5,
        http_deadline_ms: int = 3000,
        max_retries: int = 2,
        per_page_char_cap: int = 12000,
        max_urls_per_call: int = 5,
        # 1, not production's 50: escalation is opt-in per test (pass a higher value
        # explicitly) so pre-Phase-2 tests keep the short-fixture-markdown semantics they
        # were written with. Production's own default stays 50 (see harness/config.py).
        min_markdown_words: int = 1,
        browser_deadline_ms: int = 20000,
        browser_concurrency: int = 2,
        downloads_dir: str = "workspace/downloads",
        # Defaults under tmp_path, not production's "workspace/blocklist.json": a 403/401
        # in any pre-Phase-4 test would otherwise record into the real repo workspace. Every
        # test gets its own isolated, initially-empty file unless it overrides this.
        blocklist_path: str | None = None,
        blocklist_ttl_days: int = 30,
        base_url: str = "http://searx.test",
        default_max_results: int = 10,
    ) -> HarnessConfig:
        monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
        if blocklist_path is None:
            blocklist_path = str(tmp_path / "blocklist.json")
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
                min_markdown_words=min_markdown_words,
                browser_deadline_ms=browser_deadline_ms,
                browser_concurrency=browser_concurrency,
                downloads_dir=downloads_dir,
                blocklist_path=blocklist_path,
                blocklist_ttl_days=blocklist_ttl_days,
            ),
            search=SearchSettings(base_url=base_url, default_max_results=default_max_results),
        )

    return _make
