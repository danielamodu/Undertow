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

import ast
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
SRC = ROOT / "src"
# `make seed`, `make break`, `make reset` are documented commands, so their
# imports have to be installable by the documented install too.
SCRIPTS = ROOT / "scripts"

# The command the README tells a reviewer to run. If that changes, change this,
# and think hard about what the new one leaves out.
DOCUMENTED_INSTALL_EXTRA = "dev"

# Import name -> distribution name, for the cases where they differ. Anything
# not listed is assumed to share its name with its distribution, which is true
# for every other dependency here.
DISTRIBUTION_OF = {
    "datahub": "acryl-datahub",
    "yaml": "pyyaml",
    "jinja2": "jinja2",
    # Pulled in by `acryl-datahub[sql-parser]`, which the seed script needs to
    # parse scripts/sql/. It is never imported by name here, only through
    # `datahub.sql_parsing`, so it maps onto the distribution that supplies it.
    "sqlglot": "acryl-datahub",
}

# Never imported — launched as `python -m mcp_server_datahub` — so no amount of
# scanning import statements will find it. That is precisely how it went missing.
LAUNCHED_NOT_IMPORTED = {"mcp-server-datahub"}

def first_party() -> set[str]:
    """The package, plus every script module, since scripts import each other.

    `reset_demo.py` does `from seed_datahub import main` after putting its own
    directory on the path — a sibling import, not a distribution.
    """
    return {"undertow", *(p.stem for p in SCRIPTS.glob("*.py"))}


def third_party_imports(*roots: Path) -> dict[str, set[str]]:
    """Every non-stdlib, non-first-party module imported under `roots`.

    Derived by walking the AST rather than kept as a hand-maintained list: the
    original bug was a dependency nobody remembered to write down, and a list
    that has to be remembered would have exactly the same failure mode.
    """
    local = first_party()
    found: dict[str, set[str]] = {}
    for path in (p for root in roots for p in root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module] if node.module and node.level == 0 else []
            else:
                continue
            for name in names:
                root = name.split(".")[0]
                if not root or root in sys.stdlib_module_names or root in local:
                    continue
                found.setdefault(
                    DISTRIBUTION_OF.get(root, root), set()
                ).add(str(path.relative_to(ROOT)))
    return found


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

    imported = third_party_imports(SRC, SCRIPTS)
    required = set(imported) | LAUNCHED_NOT_IMPORTED
    missing = required - declared

    def where(dist: str) -> str:
        return ", ".join(sorted(imported.get(dist, {"(launched as a subprocess)"})))

    detail = "\n".join(f"  {dist} — imported by {where(dist)}" for dist in sorted(missing))
    assert not missing, (
        f'Not installed by `pip install -e ".[{DOCUMENTED_INSTALL_EXTRA}]"`:\n{detail}\n'
        "A reviewer following the README would hit ImportError on first run."
    )


def test_the_cli_starts_on_a_core_install(pyproject: dict[str, Any]) -> None:
    """Anything `undertow.cli` reaches at import time must be a core dependency.

    `jinja2` was not, and it is imported at module scope by the narrator, which
    the CLI imports unconditionally. On a machine where nothing else had pulled
    it in, `undertow --version` raised ModuleNotFoundError before printing
    anything — the entire tool, not just an optional path.

    The extras are for things guarded behind a flag and a try/except. Import-time
    dependencies are not optional, whatever the packaging says.
    """
    core = distributions_in(pyproject["project"]["dependencies"])

    import_time = {"click", "rich", "pydantic", "pyyaml", "acryl-datahub", "jinja2"}
    missing = import_time - core

    assert not missing, (
        f"{sorted(missing)} are imported at module scope but are not core "
        "dependencies, so a core install cannot start the CLI."
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
