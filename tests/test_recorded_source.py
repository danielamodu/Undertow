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
from undertow.resolver.base import LineageNode
from undertow.resolver.traversal import _extract_baseline_snapshot, _member

RECORDING = Path(__file__).resolve().parent.parent / "examples" / "recorded-graph.json"
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
    if not RECORDING.exists():
        pytest.skip("no recording committed")
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


def test_the_recorded_source_reports_no_connection_error(recording: dict) -> None:
    """`_assert_not_blind` reads this; a missing attribute would crash the demo."""
    assert RecordedLineageSource(recording, "before").connection_error is None
