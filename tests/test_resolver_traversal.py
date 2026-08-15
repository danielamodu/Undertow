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
        # Every hop is a lineage edge, including the ML ones. This mirrors both
        # real backends: DataHub's lineage registry annotates `Consumes` and
        # `DerivedFrom` as lineage, so a plain lineage query returns them. The
        # previous mock put ML edges in aspects instead, which is a shape the MCP
        # server never produces — `entity_details.gql` selects no `mlFeatures`
        # field on `MLModel` and no `sources` on `MLFeature`.
        self.lineage_map: dict[str, list[LineageEdge]] = {
            MODEL_URN: [
                LineageEdge(source_urn=MODEL_URN, target_urn=FEATURE_1, relationship="Consumes"),
                LineageEdge(source_urn=MODEL_URN, target_urn=FEATURE_2, relationship="Consumes"),
            ],
            FEATURE_1: [
                LineageEdge(
                    source_urn=FEATURE_1, target_urn=STAGING_PAYMENTS, relationship="DerivedFrom"
                ),
            ],
            FEATURE_2: [
                LineageEdge(
                    source_urn=FEATURE_2, target_urn=STAGING_PAYMENTS, relationship="DerivedFrom"
                ),
            ],
            STAGING_PAYMENTS: [
                LineageEdge(
                    source_urn=RAW_PAYMENTS,
                    target_urn=STAGING_PAYMENTS,
                    relationship="DownstreamOf",
                ),
                LineageEdge(
                    source_urn=RAW_TXNS,
                    target_urn=STAGING_PAYMENTS,
                    relationship="DownstreamOf",
                ),
            ],
        }
        self.schema_map: dict[str, list[SchemaFieldInfo]] = {
            RAW_PAYMENTS: [
                SchemaFieldInfo(
                    field_path="amount_usd", data_type="number", native_type="DECIMAL(10,2)"
                ),
                SchemaFieldInfo(
                    field_path="user_id", data_type="string", native_type="VARCHAR(64)"
                ),
            ],
            STAGING_PAYMENTS: [
                SchemaFieldInfo(
                    field_path="amount", data_type="number", native_type="DECIMAL(10,2)"
                ),
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
        LineageEdge(
            source_urn=FEATURE_TABLE,
            target_urn=STAGING_PAYMENTS,
            relationship="DownstreamOf",
        )
    )

    footprint = resolve_footprint(MODEL_URN, source, max_hops=5)
    assert FEATURE_TABLE not in footprint.snapshot.assets


def test_profiles_are_only_fetched_for_datasets() -> None:
    """Asking GMS for a dataset profile about an mlModel costs ~28 seconds.

    `datasetProfile` is a dataset aspect. GMS answers a request for one about a
    feature or a model eventually, with nothing, after the SDK has retried. A
    six-asset footprint spent 85 of 87 seconds on three such calls.

    The traversal must not make them at all.
    """
    asked: list[str] = []

    class ProfileWatchingSource(MockLineageSource):
        def get_latest_profile(self, urn: str) -> dict[str, object] | None:
            asked.append(urn)
            return None

    resolve_footprint(MODEL_URN, ProfileWatchingSource(), max_hops=5)

    assert asked, "no profiles were fetched at all"
    non_datasets = [u for u in asked if ":dataset:" not in u]
    assert not non_datasets, (
        f"asked for dataset profiles about {non_datasets}, which costs seconds each "
        "and can only ever return None"
    )


def test_max_hops_is_respected() -> None:
    source = MockLineageSource()
    footprint = resolve_footprint(MODEL_URN, source, max_hops=1)
    assert STAGING_PAYMENTS not in footprint.snapshot.assets
    assert RAW_PAYMENTS not in footprint.snapshot.assets


def test_cycle_guard_prevents_infinite_loops() -> None:
    source = MockLineageSource()
    source.lineage_map[RAW_PAYMENTS] = [
        LineageEdge(
            source_urn=STAGING_PAYMENTS,
            target_urn=RAW_PAYMENTS,
            relationship="DownstreamOf",
        )
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


# --------------------------------------------------------------------------
# Hop-cap truncation
#
# The cap is a legitimate bound on traversal cost. Being quiet about hitting it
# is not: an asset beyond the cap is never fetched, so it is absent from
# `snapshot.assets`, so it drops out of `shared_urns()`, so it produces no
# finding — and a footprint that stopped early renders identically to one that
# walked the whole graph and found nothing.
# --------------------------------------------------------------------------


def test_hop_cap_records_what_it_left_behind() -> None:
    source = MockLineageSource()
    footprint = resolve_footprint(MODEL_URN, source, max_hops=1)

    # Both features sit on the cap with STAGING_PAYMENTS unwalked behind them.
    assert footprint.truncated
    assert set(footprint.truncated_urns) == {FEATURE_1, FEATURE_2}
    assert STAGING_PAYMENTS not in footprint.snapshot.assets

    summary = footprint.truncation_summary()
    assert summary is not None
    assert "1-hop limit" in summary


def test_a_complete_walk_is_not_marked_truncated() -> None:
    source = MockLineageSource()
    footprint = resolve_footprint(MODEL_URN, source, max_hops=5)

    assert not footprint.truncated
    assert footprint.truncated_urns == ()
    assert footprint.truncation_summary() is None


def test_a_leaf_sitting_exactly_on_the_cap_is_not_truncation() -> None:
    """Reaching the cap is only a problem if something was behind it.

    RAW_PAYMENTS and RAW_TXNS are at depth 3 with no upstreams of their own, so
    `max_hops=3` walks the entire graph and happens to stop at the boundary.
    Reporting that would fire the warning on healthy footprints until a team
    learned to ignore it, which is how a real signal gets trained out.
    """
    source = MockLineageSource()
    footprint = resolve_footprint(MODEL_URN, source, max_hops=3)

    assert RAW_PAYMENTS in footprint.snapshot.assets
    assert RAW_TXNS in footprint.snapshot.assets
    assert not footprint.truncated


def test_truncation_is_recorded_at_the_node_that_actually_stopped() -> None:
    """The report names the boundary, not everything on the path to it.

    At `max_hops=2` the features are walked through cleanly and STAGING is the
    node left holding unvisited upstreams. Marking its children too would make
    the count meaningless as a measure of how much graph went unseen.
    """
    source = MockLineageSource()
    footprint = resolve_footprint(MODEL_URN, source, max_hops=2)

    assert STAGING_PAYMENTS in footprint.snapshot.assets
    assert footprint.truncated_urns == (STAGING_PAYMENTS,)


# --------------------------------------------------------------------------
# Column-level feature attribution
# --------------------------------------------------------------------------


def _schema_field(dataset_urn: str, column: str) -> str:
    return f"urn:li:schemaField:({dataset_urn},{column})"


class ColumnLineageSource(MockLineageSource):
    """A source that can answer column-level questions, as the MCP server can."""

    def __init__(self) -> None:
        super().__init__()
        self.column_calls: list[tuple[str, str]] = []
        self.column_map: dict[tuple[str, str], list[str]] = {
            # staging.amount is built from raw.payments.amount_usd...
            (STAGING_PAYMENTS, "amount"): [_schema_field(RAW_PAYMENTS, "amount_usd")],
            # ...which is itself built from raw.transactions.amount_raw.
            (RAW_PAYMENTS, "amount_usd"): [_schema_field(RAW_TXNS, "amount_raw")],
        }

    def get_column_lineage(self, urn: str, column: str, hops: int = 1) -> list[LineageEdge]:
        self.column_calls.append((urn, column))
        return [
            LineageEdge(source_urn=urn, target_urn=target, relationship="DownstreamOf")
            for target in self.column_map.get((urn, column), [])
        ]


def test_column_features_narrow_asset_level_attribution() -> None:
    """`feeds_features` is asset-level; this is what makes it per-column.

    Without it, dropping *any* column of an upstream table implicates every
    feature that table's descendants feed — including columns nothing
    downstream reads.
    """
    footprint = resolve_footprint(MODEL_URN, ColumnLineageSource(), max_hops=5)

    raw = footprint.snapshot.asset(RAW_PAYMENTS)
    assert raw is not None
    assert raw.column_features == {"amount_usd": (FEATURE_1, FEATURE_2)}

    # The column that actually flows downstream names the features it reaches...
    assert raw.features_for("amount_usd") == (FEATURE_1, FEATURE_2)
    # ...and the one that does not is no longer tarred with them.
    assert raw.features_for("user_id") == ()


def test_column_features_propagate_transitively() -> None:
    """Attribution has to survive intermediate layers, not just one hop."""
    footprint = resolve_footprint(MODEL_URN, ColumnLineageSource(), max_hops=5)

    txns = footprint.snapshot.asset(RAW_TXNS)
    assert txns is not None
    assert txns.column_features == {"amount_raw": (FEATURE_1, FEATURE_2)}


def test_column_features_are_skipped_when_the_source_cannot_answer() -> None:
    """Only the MCP source resolves column lineage today.

    Everything else must degrade to the asset-level answer rather than to
    silence — an empty `column_features` reads as *unknown*, and `features_for`
    falls back. This is what lets the pass be added without moving any verdict.
    """
    source = MockLineageSource()
    assert not hasattr(source, "get_column_lineage")

    footprint = resolve_footprint(MODEL_URN, source, max_hops=5)

    raw = footprint.snapshot.asset(RAW_PAYMENTS)
    staging = footprint.snapshot.asset(STAGING_PAYMENTS)
    assert raw is not None and staging is not None
    assert raw.column_features == {}
    # The asset-level answer is untouched and still resolves.
    assert set(staging.feeds_features) == {FEATURE_1, FEATURE_2}


def test_a_failing_column_query_does_not_lose_the_footprint() -> None:
    """Fine-grained lineage is enrichment; it must never take down resolution."""

    class BrokenColumnSource(MockLineageSource):
        def get_column_lineage(
            self, urn: str, column: str, hops: int = 1
        ) -> list[LineageEdge]:
            raise RuntimeError("fine-grained lineage unavailable")

    footprint = resolve_footprint(MODEL_URN, BrokenColumnSource(), max_hops=5)

    assert RAW_PAYMENTS in footprint.snapshot.assets
    raw = footprint.snapshot.asset(RAW_PAYMENTS)
    assert raw is not None
    assert raw.column_features == {}


def test_column_lineage_is_only_asked_about_feature_feeding_paths() -> None:
    """The walk starts at datasets that feed features, not at every asset.

    Each query is a round trip, and finding #3 in this repo is that nothing
    caches them yet. Asking about columns that could not reach a feature would
    be spending that cost for an answer nobody reads.
    """
    source = ColumnLineageSource()
    resolve_footprint(MODEL_URN, source, max_hops=5)

    asked = {urn for urn, _ in source.column_calls}
    assert MODEL_URN not in asked
    assert FEATURE_1 not in asked
    assert STAGING_PAYMENTS in asked


def test_urn_entity_type_parser() -> None:
    assert parse_entity_type(MODEL_URN) == "mlModel"
    assert parse_entity_type(FEATURE_1) == "mlFeature"
    assert parse_entity_type(FEATURE_TABLE) == "mlFeatureTable"
    assert parse_entity_type(RAW_PAYMENTS) == "dataset"
    assert parse_entity_type("invalid_urn") == "unknown"


def test_zero_valued_profile_statistics_survive_snapshotting() -> None:
    """Zero is a measurement, not a missing value.

    The reader previously used `getattr(x, k, None) or _field(x, k)`, which
    collapsed `rowCount=0`, `nullCount=0`, and `min="0"` into None. The differ
    reads None as *cannot assess*, so an empty table or an all-null column —
    precisely the states worth blocking on — registered as no signal at all.
    """

    class ZeroSource(MockLineageSource):
        def __init__(self) -> None:
            super().__init__()
            self.nodes[RAW_PAYMENTS] = LineageNode(
                urn=RAW_PAYMENTS,
                entity_type="dataset",
                aspects={
                    "datasetProfile": DummyProps(
                        rowCount=0,
                        columnCount=0,
                        fieldProfiles=[
                            DummyProps(
                                fieldPath="amount_usd",
                                nullCount=0,
                                nullProportion=0.0,
                                uniqueCount=0,
                                min=0,
                                mean=0.0,
                                stdev=0,
                            )
                        ],
                    ),
                },
            )

    footprint = resolve_footprint(MODEL_URN, ZeroSource(), max_hops=5)
    snap = footprint.snapshot.asset(RAW_PAYMENTS)

    assert snap is not None
    assert snap.profile is not None
    assert snap.profile.row_count == 0
    assert snap.profile.column_count == 0

    fp = snap.profile.field_profile("amount_usd")
    assert fp is not None
    assert fp.null_count == 0
    assert fp.null_proportion == 0.0
    assert fp.unique_count == 0
    assert fp.min == "0"
    assert fp.mean == "0.0"
    assert fp.stdev == "0"


def test_mcp_source_refuses_to_be_built_without_an_executor() -> None:
    """A source that cannot reach DataHub must not be constructible.

    The previous revision defaulted the executor to None and answered every
    query with an empty collection, which the differ reads as "nothing changed"
    and the engine reports as CLEAR.
    """
    with pytest.raises(ValueError, match="requires a tool executor"):
        McpLineageSource(None)  # type: ignore[arg-type]


def test_mcp_get_lineage_uses_the_servers_actual_signature() -> None:
    """`get_lineage` takes `upstream: bool` — not `direction: str`.

    Pinned because the mismatch is invisible until it hits a live server.
    """
    calls: list[tuple[str, dict]] = []

    def executor(name: str, args: dict) -> object:
        calls.append((name, args))
        return {
            "upstreams": {
                "searchResults": [
                    {"entity": {"urn": RAW_PAYMENTS, "type": "DATASET"}},
                ]
            }
        }

    source = McpLineageSource(executor)
    edges = source.get_lineage(FEATURE_1, direction="UPSTREAM", hops=1)

    name, args = calls[0]
    assert name == "get_lineage"
    assert args["upstream"] is True
    assert "direction" not in args
    assert [e.target_urn for e in edges] == [RAW_PAYMENTS]
    # mlFeature -> dataset is DerivedFrom in DataHub's lineage registry.
    assert edges[0].relationship == "DerivedFrom"


def test_mcp_downstream_reads_the_downstreams_key() -> None:
    def executor(name: str, args: dict) -> object:
        assert args["upstream"] is False
        return {"downstreams": {"searchResults": [{"entity": {"urn": MODEL_URN}}]}}

    source = McpLineageSource(executor)
    edges = source.get_lineage(RAW_PAYMENTS, direction="DOWNSTREAM", hops=1)
    assert [e.target_urn for e in edges] == [MODEL_URN]


def test_mcp_get_entities_normalises_graphql_tag_and_owner_shapes() -> None:
    """The MCP server speaks GraphQL; the traversal reads SDK aspect names."""

    def executor(name: str, args: dict) -> object:
        return [
            {
                "urn": RAW_PAYMENTS,
                "type": "DATASET",
                "tags": {"tags": [{"tag": {"urn": "urn:li:tag:PII"}}]},
                "ownership": {"owners": [{"owner": {"urn": "urn:li:corpuser:tom"}}]},
            }
        ]

    source = McpLineageSource(executor)
    node = source.get_entity(RAW_PAYMENTS)

    assert node is not None
    assert node.aspects["globalTags"] == {"tags": [{"tag": "urn:li:tag:PII"}]}
    assert node.aspects["ownership"] == {"owners": [{"owner": "urn:li:corpuser:tom"}]}


def test_mcp_schema_fields_parse_the_documented_envelope() -> None:
    """`list_schema_fields` returns an envelope, not a bare list."""

    def executor(name: str, args: dict) -> object:
        return {
            "urn": RAW_PAYMENTS,
            "totalFields": 1,
            "returned": 1,
            "fields": [
                {
                    "fieldPath": "amount_usd",
                    "type": "NUMBER",
                    "nativeDataType": "DECIMAL(10,2)",
                    "nullable": False,
                    "tags": {"tags": [{"tag": {"urn": "urn:li:tag:PII"}}]},
                }
            ],
        }

    source = McpLineageSource(executor)
    fields = source.list_schema_fields(RAW_PAYMENTS)

    assert len(fields) == 1
    assert fields[0].field_path == "amount_usd"
    assert fields[0].native_type == "DECIMAL(10,2)"
    assert fields[0].tags == ("urn:li:tag:PII",)


def test_mcp_missing_entity_is_recorded_not_fatal() -> None:
    """Per-entity errors come back inline; a deleted upstream is a real answer."""

    def executor(name: str, args: dict) -> object:
        return [{"urn": RAW_PAYMENTS, "error": "Entity not found"}]

    source = McpLineageSource(executor)
    node = source.get_entity(RAW_PAYMENTS)
    assert node is not None
    assert node.entity_type == "dataset"
    assert node.aspects.get("schemaMetadata") is None


def test_sdk_lineage_source_stub() -> None:
    sdk_source = SdkLineageSource()
    node = sdk_source.get_entity(RAW_PAYMENTS)
    assert node is not None
    assert node.entity_type == "dataset"
