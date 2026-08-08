"""Tests for the CLI's exit-code contract.

CI reads exit codes, not prose, so the codes are the actual interface:

    0  proceed
    1  BLOCK
    2  Undertow failed

The distinction between 1 and 2 is the one worth defending. A gate that
returns "blocked" when it actually crashed teaches a team to ignore it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from undertow.cli import EXIT_ERROR, EXIT_OK, main
from undertow.models import FindingKind

FRAUD_MODEL = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detector_v3,PROD)"
MISSING_MODEL = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,nonexistent_model_xyz,PROD)"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def output_of(result) -> str:  # type: ignore[no-untyped-def]
    """Everything the command printed, on either stream, unwrapped.

    Two portability problems, both solved here rather than in every assertion:

    Click 8.1 and 8.2 disagree about whether `Result.output` includes stderr,
    and `Result.stderr` raises on 8.1 when the streams are mixed.

    Rich hard-wraps to the terminal width, so a message can arrive as
    "must\\nexpire". Collapsing whitespace lets tests assert on the message
    rather than on how wide the console happened to be.
    """
    text = result.output or ""
    try:
        extra = result.stderr or ""
    except ValueError:  # 8.1 with mixed streams — already in .output
        extra = ""
    return " ".join((text + extra).split())


def write(tmp_path: Path, body: str) -> str:
    p = tmp_path / "undertow.yaml"
    p.write_text(body, encoding="utf-8")
    return str(p)


# --------------------------------------------------------------------------
# policy validate
# --------------------------------------------------------------------------


def test_missing_policy_file_falls_back_to_defaults(runner: CliRunner, tmp_path: Path) -> None:
    # Undertow has to work with no config at all, or the "plug and play" claim
    # is false on first contact.
    result = runner.invoke(main, ["policy", "validate", "--config", str(tmp_path / "nope.yaml")])
    assert result.exit_code == EXIT_OK
    assert "built-in defaults" in output_of(result)


def test_valid_policy_file_passes(runner: CliRunner, tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """
        max_hops: 3
        rules:
          COLUMN_DROPPED: BLOCK
        exemptions: []
        """,
    )
    result = runner.invoke(main, ["policy", "validate", "--config", path])
    assert result.exit_code == EXIT_OK
    assert "1 rule override" in output_of(result)


def test_unknown_finding_kind_exits_two_not_one(runner: CliRunner, tmp_path: Path) -> None:
    # A typo'd rule name is a config error, not a blocked deploy.
    path = write(tmp_path, "rules:\n  COLUMN_VANISHED: BLOCK\n")
    result = runner.invoke(main, ["policy", "validate", "--config", path])
    assert result.exit_code == EXIT_ERROR
    assert "unknown finding kinds" in output_of(result)


def test_exemption_without_expiry_exits_two(runner: CliRunner, tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """
        exemptions:
          - reason: "forever and ever"
        """,
    )
    result = runner.invoke(main, ["policy", "validate", "--config", path])
    assert result.exit_code == EXIT_ERROR
    assert "must expire" in output_of(result)


def test_malformed_yaml_exits_two_and_does_not_silently_default(
    runner: CliRunner, tmp_path: Path
) -> None:
    # The dangerous failure: falling back to defaults would mean a team's BLOCK
    # rules quietly stop applying while CI still reports green.
    path = write(tmp_path, "rules: [this, is, a, list, not, a, mapping]\n")
    result = runner.invoke(main, ["policy", "validate", "--config", path])
    assert result.exit_code == EXIT_ERROR
    assert "Could not load policy" in output_of(result)


def test_allow_probable_block_is_called_out(runner: CliRunner, tmp_path: Path) -> None:
    path = write(tmp_path, "allow_probable_block: true\n")
    result = runner.invoke(main, ["policy", "validate", "--config", path])
    assert result.exit_code == EXIT_OK
    assert "allow_probable_block is ON" in output_of(result)


# --------------------------------------------------------------------------
# policy show
# --------------------------------------------------------------------------


def test_policy_show_lists_every_kind(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        main, ["policy", "show", "--config", str(tmp_path / "absent.yaml")]
    )
    assert result.exit_code == EXIT_OK
    # Rich wraps at terminal width; check kinds appear, not exact layout.
    text = output_of(result)
    for kind in FindingKind:
        assert kind.value in text


def test_policy_show_marks_overrides(runner: CliRunner, tmp_path: Path) -> None:
    path = write(tmp_path, "rules:\n  MEAN_SHIFT: CLEAR\n")
    result = runner.invoke(main, ["policy", "show", "--config", path])
    assert result.exit_code == EXIT_OK
    assert "override" in output_of(result)


# --------------------------------------------------------------------------
# Implemented commands: resolve and check
# --------------------------------------------------------------------------


def test_resolve_command_runs_successfully(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_source = MagicMock()
    mock_source.connection_error = None
    mock_source.get_entity.return_value = None
    mock_source.get_entities.return_value = {}
    mock_source.get_lineage.return_value = []
    mock_source.list_schema_fields.return_value = []
    monkeypatch.setattr("undertow.cli.SdkLineageSource", lambda *args, **kwargs: mock_source)

    result = runner.invoke(main, ["resolve", "--model", FRAUD_MODEL])
    assert result.exit_code == EXIT_OK
    assert "Footprint resolved" in output_of(result)


def test_check_fails_closed_when_nothing_resolves(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty footprint is an error, never a verdict.

    A source that returns nothing is indistinguishable from a model whose
    upstream is genuinely clean. Only one of those is safe to ship, so Undertow
    refuses to guess.
    """
    mock_source = MagicMock()
    mock_source.connection_error = None
    mock_source.get_entity.return_value = None
    mock_source.get_entities.return_value = {}
    mock_source.get_lineage.return_value = []
    mock_source.list_schema_fields.return_value = []
    monkeypatch.setattr("undertow.cli.SdkLineageSource", lambda *args, **kwargs: mock_source)

    result = runner.invoke(main, ["check", "--model", MISSING_MODEL])
    assert result.exit_code == EXIT_ERROR
    assert "Resolved nothing upstream" in output_of(result)


def test_check_fails_closed_when_datahub_is_unreachable(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable GMS must not produce a green light."""
    mock_source = MagicMock()
    mock_source.connection_error = "ConnectionError: [Errno 111] Connection refused"
    mock_source.get_entity.return_value = None
    mock_source.get_entities.return_value = {}
    mock_source.get_lineage.return_value = []
    mock_source.list_schema_fields.return_value = []
    monkeypatch.setattr("undertow.cli.SdkLineageSource", lambda *args, **kwargs: mock_source)

    result = runner.invoke(main, ["check", "--model", FRAUD_MODEL])
    assert result.exit_code == EXIT_ERROR
    assert "Cannot reach DataHub" in output_of(result)




def test_investigate_without_mcp_says_so(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_source = MagicMock()
    mock_source.connection_error = None
    mock_source.get_entity.return_value = None
    mock_source.get_entities.return_value = {}
    mock_source.get_lineage.return_value = []
    mock_source.list_schema_fields.return_value = []
    monkeypatch.setattr("undertow.cli.SdkLineageSource", lambda *args, **kwargs: mock_source)

    result = runner.invoke(main, ["check", "--model", FRAUD_MODEL, "--investigate"])

    assert "--investigate requires --mcp" in output_of(result)


def test_investigate_without_a_key_says_so_rather_than_degrading_in_silence(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug this pins: `--investigate` with no key looked exactly like success.

    Same output, same exit code, no message — so the only conclusion available
    to someone running it was that the agent loop does nothing at all.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    mock_source = MagicMock()
    mock_source.connection_error = None
    mock_source.get_entity.return_value = None
    mock_source.get_entities.return_value = {}
    mock_source.get_lineage.return_value = []
    mock_source.list_schema_fields.return_value = []
    monkeypatch.setattr("undertow.cli.McpToolExecutor", lambda *a, **kw: MagicMock())
    monkeypatch.setattr("undertow.cli.McpLineageSource", lambda *a, **kw: mock_source)

    result = runner.invoke(
        main, ["check", "--model", FRAUD_MODEL, "--mcp", "--investigate"]
    )

    output = output_of(result)
    assert "--investigate is not available" in output
    assert "ANTHROPIC_API_KEY" in output


# --------------------------------------------------------------------------
# Sweeping an inventory
# --------------------------------------------------------------------------
#
# A team has twelve models, not one. Without an inventory, adopting Undertow
# means keeping a set of 70-character URNs in step with a CI script by hand,
# which is how a gate ends up covering three of the twelve.


def test_all_and_model_together_is_rejected(runner: CliRunner) -> None:
    result = runner.invoke(main, ["check", "--all", "--model", FRAUD_MODEL])

    assert result.exit_code == EXIT_ERROR
    assert "not both" in output_of(result)


def test_neither_all_nor_model_is_rejected(runner: CliRunner) -> None:
    result = runner.invoke(main, ["check"])

    assert result.exit_code == EXIT_ERROR
    assert "--all" in output_of(result)


def test_all_without_an_inventory_says_how_to_add_one(
    runner: CliRunner, tmp_path: Path
) -> None:
    """An empty inventory must not read as 'nothing to check, all clear'."""
    path = write(tmp_path, "max_hops: 3\n")

    result = runner.invoke(main, ["check", "--all", "--config", path])

    assert result.exit_code == EXIT_ERROR
    assert "models:" in output_of(result)


def test_the_inventory_is_read_from_the_policy(tmp_path: Path) -> None:
    from undertow.policy import Policy

    path = tmp_path / "undertow.yaml"
    path.write_text(
        'models:\n  - "urn:li:mlModel:(a,b,PROD)"\n  - "urn:li:mlModel:(c,d,PROD)"\n',
        encoding="utf-8",
    )

    assert Policy.load(path).models == (
        "urn:li:mlModel:(a,b,PROD)",
        "urn:li:mlModel:(c,d,PROD)",
    )


def test_an_absent_inventory_defaults_to_empty_not_to_everything() -> None:
    from undertow.policy import Policy

    assert Policy().models == ()


@pytest.mark.parametrize(
    "codes, expected",
    [
        ([0, 0], 0),
        ([0, 1], 1),
        ([1, 0], 1),
        # 2 outranks 1: a model the gate could not see is worse than one it saw
        # and blocked, because only the first is indistinguishable from a pass.
        ([1, 2], 2),
        ([2, 1], 2),
        ([0, 2], 2),
    ],
)
def test_the_sweep_reports_the_worst_outcome(codes: list[int], expected: int) -> None:
    """Reimplements the fold, so the ordering is asserted rather than assumed."""
    worst = 0
    for code in codes:
        worst = 2 if 2 in (worst, code) else max(worst, code)

    assert worst == expected


def test_check_reports_a_bad_policy_before_anything_else(
    runner: CliRunner, tmp_path: Path
) -> None:
    path = write(tmp_path, "rules:\n  NOT_A_KIND: BLOCK\n")
    result = runner.invoke(
        main, ["check", "--model", "urn:li:mlModel:(x,y,PROD)", "--config", path]
    )
    assert result.exit_code == EXIT_ERROR
    assert "unknown finding kinds" in output_of(result)
