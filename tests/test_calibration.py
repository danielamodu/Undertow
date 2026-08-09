"""Tests for policy self-calibration.

`suggest` is advisory only: it reads a model's recorded history and proposes
a threshold worth reviewing. Nothing here can touch a policy file, a
baseline, or a verdict — it returns data, and a human decides what to do
with it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from undertow.calibration import suggest
from undertow.history import VerdictRun


def _run(severity: str, warning_kinds: tuple[str, ...] = (), warning: int = 0) -> VerdictRun:
    return VerdictRun(
        checked_at=datetime(2026, 1, 1, tzinfo=UTC),
        result="SUCCESS" if severity != "BLOCK" else "FAILURE",
        severity=severity,
        blocking=1 if severity == "BLOCK" else 0,
        warning=warning,
        assets_checked=6,
        run_id="run-1",
        warning_kinds=warning_kinds,
    )


def test_no_suggestion_below_the_minimum_run_count() -> None:
    """Three runs of the same warning is a coincidence, not a trend — the
    whole point of a minimum is not crying wolf on day one.
    """
    runs = [_run("WARN", ("STATISTICAL_DRIFT",), 1) for _ in range(3)]

    assert suggest(runs) == []


def test_chronic_warning_is_flagged() -> None:
    runs = [_run("WARN", ("STATISTICAL_DRIFT",), 1) for _ in range(6)]

    suggestions = suggest(runs)

    assert len(suggestions) == 1
    assert suggestions[0].finding_kind == "STATISTICAL_DRIFT"
    assert suggestions[0].warn_count == 6
    assert suggestions[0].total_runs == 6
    assert suggestions[0].rate == 1.0


def test_occasional_warning_is_not_flagged() -> None:
    """Below the chronic threshold, this is exactly the kind of finding a
    gate is supposed to catch occasionally — not a sign the policy is wrong.
    """
    runs = [
        _run("WARN", ("STATISTICAL_DRIFT",), 1),
        _run("CLEAR"),
        _run("CLEAR"),
        _run("CLEAR"),
        _run("CLEAR"),
    ]

    assert suggest(runs) == []


def test_clean_history_suggests_nothing() -> None:
    runs = [_run("CLEAR") for _ in range(10)]

    assert suggest(runs) == []


def test_multiple_recurring_kinds_are_all_reported_worst_first() -> None:
    runs = [
        _run("WARN", ("STATISTICAL_DRIFT", "PII_APPLIED"), 2),
        _run("WARN", ("STATISTICAL_DRIFT",), 1),
        _run("WARN", ("STATISTICAL_DRIFT",), 1),
        _run("CLEAR"),
        _run("CLEAR"),
    ]

    suggestions = suggest(runs)

    kinds = [s.finding_kind for s in suggestions]
    assert "STATISTICAL_DRIFT" in kinds
    # STATISTICAL_DRIFT warned 3/5 runs, PII_APPLIED only 1/5 -- below the
    # chronic threshold, so it should not appear at all.
    assert "PII_APPLIED" not in kinds
    assert suggestions[0].finding_kind == "STATISTICAL_DRIFT"


def test_a_finding_kind_warning_twice_in_one_run_still_counts_once() -> None:
    """`warning_kinds` is a set per run by construction (see datahub_writer),
    but the reader should not double-count even if that ever changed.
    """
    runs = [_run("WARN", ("STATISTICAL_DRIFT", "STATISTICAL_DRIFT"), 2) for _ in range(6)]

    suggestions = suggest(runs)

    assert len(suggestions) == 1
    assert suggestions[0].warn_count == 6


def test_runs_written_before_this_field_existed_are_silently_ignored() -> None:
    """Old assertion run events have no `warning_kinds` key at all --
    history.py's reader already turns that into an empty tuple, and this
    module must treat that the same as a run with no warnings, not error.
    """
    runs = [_run("WARN", (), 1) for _ in range(6)]

    assert suggest(runs) == []
