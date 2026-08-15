"""Tests for `CachingLineageSource`.

Two things are being defended here, and only one of them is about speed.

The obvious one: a sweep resolves many models against one source, and models on
a team's inventory share upstream layers, so the same entities get asked for
repeatedly.

The subtle one: the traversal probes for optional methods with `getattr` and
changes behaviour when they are absent. A wrapper that answered those probes on
behalf of a source that cannot actually serve them would make an SDK run claim
column-level lineage and then resolve nothing — a quieter, worse bug than the
latency this class exists to remove.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest

from undertow.resolver.base import LineageEdge, LineageNode, SchemaFieldInfo
from undertow.resolver.caching import CachingLineageSource

DATASET = "urn:li:dataset:(urn:li:dataPlatform:snowflake,raw.payments,PROD)"
OTHER = "urn:li:dataset:(urn:li:dataPlatform:snowflake,raw.transactions,PROD)"
ABSENT = "urn:li:dataset:(urn:li:dataPlatform:snowflake,gone.missing,PROD)"


class CountingSource:
    """Records every call so the tests can assert on what reached DataHub."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.connection_error: str | None = None
        self.nodes = {
            DATASET: LineageNode(urn=DATASET, entity_type="dataset", aspects={}),
            OTHER: LineageNode(urn=OTHER, entity_type="dataset", aspects={}),
        }

    def get_entity(self, urn: str) -> LineageNode | None:
        self.calls["get_entity"] += 1
        return self.nodes.get(urn)

    def get_entities(self, urns: list[str]) -> dict[str, LineageNode]:
        self.calls["get_entities"] += 1
        return {u: self.nodes[u] for u in urns if u in self.nodes}

    def get_lineage(
        self, urn: str, direction: str = "UPSTREAM", hops: int = 1
    ) -> list[LineageEdge]:
        self.calls["get_lineage"] += 1
        return [LineageEdge(source_urn=urn, target_urn=OTHER, relationship="DownstreamOf")]

    def list_schema_fields(self, urn: str) -> list[SchemaFieldInfo]:
        self.calls["list_schema_fields"] += 1
        return [SchemaFieldInfo(field_path="amount", data_type="number")]


class ColumnCapableSource(CountingSource):
    def get_latest_profile(self, urn: str) -> dict[str, Any] | None:
        self.calls["get_latest_profile"] += 1
        return {"rowCount": 10}

    def get_column_lineage(self, urn: str, column: str, hops: int = 1) -> list[LineageEdge]:
        self.calls["get_column_lineage"] += 1
        return []


# --------------------------------------------------------------------------
# Capability detection — the invariant that matters most
# --------------------------------------------------------------------------


def test_wrapping_does_not_invent_a_capability_the_source_lacks() -> None:
    """Only the MCP source resolves column lineage.

    If the wrapper answered this probe for an SDK source, the traversal would
    walk every column of every feature-feeding dataset, get nothing back, and
    report column-level attribution it never actually had.
    """
    cached = CachingLineageSource(CountingSource())

    assert getattr(cached, "get_column_lineage", None) is None
    assert getattr(cached, "get_latest_profile", None) is None
    assert not hasattr(cached, "get_column_lineage")


def test_wrapping_preserves_a_capability_the_source_has() -> None:
    inner = ColumnCapableSource()
    cached = CachingLineageSource(inner)

    fn = getattr(cached, "get_column_lineage", None)
    assert callable(fn)
    assert fn(DATASET, "amount") == []
    assert inner.calls["get_column_lineage"] == 1


def test_unknown_attributes_still_raise() -> None:
    cached = CachingLineageSource(CountingSource())
    with pytest.raises(AttributeError):
        _ = cached.no_such_method


# --------------------------------------------------------------------------
# Failing closed must survive the wrapper
# --------------------------------------------------------------------------


def test_connection_error_is_visible_through_the_wrapper() -> None:
    """`_fail_if_unreachable` reads this off the source by name.

    A wrapper that swallowed it would turn an unreachable DataHub into a silent
    empty walk, which diffs clean against any baseline — the precise failure the
    gate exists to refuse.
    """
    inner = CountingSource()
    inner.connection_error = "connection refused"
    cached = CachingLineageSource(inner)

    assert cached.connection_error == "connection refused"


# --------------------------------------------------------------------------
# Caching
# --------------------------------------------------------------------------


def test_repeated_reads_reach_the_source_once() -> None:
    inner = CountingSource()
    cached = CachingLineageSource(inner)

    for _ in range(5):
        cached.get_entity(DATASET)
        cached.list_schema_fields(DATASET)
        cached.get_lineage(DATASET)

    assert inner.calls["get_entity"] == 1
    assert inner.calls["list_schema_fields"] == 1
    assert inner.calls["get_lineage"] == 1
    assert cached.hits == 12  # 15 reads, 3 of them misses


def test_a_missing_entity_is_only_asked_for_once() -> None:
    """"Nothing resolves to this URN" is an answer, and re-asking a sweep's
    worth of times costs exactly as much as asking for one that exists."""
    inner = CountingSource()
    cached = CachingLineageSource(inner)

    assert cached.get_entity(ABSENT) is None
    assert cached.get_entity(ABSENT) is None

    assert inner.calls["get_entity"] == 1


def test_direction_spelling_does_not_split_the_cache() -> None:
    inner = CountingSource()
    cached = CachingLineageSource(inner)

    cached.get_lineage(DATASET, direction="UPSTREAM")
    cached.get_lineage(DATASET, direction="upstream")

    assert inner.calls["get_lineage"] == 1


def test_direction_and_hops_are_still_distinct_keys() -> None:
    """Normalising case must not collapse genuinely different questions."""
    inner = CountingSource()
    cached = CachingLineageSource(inner)

    cached.get_lineage(DATASET, direction="UPSTREAM", hops=1)
    cached.get_lineage(DATASET, direction="DOWNSTREAM", hops=1)
    cached.get_lineage(DATASET, direction="UPSTREAM", hops=3)

    assert inner.calls["get_lineage"] == 3


def test_batch_fetches_only_what_is_missing() -> None:
    inner = CountingSource()
    cached = CachingLineageSource(inner)

    cached.get_entity(DATASET)
    found = cached.get_entities([DATASET, OTHER])

    assert set(found) == {DATASET, OTHER}
    # One batch call, and it asked only for OTHER.
    assert inner.calls["get_entities"] == 1
    assert inner.calls["get_entity"] == 1

    # Fully warm now — no further calls at all.
    cached.get_entities([DATASET, OTHER])
    assert inner.calls["get_entities"] == 1


def test_a_batch_populates_the_single_entity_cache() -> None:
    inner = CountingSource()
    cached = CachingLineageSource(inner)

    cached.get_entities([DATASET, OTHER])
    cached.get_entity(DATASET)

    assert inner.calls["get_entity"] == 0


def test_optional_methods_are_cached_too() -> None:
    inner = ColumnCapableSource()
    cached = CachingLineageSource(inner)

    for _ in range(4):
        cached.get_latest_profile(DATASET)
        cached.get_column_lineage(DATASET, "amount")

    assert inner.calls["get_latest_profile"] == 1
    assert inner.calls["get_column_lineage"] == 1


def test_optional_method_cache_keys_on_arguments() -> None:
    inner = ColumnCapableSource()
    cached = CachingLineageSource(inner)

    cached.get_column_lineage(DATASET, "amount")
    cached.get_column_lineage(DATASET, "user_id")

    assert inner.calls["get_column_lineage"] == 2


# --------------------------------------------------------------------------
# The reason this exists: sweeping an inventory
# --------------------------------------------------------------------------


def test_a_second_resolution_over_a_shared_graph_costs_almost_nothing() -> None:
    """`check --all` gates each model independently against one source.

    Models on a team's inventory overwhelmingly share upstream layers, so the
    second walk should be answered from what the first one already fetched.
    """
    from undertow.resolver.traversal import resolve_footprint

    MODEL_A = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,a,PROD)"
    MODEL_B = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,b,PROD)"
    FEATURE = "urn:li:mlFeature:(shared,f1)"

    class SharedGraph(CountingSource):
        def __init__(self) -> None:
            super().__init__()
            self.edges = {
                MODEL_A: [
                    LineageEdge(
                        source_urn=MODEL_A, target_urn=FEATURE, relationship="Consumes"
                    )
                ],
                MODEL_B: [
                    LineageEdge(
                        source_urn=MODEL_B, target_urn=FEATURE, relationship="Consumes"
                    )
                ],
                FEATURE: [
                    LineageEdge(
                        source_urn=FEATURE, target_urn=DATASET, relationship="DerivedFrom"
                    )
                ],
                DATASET: [],
            }

        def get_lineage(
            self, urn: str, direction: str = "UPSTREAM", hops: int = 1
        ) -> list[LineageEdge]:
            self.calls["get_lineage"] += 1
            return self.edges.get(urn, [])

    inner = SharedGraph()
    cached = CachingLineageSource(inner)

    resolve_footprint(MODEL_A, cached, max_hops=5)
    after_first = dict(inner.calls)

    resolve_footprint(MODEL_B, cached, max_hops=5)
    second_walk_cost = inner.calls["get_lineage"] - after_first["get_lineage"]

    # Only MODEL_B itself is new; the feature and the dataset behind it were
    # already fetched while resolving MODEL_A.
    assert second_walk_cost == 1
    assert inner.calls["list_schema_fields"] == after_first["list_schema_fields"] + 1
