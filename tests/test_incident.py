"""Tests for cross-model incident correlation.

`check --all` gates each model independently; `correlate` re-groups the
resulting verdicts by shared root cause afterward. Nothing here computes a
verdict or a severity — those come in already decided, exactly as the policy
engine produced them.
"""

from __future__ import annotations

from undertow.incident import correlate, format_incident_lines
from undertow.models import (
    AttributionHop,
    AttributionPath,
    Finding,
    FindingKind,
    RuledFinding,
    Severity,
    Verdict,
)

RAW = "urn:li:dataset:(urn:li:dataPlatform:snowflake,transactions.raw,PROD)"
FRAUD = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detector_v3,PROD)"
CHURN = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,churn_predictor_v1,PROD)"
OTHER_ROOT = "urn:li:dataset:(urn:li:dataPlatform:snowflake,customers.raw,PROD)"


def _blocked_verdict(model_urn: str, root_urn: str, owners: tuple[str, ...] = ()) -> Verdict:
    finding = Finding(
        kind=FindingKind.COLUMN_DROPPED,
        subject_urn=root_urn,
        subject_column="amount",
        summary=f"Column `amount` was dropped from {root_urn}",
        path=AttributionPath(
            hops=(
                AttributionHop(urn=root_urn, entity_type="dataset", column="amount"),
                AttributionHop(urn=model_urn, entity_type="mlModel", via="Consumes"),
            ),
            owners=owners,
        ),
    )
    return Verdict(
        model_urn=model_urn,
        severity=Severity.BLOCK,
        ruled_findings=(
            RuledFinding(finding=finding, severity=Severity.BLOCK, rule_id="COLUMN_DROPPED"),
        ),
        assets_checked=2,
    )


def _clear_verdict(model_urn: str) -> Verdict:
    return Verdict(model_urn=model_urn, severity=Severity.CLEAR, assets_checked=2)


def test_two_models_sharing_a_root_become_one_incident() -> None:
    verdicts = [
        _blocked_verdict(FRAUD, RAW, owners=("urn:li:corpuser:data_eng_tom",)),
        _blocked_verdict(CHURN, RAW, owners=("urn:li:corpuser:data_eng_tom",)),
    ]

    incidents = correlate(verdicts)

    assert len(incidents) == 1
    assert incidents[0].root_urn == RAW
    assert incidents[0].model_count == 2
    assert {m for m, _ in incidents[0].affected} == {FRAUD, CHURN}
    assert incidents[0].root_owners == ("urn:li:corpuser:data_eng_tom",)


def test_one_model_alone_is_not_an_incident() -> None:
    """The whole point is distinguishing 'shared' from 'isolated' — a single
    affected model is just a finding, already fully visible in its own box.
    """
    incidents = correlate([_blocked_verdict(FRAUD, RAW)])

    assert incidents == []


def test_two_models_with_different_roots_are_two_separate_findings_not_one_incident() -> None:
    verdicts = [
        _blocked_verdict(FRAUD, RAW),
        _blocked_verdict(CHURN, OTHER_ROOT),
    ]

    incidents = correlate(verdicts)

    assert incidents == []


def test_clear_verdicts_never_produce_an_incident() -> None:
    incidents = correlate([_clear_verdict(FRAUD), _clear_verdict(CHURN)])

    assert incidents == []


def test_findings_without_an_attribution_path_do_not_correlate() -> None:
    """No path means no root to group on — exactly as visible as before, no
    crash, no phantom incident.
    """
    finding = Finding(
        kind=FindingKind.COLUMN_DROPPED,
        subject_urn=RAW,
        summary="dropped, but unattributed",
    )
    verdict = Verdict(
        model_urn=FRAUD,
        severity=Severity.BLOCK,
        ruled_findings=(
            RuledFinding(finding=finding, severity=Severity.BLOCK, rule_id="COLUMN_DROPPED"),
        ),
        assets_checked=1,
    )

    assert correlate([verdict, verdict]) == []


def test_worst_severity_wins_when_models_disagree() -> None:
    """One model blocks on the root, another only warns on it — the incident
    reports the worse of the two, since that is what a reader needs to see
    first.
    """
    warn_finding = Finding(
        kind=FindingKind.MEAN_SHIFT,
        subject_urn=RAW,
        summary="drift",
        path=AttributionPath(
            hops=(
                AttributionHop(urn=RAW, entity_type="dataset"),
                AttributionHop(urn=CHURN, entity_type="mlModel", via="Consumes"),
            )
        ),
    )
    warn_verdict = Verdict(
        model_urn=CHURN,
        severity=Severity.WARN,
        ruled_findings=(
            RuledFinding(finding=warn_finding, severity=Severity.WARN, rule_id="MEAN_SHIFT"),
        ),
        assets_checked=2,
    )

    incidents = correlate([_blocked_verdict(FRAUD, RAW), warn_verdict])

    assert len(incidents) == 1
    assert incidents[0].worst_severity is Severity.BLOCK


def test_sorted_by_how_many_models_are_affected_first() -> None:
    three_way_root = "urn:li:dataset:(urn:li:dataPlatform:snowflake,shared.raw,PROD)"
    third_model = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,third_model,PROD)"
    verdicts = [
        _blocked_verdict(FRAUD, RAW),
        _blocked_verdict(CHURN, RAW),
        _blocked_verdict(FRAUD, three_way_root),
        _blocked_verdict(CHURN, three_way_root),
        _blocked_verdict(third_model, three_way_root),
    ]

    incidents = correlate(verdicts)

    assert len(incidents) == 2
    assert incidents[0].root_urn == three_way_root  # 3 models, listed first
    assert incidents[0].model_count == 3
    assert incidents[1].root_urn == RAW  # 2 models


def test_format_incident_lines_names_every_affected_model() -> None:
    incidents = correlate(
        [
            _blocked_verdict(FRAUD, RAW, owners=("urn:li:corpuser:data_eng_tom",)),
            _blocked_verdict(CHURN, RAW, owners=("urn:li:corpuser:data_eng_tom",)),
        ]
    )

    lines = format_incident_lines(incidents[0])

    assert "data_eng_tom" in lines[0]
    assert any("fraud_detector_v3" in line for line in lines)
    assert any("churn_predictor_v1" in line for line in lines)
