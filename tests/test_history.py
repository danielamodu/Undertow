"""Tests for reading verdict history back out of DataHub.

Undertow keeps no database. Every `--write-back` appends an `assertionRunEvent`
to a native assertion, and because that aspect is a timeseries one, runs
accumulate instead of overwriting. The history is the catalog's, not ours.

The load-bearing detail is that the assertion URN is *derived* from the model
URN rather than stored anywhere. If the derivation in `history` and the one in
`datahub_writer` ever drift apart, nothing errors — a fresh series just starts
and the existing history is orphaned. The first test pins them together.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import datahub.metadata.schema_classes as models

from undertow.history import VerdictRun, assertion_urn_for, read_history, summarise
from undertow.models import (
    Finding,
    FindingKind,
    RuledFinding,
    Severity,
    Verdict,
)
from undertow.reporter.datahub_writer import create_verdict_mcps

MODEL = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detector_v3,PROD)"


def run_event(millis: int, severity: str, blocking: int = 0, warning: int = 0) -> Any:
    return models.AssertionRunEventClass(
        timestampMillis=millis,
        runId=f"undertow-{millis}",
        assertionUrn=assertion_urn_for(MODEL),
        asserteeUrn="urn:li:dataset:(urn:li:dataPlatform:snowflake,transactions.raw,PROD)",
        status=models.AssertionRunStatusClass.COMPLETE,
        result=models.AssertionResultClass(
            type=(
                models.AssertionResultTypeClass.FAILURE
                if severity == "BLOCK"
                else models.AssertionResultTypeClass.SUCCESS
            ),
            nativeResults={
                "severity": severity,
                "blocking_count": str(blocking),
                "warning_count": str(warning),
                "assets_checked": "6",
            },
        ),
    )


class StubGraph:
    def __init__(self, events: list[Any]) -> None:
        self.events = events
        self.calls: list[dict[str, Any]] = []

    def get_timeseries_values(self, **kwargs: Any) -> list[Any]:
        self.calls.append(kwargs)
        return self.events[: kwargs.get("limit", 10)]


def test_the_assertion_urn_matches_the_one_write_back_uses() -> None:
    """Derived in two places; they must agree or history silently splits."""
    verdict = Verdict(
        model_urn=MODEL,
        severity=Severity.BLOCK,
        ruled_findings=(
            RuledFinding(
                finding=Finding(
                    kind=FindingKind.COLUMN_DROPPED,
                    subject_urn="urn:li:dataset:(urn:li:dataPlatform:snowflake,x,PROD)",
                    summary="dropped",
                ),
                severity=Severity.BLOCK,
                rule_id="COLUMN_DROPPED",
            ),
        ),
        assets_checked=6,
    )

    written = {
        mcp.entityUrn
        for mcp in create_verdict_mcps(verdict)
        if str(mcp.entityUrn).startswith("urn:li:assertion:")
    }

    assert assertion_urn_for(MODEL) in written


def test_the_urn_is_stable_across_calls() -> None:
    assert assertion_urn_for(MODEL) == assertion_urn_for(MODEL)


def test_different_models_get_different_assertions() -> None:
    other = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,churn_predictor_v1,PROD)"

    assert assertion_urn_for(MODEL) != assertion_urn_for(other)


def test_runs_come_back_newest_first() -> None:
    graph = StubGraph(
        [
            run_event(1_000_000_000_000, "CLEAR"),
            run_event(3_000_000_000_000, "BLOCK", blocking=1),
            run_event(2_000_000_000_000, "WARN", warning=2),
        ]
    )

    runs = read_history(MODEL, graph=graph)

    assert [r.severity for r in runs] == ["BLOCK", "WARN", "CLEAR"]


def test_counts_and_exit_codes_are_recovered() -> None:
    graph = StubGraph([run_event(3_000_000_000_000, "BLOCK", blocking=2, warning=1)])

    run = read_history(MODEL, graph=graph)[0]

    assert run.blocking == 2
    assert run.warning == 1
    assert run.assets_checked == 6
    assert run.exit_code() == 1
    assert run.passed is False


def test_a_warn_run_reports_the_exit_code_it_gave_ci() -> None:
    """WARN exits 0. History has to agree with what the gate actually returned."""
    graph = StubGraph([run_event(3_000_000_000_000, "WARN", warning=1)])

    run = read_history(MODEL, graph=graph)[0]

    assert run.exit_code() == 0
    assert run.passed is True


def test_timestamps_are_timezone_aware() -> None:
    graph = StubGraph([run_event(1_700_000_000_000, "CLEAR")])

    run = read_history(MODEL, graph=graph)[0]

    assert run.checked_at.tzinfo is not None
    assert run.checked_at == datetime.fromtimestamp(1_700_000_000, tz=UTC)


def test_the_limit_is_passed_through() -> None:
    graph = StubGraph([run_event(1_000_000_000_000 + i, "CLEAR") for i in range(50)])

    read_history(MODEL, graph=graph, limit=5)

    assert graph.calls[0]["limit"] == 5


def test_no_history_is_distinguishable_from_all_clear() -> None:
    """Reporting an empty series as 'nothing ever failed' would be a lie."""
    assert read_history(MODEL, graph=StubGraph([])) == []
    assert "No recorded runs" in summarise([])


def test_the_summary_counts_blocks() -> None:
    runs = [
        VerdictRun(datetime.now(UTC), "FAILURE", "BLOCK", 1, 0, 6, "a"),
        VerdictRun(datetime.now(UTC), "SUCCESS", "CLEAR", 0, 0, 6, "b"),
    ]

    assert "2 run(s)" in summarise(runs)
    assert "1 blocked" in summarise(runs)
