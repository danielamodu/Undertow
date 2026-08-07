"""Tests for the graph resolver and traversal engine.

Tests the BFS traversal, cycle guards, hop caps, feature table exclusion, and
attribution path generation using mock LineageSource implementations — no live DataHub needed.
"""

from __future__ import annotations

import pytest

from undertow.models import DependencyFootprint
from undertow.resolver.base import (
    LineageEdge,
    LineageNode,
    LineageSource,
    SchemaFieldInfo,
    parse_entity_type,
)
from undertow.resolver.mcp_source import McpLineageSource
from undertow.resolver.sdk_source import SdkLineageSource
from undertow.resolver.traversal import resolve_footprint

MODEL_URN = "urn:li:mlModel:(urn:li:dataPlatform:mlflow,fraud_detector_v3,PROD)"
FEATURE_1 = "urn:li:mlFeature:(txn_aggregates,avg_txn_30d)"
FEATURE_2 = "urn:li:mlFeature:(txn_aggregates,txn_velocity)"
FEATURE_TABLE = "urn:li:mlFeatureTable:(urn:li:dataPlatform:feast,txn_aggregates)"
STAGING_PAYMENTS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,staging.payments_clean,PROD)"
RAW_PAYMENTS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,raw.payments,PROD)"
RAW_TXNS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,raw.transactions,PROD)"


class DummyProps:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class MockLineageSource(LineageSource):
    """Mock LineageSource representing a real ML graph."""

    def __init__(self) -> None:
        self.nodes: dict[str, LineageNode] = {
            MODEL_URN: LineageNode(
                urn=MODEL_URN,
                entity_type="mlModel",
                aspects={
                    "mlModelProperties": DummyProps(mlFeatures=[FEATURE_1, FEATURE_2]),
                    "ownership": DummyProps(owners=["urn:li:corpuser:ml-eng-alex"]),
                },
            ),
            FEATURE_1: LineageNode(
                urn=FEATURE_1,
                entity_type="mlFeature",
                aspects={
                    "mlFeatureProperties": DummyProps(sources=[STAGING_PAYMENTS]),
                },
            ),
            FEATURE_2: LineageNode(
                urn=FEATURE_2,
                entity_type="mlFeature",
                aspects={
                    "mlFeatureProperties": DummyProps(sources=[STAGING_PAYMENTS]),
                },
            ),
            FEATURE_TABLE: LineageNode(
                urn=FEATURE_TABLE,
                entity_type="mlFeatureTable",
                aspects={},
            ),
            STAGING_PAYMENTS: LineageNode(
                urn=STAGING_PAYMENTS,
                entity_type="dataset",
                aspects={
                    "ownership": DummyProps(owners=["urn:li:corpuser:data-eng-tom"]),
                },
            ),
            RAW_PAYMENTS: LineageNode(
                urn=RAW_PAYMENTS,
                entity_type="dataset",
                aspects={
                    "ownership": DummyProps(owners=["urn:li:corpuser:data-eng-tom"]),
                },
            ),
            RAW_TXNS: LineageNode(
                urn=RAW_TXNS,
                entity_type="dataset",
                aspects={},
            ),
        }
        self.lineage_map: dict[str, list[LineageEdge]] = {
            STAGING_PAYMENTS: [
                LineageEdge(source_urn=RAW_PAYMENTS, target_urn=STAGING_PAYMENTS, relationship="DownstreamOf"),
                LineageEdge(source_urn=RAW_TXNS, target_urn=STAGING_PAYMENTS, relationship="DownstreamOf"),
            ],
        }
        self.schema_map: dict[str, list[SchemaFieldInfo]] = {
            RAW_PAYMENTS: [
                SchemaFieldInfo(field_path="amount_usd", data_type="number", native_type="DECIMAL(10,2)"),
                SchemaFieldInfo(field_path="user_id", data_type="string", native_type="VARCHAR(64)"),
            ],
            STAGING_PAYMENTS: [
                SchemaFieldInfo(field_path="amount", data_type="number", native_type="DECIMAL(10,2)"),
            ],
        }

    def get_entity(self, urn: str) -> LineageNode | None:
        return self.nodes.get(urn)

    def get_entities(self, urns: list[str]) -> dict[str, LineageNode]:
        return {u: self.nodes[u] for u in urns if u in self.nodes}

    def get_lineage(
        self, urn: str, direction: str = "UPSTREAM", hops: int = 1
    ) -> list[LineageEdge]:
        return self.lineage_map.get(urn, [])

    def list_schema_fields(self, urn: str) -> list[SchemaFieldInfo]:
        return self.schema_map.get(urn, [])


# --------------------------------------------------------------------------
# Traversal tests
# --------------------------------------------------------------------------


def test_resolve_footprint_traverses_two_hop_path() -> None:
    source = MockLineageSource()
    footprint = resolve_footprint(MODEL_URN, source, max_hops=5)

    assert isinstance(footprint, DependencyFootprint)
    assert footprint.model_urn == MODEL_URN
    assert MODEL_URN in footprint.snapshot.assets
    assert FEATURE_1 in footprint.snapshot.assets
    assert FEATURE_2 in footprint.snapshot.assets
    assert STAGING_PAYMENTS in footprint.snapshot.assets
    assert RAW_PAYMENTS in footprint.snapshot.assets
    assert RAW_TXNS in footprint.snapshot.assets


def test_feature_tables_are_excluded_from_lineage_path() -> None:
    source = MockLineageSource()

    source.lineage_map[STAGING_PAYMENTS].append(
        LineageEdge(source_urn=FEATURE_TABLE, target_urn=STAGING_PAYMENTS, relationship="DownstreamOf")
    )

    footprint = resolve_footprint(MODEL_URN, source, max_hops=5)
    assert FEATURE_TABLE not in footprint.snapshot.assets


def test_max_hops_is_respected() -> None:
    source = MockLineageSource()
    footprint = resolve_footprint(MODEL_URN, source, max_hops=1)
    assert STAGING_PAYMENTS not in footprint.snapshot.assets
    assert RAW_PAYMENTS not in footprint.snapshot.assets


def test_cycle_guard_prevents_infinite_loops() -> None:
    source = MockLineageSource()
    source.lineage_map[RAW_PAYMENTS] = [
        LineageEdge(source_urn=STAGING_PAYMENTS, target_urn=RAW_PAYMENTS, relationship="DownstreamOf")
    ]

    footprint = resolve_footprint(MODEL_URN, source, max_hops=10)
    assert len(footprint.snapshot.assets) > 0


def test_attribution_path_structure() -> None:
    source = MockLineageSource()
    footprint = resolve_footprint(MODEL_URN, source, max_hops=5)

    path = footprint.paths[RAW_PAYMENTS]
    assert path.root.urn == RAW_PAYMENTS
    assert path.leaf.urn == MODEL_URN
    assert path.depth == 3
    assert any("data-eng-tom" in owner for owner in path.owners)



def test_dataset_feeds_features_is_populated() -> None:
    source = MockLineageSource()
    footprint = resolve_footprint(MODEL_URN, source, max_hops=5)

    staging_snap = footprint.snapshot.asset(STAGING_PAYMENTS)
    assert staging_snap is not None
    assert FEATURE_1 in staging_snap.feeds_features
    assert FEATURE_2 in staging_snap.feeds_features


def test_urn_entity_type_parser() -> None:
    assert parse_entity_type(MODEL_URN) == "mlModel"
    assert parse_entity_type(FEATURE_1) == "mlFeature"
    assert parse_entity_type(FEATURE_TABLE) == "mlFeatureTable"
    assert parse_entity_type(RAW_PAYMENTS) == "dataset"
    assert parse_entity_type("invalid_urn") == "unknown"


def test_mcp_lineage_source_stub() -> None:
    mcp_source = McpLineageSource()
    node = mcp_source.get_entity(MODEL_URN)
    assert node is not None
    assert node.entity_type == "mlModel"


def test_sdk_lineage_source_stub() -> None:
    sdk_source = SdkLineageSource()
    node = sdk_source.get_entity(RAW_PAYMENTS)
    assert node is not None
    assert node.entity_type == "dataset"
