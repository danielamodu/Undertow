"""Tests for the attributor component.

Verifies finding enrichment with AttributionPaths, owner resolution,
unattributed finding handling, and query evidence attachment — using mock fixtures.
"""

from __future__ import annotations

import pytest

from undertow.attributor import attribute_findings
from undertow.models import (
    AssetSnapshot,
    AttributionHop,
    AttributionPath,
    DependencyFootprint,
    Finding,
    FindingKind,
    UndertowSnapshot,
)

MODEL_URN = "urn:li:mlModel:(urn:li:dataPlatform:mlflow,fraud_detector_v3,PROD)"
FEATURE_1 = "urn:li:mlFeature:(txn_aggregates,avg_txn_30d)"
STAGING = "urn:li:dataset:(urn:li:dataPlatform:snowflake,staging.payments_clean,PROD)"
RAW_PAYMENTS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,raw.payments,PROD)"
UNKNOWN_DATASET = "urn:li:dataset:(urn:li:dataPlatform:snowflake,unknown.dataset,PROD)"

TOM = "urn:li:corpuser:data-eng-tom"


@pytest.fixture
def mock_footprint() -> DependencyFootprint:
    raw_snap = AssetSnapshot(urn=RAW_PAYMENTS, owners=(TOM,))
    staging_snap = AssetSnapshot(urn=STAGING, owners=(TOM,))
    model_snap = AssetSnapshot(urn=MODEL_URN, entity_type="mlModel")

    snapshot = UndertowSnapshot(
        model_urn=MODEL_URN,
        assets={
            RAW_PAYMENTS: raw_snap,
            STAGING: staging_snap,
            MODEL_URN: model_snap,
        },
    )

    path_raw = AttributionPath(
        hops=(
            AttributionHop(urn=RAW_PAYMENTS, entity_type="dataset"),
            AttributionHop(urn=STAGING, entity_type="dataset", via="DownstreamOf"),
            AttributionHop(urn=FEATURE_1, entity_type="mlFeature", via="DerivedFrom"),
            AttributionHop(urn=MODEL_URN, entity_type="mlModel", via="Consumes"),
        ),
        owners=(TOM,),
    )

    return DependencyFootprint(
        model_urn=MODEL_URN,
        snapshot=snapshot,
        paths={RAW_PAYMENTS: path_raw},
    )


def test_attribute_findings_attaches_path_and_owners(mock_footprint: DependencyFootprint) -> None:
    finding = Finding(
        kind=FindingKind.COLUMN_DROPPED,
        subject_urn=RAW_PAYMENTS,
        subject_column="amount_usd",
        summary="Column dropped",
    )

    enriched = attribute_findings([finding], mock_footprint)
    assert len(enriched) == 1
    result = enriched[0]

    assert result.path is not None
    assert result.path.root.urn == RAW_PAYMENTS
    assert result.path.root.column == "amount_usd"
    assert result.path.leaf.urn == MODEL_URN
    assert result.path.owners == (TOM,)


def test_attribute_findings_handles_unattributed(mock_footprint: DependencyFootprint) -> None:
    finding = Finding(
        kind=FindingKind.COLUMN_DROPPED,
        subject_urn=UNKNOWN_DATASET,
        summary="Unattributed column dropped",
    )

    enriched = attribute_findings([finding], mock_footprint)
    assert len(enriched) == 1
    result = enriched[0]

    assert result.path is None
    assert result.evidence.get("unattributed") is True


def test_attribute_findings_attaches_query_evidence(mock_footprint: DependencyFootprint) -> None:
    finding = Finding(
        kind=FindingKind.COLUMN_DROPPED,
        subject_urn=RAW_PAYMENTS,
        summary="Column dropped",
    )

    queries = {RAW_PAYMENTS: "SELECT amount_usd FROM raw.payments"}
    enriched = attribute_findings([finding], mock_footprint, query_evidence=queries)
    result = enriched[0]

    assert result.evidence.get("query_sql") == "SELECT amount_usd FROM raw.payments"


def test_attribute_findings_is_pure_and_does_not_mutate_inputs(
    mock_footprint: DependencyFootprint,
) -> None:
    finding = Finding(
        kind=FindingKind.COLUMN_DROPPED,
        subject_urn=RAW_PAYMENTS,
        summary="Column dropped",
    )

    enriched = attribute_findings([finding], mock_footprint)
    assert finding.path is None
    assert enriched[0].path is not None
