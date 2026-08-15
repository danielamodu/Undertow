"""Tests for the offline demo path, and the bug building it exposed.

`RecordedLineageSource` replays aspects as plain dicts, which is also what the
MCP server returns. That shape found a defect the class-shaped SDK path had been
hiding: `getattr(some_dict, "values", None)` returns `dict.values` — a bound
method, and truthy — so the baseline reader picked up the method and subscripted
it.

On the SDK path aspects are schema classes, so `getattr` found the real field and
the bug never fired. On the MCP path it was masked by the local-snapshot
fallback, which supplies a baseline before the failure can matter. Neither
covered it, and both would have started failing the moment the graph became the
only source of a baseline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from undertow.models import UndertowSnapshot
from undertow.resolver import RecordedLineageSource
from undertow.resolver.base import LineageNode, fine_grained_upstreams
from undertow.resolver.traversal import _extract_baseline_snapshot, _member

RECORDING = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "undertow"
    / "data"
    / "recorded-graph.json"
)
FRAUD = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detector_v3,PROD)"
RAW = "urn:li:dataset:(urn:li:dataPlatform:snowflake,transactions.raw,PROD)"


# --------------------------------------------------------------------------
# The dict-method trap
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["values", "items", "keys", "get", "copy", "update"])
def test_member_never_returns_a_dict_method(name: str) -> None:
    """Every one of these is a real dict attribute and a plausible field name."""
    assert _member({}, name) is None


def test_member_reads_mappings_and_objects_alike() -> None:
    class Aspect:
        values = ["from-attribute"]

    assert _member({"values": ["from-mapping"]}, "values") == ["from-mapping"]
    assert _member(Aspect(), "values") == ["from-attribute"]


def test_baseline_reads_from_dict_shaped_structured_properties() -> None:
    """The shape the MCP server and recordings produce."""
    snapshot = UndertowSnapshot(model_urn=FRAUD, assets={}, baseline_ref="v1")
    node = LineageNode(
        urn=FRAUD,
        entity_type="mlModel",
        aspects={
            "structuredProperties": {
                "properties": [
                    {
                        "propertyUrn": "urn:li:structuredProperty:undertow_baseline",
                        "values": [snapshot.model_dump_json()],
                    }
                ]
            }
        },
    )

    recovered = _extract_baseline_snapshot(node)

    assert recovered is not None
    assert recovered.model_urn == FRAUD


def test_baseline_reads_from_class_shaped_structured_properties() -> None:
    """The shape the SDK produces, which is what used to work."""
    snapshot = UndertowSnapshot(model_urn=FRAUD, assets={}, baseline_ref="v1")

    class Value:
        def __init__(self, value: str) -> None:
            self.value = value

    class Prop:
        propertyUrn = "urn:li:structuredProperty:undertow_baseline"

        def __init__(self, values: list[Value]) -> None:
            self.values = values

    class Aspect:
        def __init__(self, properties: list[Prop]) -> None:
            self.properties = properties

    node = LineageNode(
        urn=FRAUD,
        entity_type="mlModel",
        aspects={
            "structuredProperties": Aspect([Prop([Value(snapshot.model_dump_json())])])
        },
    )

    recovered = _extract_baseline_snapshot(node)

    assert recovered is not None
    assert recovered.model_urn == FRAUD


def test_an_unrelated_property_is_not_mistaken_for_a_baseline() -> None:
    node = LineageNode(
        urn=FRAUD,
        entity_type="mlModel",
        aspects={
            "structuredProperties": {
                "properties": [
                    {"propertyUrn": "urn:li:structuredProperty:probe_alpha", "values": ["x"]}
                ]
            }
        },
    )

    assert _extract_baseline_snapshot(node) is None


# --------------------------------------------------------------------------
# The recording itself
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def recording() -> dict:
    """The committed recording. Absent is a failure, not a skip.

    This previously skipped when the file was missing, and when the recording
    moved into the package these seven tests stopped running without saying so
    — the exact silent-degradation shape the rest of this suite exists to catch.
    `undertow demo` cannot work without this file, so its absence is a broken
    build.
    """
    assert RECORDING.exists(), (
        f"{RECORDING} is missing. `undertow demo` depends on it; regenerate with "
        "`python scripts/record_fixture.py` against a live DataHub."
    )
    with open(RECORDING, encoding="utf-8") as handle:
        return json.load(handle)


def test_the_recording_holds_both_states(recording: dict) -> None:
    """A verdict is a comparison; one state cannot produce one."""
    assert set(recording) >= {"before", "after", "models"}


def test_the_recording_says_where_it_came_from(recording: dict) -> None:
    """It is a transcript of a real instance, and has to say so."""
    assert "recorded" in recording["_comment"].lower()
    assert recording["gms_version"]


def test_the_dropped_column_is_present_before_and_gone_after(recording: dict) -> None:
    """The whole demo turns on this one difference."""
    before = RecordedLineageSource(recording, "before")
    after = RecordedLineageSource(recording, "after")

    before_cols = {f.field_path for f in before.list_schema_fields(RAW)}
    after_cols = {f.field_path for f in after.list_schema_fields(RAW)}

    assert "transaction_amount" in before_cols
    assert "transaction_amount" not in after_cols
    assert before_cols - after_cols == {"transaction_amount"}


def test_lineage_replays_in_both_directions(recording: dict) -> None:
    source = RecordedLineageSource(recording, "before")

    upstream = source.get_lineage(FRAUD, direction="UPSTREAM")
    downstream = source.get_lineage(RAW, direction="DOWNSTREAM")

    assert upstream
    assert downstream


def test_profiles_replay(recording: dict) -> None:
    """Recorded from the timeseries API, so statistics work offline too."""
    source = RecordedLineageSource(recording, "before")

    assert source.get_latest_profile(RAW) is not None


def test_an_unknown_state_is_rejected_loudly(recording: dict) -> None:
    with pytest.raises(ValueError, match="no 'sideways' state"):
        RecordedLineageSource(recording, "sideways")


# --------------------------------------------------------------------------
# The fineGrainedLineages reader
#
# Has to read plain dicts (recordings, MCP) and semityped classes (SDK) alike,
# because it is the one parser both shapes have to go through.
# --------------------------------------------------------------------------

_SF = "urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:snowflake,a.b,PROD),{})"


def _lineage_aspect(downstream: str, upstream: str) -> dict:
    return {
        "upstreamLineage": {
            "fineGrainedLineages": [
                {
                    "downstreams": [_SF.format(downstream)],
                    "upstreams": [_SF.format(upstream)],
                }
            ]
        }
    }


def test_fine_grained_reader_matches_the_requested_column() -> None:
    aspects = _lineage_aspect("amount", "amount_usd")

    assert fine_grained_upstreams(aspects, "amount") == [_SF.format("amount_usd")]
    assert fine_grained_upstreams(aspects, "customer_id") == []


def test_fine_grained_reader_handles_a_missing_aspect() -> None:
    """Absent aspect, no fine-grained lineage, and no upstream for this column
    are three different situations that mean the same thing to a caller."""
    assert fine_grained_upstreams({}, "amount") == []
    assert fine_grained_upstreams({"upstreamLineage": {}}, "amount") == []
    assert fine_grained_upstreams({"upstreamLineage": {"upstreams": []}}, "amount") == []


def test_fine_grained_reader_reads_objects_as_well_as_dicts() -> None:
    """The SDK hands back semityped classes, not dicts."""

    class Obj:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    aspects = Obj(
        upstreamLineage=Obj(
            fineGrainedLineages=[
                Obj(downstreams=[_SF.format("amount")], upstreams=[_SF.format("amount_usd")])
            ]
        )
    )

    assert fine_grained_upstreams(aspects, "amount") == [_SF.format("amount_usd")]


def test_fine_grained_reader_ignores_non_schema_field_urns() -> None:
    """`downstreams` can name a whole dataset when lineage is table-level."""
    aspects = {
        "upstreamLineage": {
            "fineGrainedLineages": [
                {"downstreams": [RAW], "upstreams": [_SF.format("amount_usd")]}
            ]
        }
    }

    assert fine_grained_upstreams(aspects, "amount") == []


# --------------------------------------------------------------------------
# Column-level attribution, against the real recording
#
# `fineGrainedLineages` was captured from the live instance all along — the
# seed derives it by running DataHub's own SQL parser over scripts/sql/ — and
# nothing read it. These assert against that captured data rather than a mock,
# which is what makes them evidence that column-level attribution works on a
# real catalog and not just on a fixture shaped to agree with it.
# --------------------------------------------------------------------------

STAGING = "urn:li:dataset:(urn:li:dataPlatform:snowflake,staging.transactions_clean,PROD)"
VELOCITY = "urn:li:mlFeature:(fraud_detection,transaction_velocity_7d)"


def test_the_recording_carries_column_level_lineage(recording: dict) -> None:
    source = RecordedLineageSource(recording, "before")

    edges = source.get_column_lineage(STAGING, "amount")

    assert [e.target_urn for e in edges] == [
        f"urn:li:schemaField:({RAW},transaction_amount)"
    ]


def test_a_column_with_no_fine_grained_entry_replays_as_empty(recording: dict) -> None:
    source = RecordedLineageSource(recording, "before")

    assert source.get_column_lineage(STAGING, "no_such_column") == []


def test_column_features_resolve_from_the_real_recording(recording: dict) -> None:
    """The end-to-end claim: a real graph, walked, attributed per column."""
    from undertow.resolver.traversal import resolve_footprint

    footprint = resolve_footprint(
        recording["models"]["fraud"], RecordedLineageSource(recording, "before"), max_hops=5
    )
    raw = footprint.snapshot.asset(RAW)

    assert raw is not None
    assert raw.column_features, "column-level lineage resolved nothing"
    assert raw.features_for("transaction_amount") == (VELOCITY,)


def test_a_column_nothing_reads_is_not_tarred_with_the_feature(recording: dict) -> None:
    """`merchant_id` exists on transactions.raw and feeds no downstream column.

    This is the whole point of the refinement. Asset-level attribution cannot
    tell it apart from `transaction_amount`, which reaches a live feature three
    hops away.
    """
    from undertow.resolver.traversal import resolve_footprint

    footprint = resolve_footprint(
        recording["models"]["fraud"], RecordedLineageSource(recording, "before"), max_hops=5
    )
    raw = footprint.snapshot.asset(RAW)

    assert raw is not None
    assert "merchant_id" in {c.path for c in raw.columns}
    assert raw.features_for("merchant_id") == ()


def test_the_dropped_column_finding_reports_column_level_lineage(recording: dict) -> None:
    """`evidence["column_level_lineage"]` was False on every run until now."""
    from undertow.differ import diff_snapshots
    from undertow.models import FindingKind
    from undertow.policy import Policy
    from undertow.resolver.traversal import resolve_footprint

    model = recording["models"]["fraud"]
    before = resolve_footprint(
        model, RecordedLineageSource(recording, "before"), max_hops=5
    ).snapshot
    after = resolve_footprint(
        model, RecordedLineageSource(recording, "after"), max_hops=5
    ).snapshot

    dropped = [
        f
        for f in diff_snapshots(before, after, Policy.default())
        if f.kind is FindingKind.COLUMN_DROPPED and f.subject_column == "transaction_amount"
    ]

    assert len(dropped) == 1
    assert dropped[0].evidence["column_level_lineage"] is True
    assert dropped[0].affected_feature_urn == VELOCITY
    assert "which feeds transaction_velocity_7d" in dropped[0].summary


def test_the_recorded_source_reports_no_connection_error(recording: dict) -> None:
    """`_assert_not_blind` reads this; a missing attribute would crash the demo."""
    assert RecordedLineageSource(recording, "before").connection_error is None
