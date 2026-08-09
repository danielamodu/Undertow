"""Shared test fixtures.

`undertow.cli` calls `load_dotenv()` on import — deliberately, so a developer's
`.env` reaches every command without exporting it into the shell first. That is
exactly why the test suite cannot be allowed to see it: a real key sitting in a
local `.env` (or in the developer's actual shell environment) would silently
change which tests pass, on that machine only, in a way CI would never
reproduce. A provider-selection test that "passes" because someone's laptop
happens to have GROQ_API_KEY set is not proof of anything.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

# Every environment variable a provider can be selected from. Kept here rather
# than imported from undertow.investigator, so this list has to be updated by
# hand when a new provider is added — a reminder to ask whether that provider
# also needs a test proving the byte-identical-verdict guarantee holds for it.
_PROVIDER_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "UNDERTOW_ANTHROPIC_MODEL",
    "GROQ_API_KEY",
    "GROQ_MODEL",
    "OPENROUTER_API_KEY",
    "OPENROUTER_MODEL",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
)


@pytest.fixture(autouse=True)
def _clean_provider_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test starts with no LLM provider configured, regardless of
    what's in the environment this process actually runs in.

    Tests that want a provider set it themselves, after this fixture has
    already run — monkeypatch's per-test teardown means their `setenv` calls
    layer on top of this clean baseline and get reverted afterward either way.
    """
    for var in _PROVIDER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield
