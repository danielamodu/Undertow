"""Tests for the DataHub Skill shipped in `skills/`.

A skill is documentation an agent executes. That makes it worse than ordinary
docs when it goes stale: a wrong command in a README wastes a reader's minute,
and a wrong command in a skill gets run.

So the parts an agent acts on are pinned to the code — every CLI subcommand and
flag the skill names must exist, and the finding-kind reference must match the
enum the policy engine actually rules on.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from undertow.cli import main
from undertow.models import FindingKind

SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "undertow-deploy-gate"
SKILL_MD = SKILL_DIR / "SKILL.md"
VERDICTS = SKILL_DIR / "references" / "verdicts.md"


def split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        raise AssertionError("SKILL.md must open with a YAML frontmatter block")
    _, raw, body = text.split("---\n", 2)
    return yaml.safe_load(raw), body


@pytest.fixture(scope="module")
def skill() -> tuple[dict, str]:
    return split_frontmatter(SKILL_MD.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Conformance with the datahub-skills format
# --------------------------------------------------------------------------


def test_frontmatter_carries_the_fields_the_registry_expects(skill) -> None:
    meta, _ = skill

    assert meta["name"] == SKILL_DIR.name
    assert meta["user-invocable"] is True
    assert "allowed-tools" in meta
    assert "min-cli-version" in meta


def test_the_description_declares_its_triggers(skill) -> None:
    """Skills are dispatched on the description, so it carries the trigger phrases."""
    meta, _ = skill

    assert "Triggers on:" in meta["description"]
    assert len(meta["description"]) > 200


def test_allowed_tools_covers_every_binary_the_skill_invokes(skill) -> None:
    meta, body = skill
    allowed = meta["allowed-tools"]

    for binary in {m for m in re.findall(r"^\s*(undertow|datahub) ", body, re.M)}:
        assert f"Bash({binary} *)" in allowed, (
            f"the skill runs `{binary}` but allowed-tools does not permit it"
        )


def test_the_advertised_files_exist(skill) -> None:
    _, body = skill

    for rel in re.findall(r"`((?:references|templates)/[\w./-]+)`", body):
        assert (SKILL_DIR / rel).exists(), f"SKILL.md points at missing {rel}"


# --------------------------------------------------------------------------
# The skill must not tell an agent to run something that does not exist
# --------------------------------------------------------------------------


def cli_commands() -> set[str]:
    return set(main.commands)


def test_every_undertow_subcommand_named_by_the_skill_exists(skill) -> None:
    _, body = skill
    named = set(re.findall(r"undertow ([a-z-]+)", body))
    # `policy` has subcommands of its own; both forms appear in the text.
    named -= {"policy"}

    unknown = named - cli_commands()

    assert not unknown, f"SKILL.md tells the agent to run {sorted(unknown)}, which do not exist"


@pytest.mark.parametrize(
    "command, flag",
    [
        ("check", "--mcp"),
        ("check", "--investigate"),
        ("check", "--write-back"),
        ("check", "--fail-on-warn"),
        ("check", "--baseline"),
        ("baseline", "--model"),
    ],
)
def test_flags_the_skill_recommends_are_real(command: str, flag: str) -> None:
    result = CliRunner().invoke(main, [command, "--help"])

    assert result.exit_code == 0
    assert flag in result.output


def test_datahub_cli_invocations_use_real_flags() -> None:
    """These were wrong on the first draft, in ways only running them revealed.

    `datahub search --query X --entity mlModel` looks entirely plausible and is
    not a thing: the query is positional and the filter field is `entity_type`.
    `-f entity=mlModel` fails with thirty pydantic validation errors that never
    name the actual problem, which is precisely the sort of dead end a skill
    exists to keep an agent out of.
    """
    from datahub.entrypoints import datahub as datahub_cli

    runner = CliRunner()

    get_help = runner.invoke(datahub_cli, ["get", "urn", "--help"])
    assert get_help.exit_code == 0
    assert "--urn" in get_help.output
    assert "--aspect" in get_help.output

    search_help = runner.invoke(datahub_cli, ["search", "query", "--help"])
    assert search_help.exit_code == 0
    assert "--urns-only" in search_help.output
    assert "-f, --filter" in search_help.output


def test_the_skill_does_not_run_the_filter_field_that_looks_right_but_is_not(
    skill,
) -> None:
    """Scoped to runnable blocks: the prose deliberately names the wrong form
    in order to warn against it, and that mention must not trip the check."""
    _, body = skill
    commands = "\n".join(re.findall(r"```bash\n(.*?)```", body, re.S))

    assert "entity_type=" in commands
    assert "-f entity=" not in commands, "`entity` is rejected; the field is `entity_type`"


def test_policy_subcommands_named_by_the_skill_exist() -> None:
    result = CliRunner().invoke(main, ["policy", "--help"])

    assert result.exit_code == 0
    for sub in ("show", "validate"):
        assert sub in result.output


# --------------------------------------------------------------------------
# The reference must match what the engine actually rules on
# --------------------------------------------------------------------------


def test_the_reference_documents_every_finding_kind_and_no_others() -> None:
    """A finding kind added to the enum but not the reference leaves the agent
    unable to explain a verdict it will eventually see."""
    text = VERDICTS.read_text(encoding="utf-8")
    documented = set(re.findall(r"^\| `?([A-Z_]{4,})`? \|", text, re.M))

    assert documented == {kind.value for kind in FindingKind}


def test_the_reference_agrees_with_the_engine_on_confidence() -> None:
    text = VERDICTS.read_text(encoding="utf-8")
    # Kinds are grouped under headings that name their confidence.
    certain_section = text.split("### Statistical")[0]
    statistical_section = text.split("### Statistical")[1]

    for kind in FindingKind:
        section = certain_section if kind.confidence.value == "CERTAIN" else statistical_section
        assert kind.value in section, (
            f"{kind.value} is {kind.confidence.value} but the reference files it elsewhere"
        )


def test_exit_codes_are_stated_correctly() -> None:
    """The one thing an agent must not get wrong."""
    text = VERDICTS.read_text(encoding="utf-8")

    assert re.search(r"\|\s*`?0`?\s*\|\s*CLEAR", text)
    assert re.search(r"\|\s*`?1`?\s*\|\s*BLOCK", text)
    # 2 is deliberately not a verdict, so it has no verdict name.
    assert "Not a pass" in text
