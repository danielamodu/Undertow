"""Tests for Undertow DataHub write-back reporter."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import datahub.metadata.schema_classes as models
from undertow.models import (
    Finding,
    FindingKind,
    RuledFinding,
    Severity,
    Verdict,
)
from undertow.reporter.datahub_writer import (
    MLModelPatchBuilder,
    create_verdict_mcps,
    write_verdict_to_datahub,
)


def test_mlmodel_patch_builder() -> None:
    model_urn = "urn:li:mlModel:(urn:li:dataPlatform:mlflow,fraud_detector_v3,PROD)"
    builder = MLModelPatchBuilder(model_urn)
    builder.set_structured_property("undertow_risk_verdict", "BLOCK")
    builder.set_structured_property("undertow_last_checked", "2026-08-05T00:00:00Z")

    mcps = builder.build()
    assert len(mcps) == 1
    mcp = mcps[0]
    assert mcp.entityUrn == model_urn
    assert mcp.aspectName == "structuredProperties"


def test_create_verdict_mcps_blocking() -> None:
    model_urn = "urn:li:mlModel:(urn:li:dataPlatform:mlflow,fraud_detector_v3,PROD)"
    finding = Finding(
        kind=FindingKind.COLUMN_DROPPED,
        subject_urn="raw.payments",
        summary="Column dropped",
    )
    ruled = RuledFinding(finding=finding, severity=Severity.BLOCK, rule_id="rule1")
    verdict = Verdict(
        model_urn=model_urn,
        severity=Severity.BLOCK,
        ruled_findings=(ruled,),
        assets_checked=10,
        checked_at=datetime.now(timezone.utc),
    )

    mcps = create_verdict_mcps(verdict, pr_url="https://github.com/org/repo/pull/12")
    aspect_names = [m.aspectName for m in mcps]

    assert "assertionInfo" in aspect_names
    assert "assertionRunEvent" in aspect_names
    assert "globalTags" in aspect_names
    assert "structuredProperties" in aspect_names
    assert "institutionalMemory" in aspect_names

    # Check assertion run result type for BLOCK verdict
    run_event_mcp = next(m for m in mcps if m.aspectName == "assertionRunEvent")
    assert run_event_mcp.aspect.result.type == models.AssertionResultTypeClass.FAILURE

    # Check tag for BLOCK verdict
    tags_mcp = next(m for m in mcps if m.aspectName == "globalTags")
    assert any("undertow:blocked" in t.tag for t in tags_mcp.aspect.tags)


def test_create_verdict_mcps_cleared() -> None:
    model_urn = "urn:li:mlModel:(urn:li:dataPlatform:mlflow,fraud_detector_v3,PROD)"
    verdict = Verdict(
        model_urn=model_urn,
        severity=Severity.CLEAR,
        ruled_findings=(),
        assets_checked=10,
        checked_at=datetime.now(timezone.utc),
    )

    mcps = create_verdict_mcps(verdict)

    # Check assertion run result type for CLEAR verdict
    run_event_mcp = next(m for m in mcps if m.aspectName == "assertionRunEvent")
    assert run_event_mcp.aspect.result.type == models.AssertionResultTypeClass.SUCCESS

    # Check tag for CLEAR verdict
    tags_mcp = next(m for m in mcps if m.aspectName == "globalTags")
    assert any("undertow:cleared" in t.tag for t in tags_mcp.aspect.tags)


def test_write_verdict_to_datahub_emitter() -> None:
    model_urn = "urn:li:mlModel:(urn:li:dataPlatform:mlflow,fraud_detector_v3,PROD)"
    verdict = Verdict(
        model_urn=model_urn,
        severity=Severity.CLEAR,
        ruled_findings=(),
        assets_checked=5,
        checked_at=datetime.now(timezone.utc),
    )

    mock_emitter = MagicMock()
    emitted = write_verdict_to_datahub(verdict, emitter=mock_emitter)

    assert mock_emitter.emit_mcp.call_count >= len(emitted)
    assert "assertionInfo" in emitted
    assert "globalTags" in emitted
