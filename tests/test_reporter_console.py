"""Tests for Undertow Rich Console reporter."""

from datetime import datetime, timezone

from undertow.models import (
    AttributionHop,
    AttributionPath,
    Finding,
    FindingKind,
    RuledFinding,
    Severity,
    Verdict,
)
from undertow.reporter.console import format_console


def test_format_console_blocking() -> None:
    model_urn = "urn:li:mlModel:(urn:li:dataPlatform:mlflow,fraud_detector_v3,PROD)"
    ds_urn = "raw.payments.amount_usd"
    feat_urn = "features.txn_aggregates.avg_txn_30d"

    path = AttributionPath(
        hops=(
            AttributionHop(urn="raw.payments", entity_type="dataset", column="amount_usd"),
            AttributionHop(urn="staging.payments_clean", entity_type="dataset", via="DownstreamOf"),
            AttributionHop(urn=feat_urn, entity_type="mlFeature", via="DerivedFrom"),
            AttributionHop(urn=model_urn, entity_type="mlModel", via="Consumes"),
        ),
        owners=("@data-eng-tom",),
    )

    finding = Finding(
        kind=FindingKind.COLUMN_DROPPED,
        subject_urn="raw.payments",
        subject_column="amount_usd",
        affected_feature_urn=feat_urn,
        summary="upstream column removed",
        path=path,
    )

    ruled = RuledFinding(
        finding=finding,
        severity=Severity.BLOCK,
        rule_id="upstream-column-dropped",
    )

    verdict = Verdict(
        model_urn=model_urn,
        severity=Severity.BLOCK,
        ruled_findings=(ruled,),
        assets_checked=12,
        checked_at=datetime.now(timezone.utc),
    )

    output = format_console(verdict, written_to_datahub=True)

    assert "🔴 BLOCK" in output
    assert "BLOCKING" in output
    assert "avg_txn_30d" in output
    assert "upstream column removed" in output
    assert "@data-eng-tom" in output
    assert "Written to DataHub" in output


def test_format_console_warning_and_clear() -> None:
    model_urn = "urn:li:mlModel:(urn:li:dataPlatform:mlflow,fraud_detector_v3,PROD)"

    finding = Finding(
        kind=FindingKind.MEAN_SHIFT,
        subject_urn="raw.merchants",
        affected_feature_urn="features.txn_aggregates.merchant_risk_score",
        summary="distribution shift",
    )

    ruled = RuledFinding(
        finding=finding,
        severity=Severity.WARN,
        rule_id="distribution-shift",
    )

    verdict_warn = Verdict(
        model_urn=model_urn,
        severity=Severity.WARN,
        ruled_findings=(ruled,),
        assets_checked=5,
        checked_at=datetime.now(timezone.utc),
    )

    output_warn = format_console(verdict_warn)
    assert "🟡 WARN" in output_warn
    assert "WARNING" in output_warn

    verdict_clear = Verdict(
        model_urn=model_urn,
        severity=Severity.CLEAR,
        ruled_findings=(),
        assets_checked=5,
        checked_at=datetime.now(timezone.utc),
    )

    output_clear = format_console(verdict_clear)
    assert "🟢 CLEAR" in output_clear
    assert "no material change" in output_clear
