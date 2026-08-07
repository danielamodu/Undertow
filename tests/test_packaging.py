"""Tests for what a documented install actually gives you.

These exist because of a specific failure. `--mcp` and `--investigate` worked on
the machine they were written on and nowhere else: `mcp`, `mcp-server-datahub`
and `anthropic` were imported by the source but absent from `pyproject.toml`,
having arrived in the developer's environment by other means. Everything passed.
The bug was invisible to every other test in this suite, because the packages
were installed.

A reviewer following the README gets one shot. If `make check-mcp` raises
ImportError on their first run, the MCP integration is broken as far as they are
concerned — and the MCP integration is the part of this project most worth
looking at.

So these assert against the declared metadata, not the environment.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

# The command the README tells a reviewer to run. If that changes, change this,
# and think hard about what the new one leaves out.
DOCUMENTED_INSTALL_EXTRA = "dev"

# Third-party distributions imported anywhere under `src/undertow`, mapped from
# import name to the distribution that provides it. `mcp_server_datahub` is not
# imported — it is launched as `python -m mcp_server_datahub` — which is exactly
# why a linter never caught its absence.
REQUIRED_DISTRIBUTIONS = {
    "acryl-datahub",
    "anthropic",
    "click",
    "mcp",
    "mcp-server-datahub",
    "pydantic",
    "pyyaml",
    "rich",
}


@pytest.fixture(scope="module")
def pyproject() -> dict[str, Any]:
    with open(PYPROJECT, "rb") as handle:
        return tomllib.load(handle)


def distributions_in(requirements: list[str]) -> set[str]:
    """Distribution names from PEP 508 requirement strings, normalised.

    Deliberately crude — enough to read `name>=1.0,<2` and `name[extra]>=1` —
    because the alternative is a packaging dependency in the test suite to parse
    the file that declares the dependencies.
    """
    names = set()
    for req in requirements:
        head = req.split(";")[0].strip()
        for sep in ("[", ">", "<", "=", "!", "~", " "):
            head = head.split(sep)[0]
        if head:
            names.add(head.strip().lower().replace("_", "-"))
    return names


def test_documented_install_provides_every_import_the_code_makes(
    pyproject: dict[str, Any],
) -> None:
    """`pip install -e ".[dev]"` must make every documented command runnable."""
    project = pyproject["project"]
    declared = distributions_in(project["dependencies"])
    declared |= distributions_in(
        project["optional-dependencies"][DOCUMENTED_INSTALL_EXTRA]
    )

    missing = REQUIRED_DISTRIBUTIONS - declared

    assert not missing, (
        f"{sorted(missing)} are imported or launched by src/undertow but are not "
        f'installed by `pip install -e ".[{DOCUMENTED_INSTALL_EXTRA}]"`. A reviewer '
        "following the README would hit ImportError on first run."
    )


def test_the_mcp_extra_installs_both_halves_of_the_mcp_path(
    pyproject: dict[str, Any],
) -> None:
    """The client speaks the protocol; the server is what it speaks to.

    Declaring only `mcp` is the subtler version of the original bug: imports
    resolve, the preflight passes, and the subprocess dies on startup instead.
    """
    extra = distributions_in(pyproject["project"]["optional-dependencies"]["mcp"])

    assert {"mcp", "mcp-server-datahub"} <= extra


def test_the_llm_extra_covers_narrator_and_investigator(pyproject: dict[str, Any]) -> None:
    extra = distributions_in(pyproject["project"]["optional-dependencies"]["llm"])

    assert "anthropic" in extra


def test_python_floor_is_high_enough_for_the_mcp_server(pyproject: dict[str, Any]) -> None:
    """`mcp-server-datahub` requires >=3.11.

    Claiming 3.10 support while the flagship integration cannot install there
    means a 3.10 user gets a successful install and a broken `--mcp`.
    """
    assert pyproject["project"]["requires-python"] == ">=3.11"


def test_a_missing_mcp_package_is_named_not_timed_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The preflight must say which package is missing, immediately.

    Before it existed, an absent `mcp-server-datahub` presented as the
    subprocess never initialising: a 20-second wait ending in "timed out
    starting the server — check DATAHUB_GMS_URL", which points at a URL that
    was never the problem.
    """
    import importlib.util

    from undertow.resolver import McpError, McpToolExecutor

    real_find_spec = importlib.util.find_spec

    def missing_server(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "mcp_server_datahub":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", missing_server)

    with pytest.raises(McpError) as excinfo:
        McpToolExecutor().start()

    message = str(excinfo.value)
    assert "mcp-server-datahub" in message
    assert ".[mcp]" in message
    # And it points at the way out that needs no install at all.
    assert "--mcp" in message


def test_console_script_points_at_something_importable(pyproject: dict[str, Any]) -> None:
    """The `undertow` entry point is the only interface CI ever touches."""
    import importlib

    target = pyproject["project"]["scripts"]["undertow"]
    module_name, _, attribute = target.partition(":")

    module = importlib.import_module(module_name)

    assert callable(getattr(module, attribute))
