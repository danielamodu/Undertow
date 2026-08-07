"""Tests for the schema differ.

The schema differ is one of two components that produce BLOCK verdicts, so the
coverage here is deliberately exhaustive on the type-compatibility lattice. A
wrong answer in either direction is expensive: a missed narrowing ships broken
data to a model, and a spurious one stops a deploy that was fine.

Everything is a plain object. No DataHub, no network, no fixtures on disk.
"""

from __future__ import annotations

import pytest

from undertow.differ.schema import diff_schema, is_compatible_change
from undertow.models import (
    AssetSnapshot,
    ColumnSnapshot,
    Confidence,
    FindingKind,
    UndertowSnapshot,
)

MODEL = "urn:li:mlModel:(urn:li:dataPlatform:mlflow,fraud_detector_v3,PROD)"
PAYMENTS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,raw.payments,PROD)"
TXNS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,raw.transactions,PROD)"
AVG_TXN = "urn:li:mlFeature:(txn_aggregates,avg_txn_30d)"
VELOCITY = "urn:li:mlFeature:(txn_aggregates,txn_velocity)"


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def col(
    path: str,
    data_type: str = "number",
    native: str | None = None,
    *,
    nullable: bool = False,
) -> ColumnSnapshot:
    return ColumnSnapshot(
        path=path, data_type=data_type, native_type=native, nullable=nullable
    )


def asset(
    *columns: ColumnSnapshot,
    urn: str = PAYMENTS,
    features: tuple[str, ...] = (AVG_TXN,),
    column_features: dict[str, tuple[str, ...]] | None = None,
) -> AssetSnapshot:
    return AssetSnapshot(
        urn=urn,
        columns=columns,
        feeds_features=features,
        column_features=column_features or {},
    )


def snap(*assets: AssetSnapshot) -> UndertowSnapshot:
    return UndertowSnapshot(model_urn=MODEL, assets={a.urn: a for a in assets})


# --------------------------------------------------------------------------
# Column presence
# --------------------------------------------------------------------------


def test_identical_schemas_produce_no_findings() -> None:
    before = snap(asset(col("amount_usd"), col("merchant_id", "string")))
    assert diff_schema(before, before) == []


def test_dropped_column_is_reported() -> None:
    before = snap(asset(col("amount_usd"), col("merchant_id", "string")))
    after = snap(asset(col("merchant_id", "string")))

    findings = diff_schema(before, after)

    assert len(findings) == 1
    assert findings[0].kind is FindingKind.COLUMN_DROPPED
    assert findings[0].subject_column == "amount_usd"
    assert findings[0].subject_urn == PAYMENTS


def test_dropped_column_is_certain_and_therefore_can_block() -> None:
    # The whole point of the schema differ: these are facts, not inferences.
    before = snap(asset(col("amount_usd")))
    after = snap(asset())

    assert diff_schema(before, after)[0].confidence is Confidence.CERTAIN


def test_dropped_column_names_the_feature_it_breaks() -> None:
    before = snap(asset(col("amount_usd")))
    after = snap(asset())

    finding = diff_schema(before, after)[0]

    assert finding.affected_feature_urn == AVG_TXN
    assert finding.evidence["on_feature_path"] is True
    assert "avg_txn_30d" in finding.summary


def test_column_level_lineage_narrows_the_affected_feature() -> None:
    # Two features hang off this dataset, but only one reads the dropped column.
    columns = {"amount_usd": (AVG_TXN,), "merchant_id": (VELOCITY,)}
    before = snap(
        asset(col("amount_usd"), col("merchant_id", "string"),
              features=(AVG_TXN, VELOCITY), column_features=columns)
    )
    after = snap(
        asset(col("merchant_id", "string"),
              features=(AVG_TXN, VELOCITY), column_features=columns)
    )

    finding = diff_schema(before, after)[0]

    assert finding.affected_feature_urn == AVG_TXN
    assert finding.evidence["features"] == AVG_TXN
    assert finding.evidence["column_level_lineage"] is True


def test_without_column_level_lineage_every_feature_is_assumed_affected() -> None:
    # Losing resolution must not make the gate go quiet. DataHub's DerivedFrom
    # edge is dataset-level, so this is the common case, not the edge case.
    before = snap(asset(col("amount_usd"), features=(AVG_TXN, VELOCITY)))
    after = snap(asset(features=(AVG_TXN, VELOCITY)))

    finding = diff_schema(before, after)[0]

    assert finding.evidence["features"] == f"{AVG_TXN}, {VELOCITY}"
    assert finding.evidence["column_level_lineage"] is False
    assert "2 live features" in finding.summary


def test_dropped_column_off_the_feature_path_is_still_reported() -> None:
    # Reported, but the evidence says so — the policy engine, not the differ,
    # decides what that is worth.
    before = snap(asset(col("amount_usd"), col("unused"),
                        column_features={"amount_usd": (AVG_TXN,)}))
    after = snap(asset(col("amount_usd"), column_features={"amount_usd": (AVG_TXN,)}))

    finding = diff_schema(before, after)[0]

    assert finding.subject_column == "unused"
    assert finding.evidence["on_feature_path"] is False
    assert finding.affected_feature_urn is None


def test_added_column_is_informational_not_a_drop() -> None:
    before = snap(asset(col("amount_usd")))
    after = snap(asset(col("amount_usd"), col("currency", "string")))

    findings = diff_schema(before, after)

    assert len(findings) == 1
    assert findings[0].kind is FindingKind.COLUMN_ADDED
    assert findings[0].subject_column == "currency"


def test_assets_present_in_only_one_snapshot_are_skipped() -> None:
    # A dataset leaving the footprint is a lineage change. Reporting all of its
    # columns as dropped would bury whatever the real finding was.
    before = snap(asset(col("amount_usd")), asset(col("txn_id"), urn=TXNS))
    after = snap(asset(col("amount_usd")))

    assert diff_schema(before, after) == []


def test_findings_are_ordered_deterministically() -> None:
    before = snap(
        asset(col("b"), col("a"), col("c")),
        asset(col("z"), col("y"), urn=TXNS),
    )
    after = snap(asset(col("c")), asset(urn=TXNS))

    columns = [(f.subject_urn, f.subject_column) for f in diff_schema(before, after)]

    assert columns == [
        (PAYMENTS, "a"),
        (PAYMENTS, "b"),
        (TXNS, "y"),
        (TXNS, "z"),
    ]


# --------------------------------------------------------------------------
# Type compatibility — the lattice
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old_type", "old_native", "new_type", "new_native"),
    [
        ("number", "int32", "number", "int64"),          # integer widening
        ("number", "smallint", "number", "bigint"),
        ("number", "float", "number", "double"),         # float widening
        ("number", "int32", "number", "double"),         # int -> float
        ("number", "decimal(10,2)", "number", "decimal(12,4)"),
        ("number", "decimal(10,2)", "number", "decimal(10,2)"),
        ("string", "varchar(64)", "string", "varchar(255)"),
        ("string", "varchar(64)", "string", "text"),     # bounded -> unbounded
        ("null", None, "number", "int32"),               # type finally inferred
        ("number", "int32", "number", None),             # native became unknown
        ("string", "varchar", "string", "text"),         # rename, not a narrowing
    ],
)
def test_widening_changes_are_compatible(
    old_type: str, old_native: str | None, new_type: str, new_native: str | None
) -> None:
    assert is_compatible_change(
        col("c", old_type, old_native), col("c", new_type, new_native)
    )


@pytest.mark.parametrize(
    ("old_type", "old_native", "new_type", "new_native"),
    [
        ("number", "int64", "number", "int32"),          # integer narrowing
        ("number", "bigint", "number", "smallint"),
        ("number", "double", "number", "float"),         # float narrowing
        ("number", "double", "number", "int64"),         # truncation
        ("number", "decimal(10,2)", "number", "decimal(8,2)"),
        ("number", "decimal(10,2)", "number", "decimal(10,4)"),  # integral digits lost
        ("number", "decimal(10,2)", "number", "decimal(12,1)"),  # scale lost
        ("string", "varchar(255)", "string", "varchar(64)"),
        ("string", "text", "string", "varchar(64)"),     # unbounded -> bounded
        ("string", "varchar(64)", "number", "int32"),    # the doc's own example
        ("number", "int32", "string", "varchar(64)"),    # arithmetic silently breaks
        ("date", None, "string", "varchar(32)"),
        ("boolean", None, "number", "int32"),
    ],
)
def test_narrowing_changes_are_incompatible(
    old_type: str, old_native: str | None, new_type: str, new_native: str | None
) -> None:
    assert not is_compatible_change(
        col("c", old_type, old_native), col("c", new_type, new_native)
    )


def test_logical_type_spelling_is_normalised() -> None:
    # DataHub's union members are `NumberType`; ingestion sometimes flattens to
    # `number`. They must not read as a type change.
    assert is_compatible_change(col("c", "NumberType"), col("c", "number"))


def test_incompatible_type_change_is_reported() -> None:
    before = snap(asset(col("amount_usd", "number", "decimal(10,2)")))
    after = snap(asset(col("amount_usd", "string", "varchar(32)")))

    findings = diff_schema(before, after)

    assert len(findings) == 1
    assert findings[0].kind is FindingKind.COLUMN_TYPE_CHANGED
    assert findings[0].evidence["old_native_type"] == "decimal(10,2)"
    assert findings[0].evidence["new_native_type"] == "varchar(32)"
    assert findings[0].evidence["compatible"] is False


def test_compatible_type_change_is_not_reported() -> None:
    # There is one COLUMN_TYPE_CHANGED kind and policy maps it to BLOCK, so a
    # safe widening must be silent or every warehouse migration stops a deploy.
    before = snap(asset(col("amount_usd", "number", "int32")))
    after = snap(asset(col("amount_usd", "number", "bigint")))

    assert diff_schema(before, after) == []


def test_unparseable_native_width_does_not_block() -> None:
    # `varchar(max)` has no comparable width. Reporting it would be a guess, and
    # a guess that stops deploys is worse than a miss.
    assert is_compatible_change(
        col("c", "string", "varchar(255)"), col("c", "string", "varchar(max)")
    )


# --------------------------------------------------------------------------
# Nullability
# --------------------------------------------------------------------------


def test_relaxed_nullability_is_reported() -> None:
    before = snap(asset(col("amount_usd", nullable=False)))
    after = snap(asset(col("amount_usd", nullable=True)))

    findings = diff_schema(before, after)

    assert len(findings) == 1
    assert findings[0].kind is FindingKind.COLUMN_NULLABILITY_RELAXED
    assert findings[0].evidence["old_nullable"] is False
    assert findings[0].evidence["new_nullable"] is True


def test_tightened_nullability_is_not_reported() -> None:
    before = snap(asset(col("amount_usd", nullable=True)))
    after = snap(asset(col("amount_usd", nullable=False)))

    assert diff_schema(before, after) == []


def test_type_change_and_nullability_change_are_separate_findings() -> None:
    before = snap(asset(col("amount_usd", "number", "bigint", nullable=False)))
    after = snap(asset(col("amount_usd", "number", "int32", nullable=True)))

    kinds = {f.kind for f in diff_schema(before, after)}

    assert kinds == {
        FindingKind.COLUMN_TYPE_CHANGED,
        FindingKind.COLUMN_NULLABILITY_RELAXED,
    }


# --------------------------------------------------------------------------
# Report readability
# --------------------------------------------------------------------------


def test_summaries_use_short_names_not_raw_urns() -> None:
    before = snap(asset(col("amount_usd")))
    after = snap(asset())

    summary = diff_schema(before, after)[0].summary

    assert "raw.payments" in summary
    assert "urn:li:dataset" not in summary
