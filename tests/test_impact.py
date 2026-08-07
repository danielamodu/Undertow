"""Tests for the pre-merge SQL impact check.

`undertow check` runs after the table was rebuilt. This runs before the SQL
merges, which means it is reasoning about a table that does not exist in the
shape being proposed — the comparison is "columns this statement would produce"
against "columns the catalog holds today".

No DataHub: the lineage source is a dict, and the schema resolver is DataHub's
own, seeded in memory.
"""

from __future__ import annotations

import pytest

pytest.importorskip("sqlglot", reason="needs acryl-datahub[sql-parser]")

import datahub.emitter.mce_builder as builder  # noqa: E402
import datahub.metadata.schema_classes as models  # noqa: E402
from datahub.sql_parsing.schema_resolver import SchemaResolver  # noqa: E402

from undertow.impact import (  # noqa: E402
    analyse_sql,
    downstream_models,
    format_pr_comment,
)
from undertow.resolver.base import LineageEdge, LineageNode, SchemaFieldInfo  # noqa: E402

RAW = "urn:li:dataset:(urn:li:dataPlatform:snowflake,transactions.raw,PROD)"
STAGING = "urn:li:dataset:(urn:li:dataPlatform:snowflake,staging.transactions_clean,PROD)"
FEATURE = "urn:li:mlFeature:(fraud_detection,transaction_velocity_7d)"
CHURN_FEATURE = "urn:li:mlFeature:(customer_churn,customer_txn_volume)"
MODEL = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detector_v3,PROD)"
CHURN_MODEL = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,churn_predictor_v1,PROD)"

STAGING_COLUMNS = ["transaction_id", "customer_id", "amount", "event_date"]

DROPS_AMOUNT = """
CREATE OR REPLACE TABLE staging.transactions_clean AS
SELECT transaction_id, customer_id, CAST(timestamp AS DATE) AS event_date
FROM transactions.raw
"""

KEEPS_EVERYTHING = """
CREATE OR REPLACE TABLE staging.transactions_clean AS
SELECT
    transaction_id,
    customer_id,
    CAST(transaction_amount AS DECIMAL(10, 2)) AS amount,
    CAST(timestamp AS DATE) AS event_date
FROM transactions.raw
"""

ADDS_A_COLUMN = """
CREATE OR REPLACE TABLE staging.transactions_clean AS
SELECT
    transaction_id,
    customer_id,
    CAST(transaction_amount AS DECIMAL(10, 2)) AS amount,
    CAST(timestamp AS DATE) AS event_date,
    merchant_id
FROM transactions.raw
"""


class StubSource:
    """Downstream lineage and schemas, as a dict."""

    def __init__(self, *, downstream=None, columns=None, owners=None):
        self.downstream = downstream if downstream is not None else DEFAULT_DOWNSTREAM
        self.columns = columns if columns is not None else {STAGING: STAGING_COLUMNS}
        self.owners = owners or {
            MODEL: ("urn:li:corpuser:ml_eng_alex",),
            CHURN_MODEL: ("urn:li:corpuser:ml_eng_priya",),
        }

    def get_lineage(self, urn, direction="UPSTREAM", hops=1):
        if direction != "DOWNSTREAM":
            return []
        return [
            LineageEdge(source_urn=urn, target_urn=t, relationship="DownstreamOf")
            for t in self.downstream.get(urn, [])
        ]

    def list_schema_fields(self, urn):
        return [
            SchemaFieldInfo(field_path=c, data_type="unknown", native_type="VARCHAR")
            for c in self.columns.get(urn, [])
        ]

    def get_entity(self, urn):
        owners = self.owners.get(urn)
        if owners is None:
            return LineageNode(urn=urn, entity_type="mlModel", aspects={})
        ownership = models.OwnershipClass(
            owners=[
                models.OwnerClass(owner=o, type=models.OwnershipTypeClass.TECHNICAL_OWNER)
                for o in owners
            ]
        )
        return LineageNode(urn=urn, entity_type="mlModel", aspects={"ownership": ownership})

    def get_entities(self, urns):
        return {u: self.get_entity(u) for u in urns}


DEFAULT_DOWNSTREAM = {
    RAW: [STAGING],
    STAGING: [FEATURE, CHURN_FEATURE],
    FEATURE: [MODEL],
    CHURN_FEATURE: [CHURN_MODEL],
}


def resolver_with_raw() -> SchemaResolver:
    resolver = SchemaResolver(platform="snowflake", env="PROD")
    resolver.add_schema_metadata(
        RAW,
        models.SchemaMetadataClass(
            schemaName="transactions.raw",
            platform=builder.make_data_platform_urn("snowflake"),
            version=0,
            hash="",
            platformSchema=models.OtherSchemaClass(rawSchema=""),
            fields=[
                models.SchemaFieldClass(
                    fieldPath=path,
                    type=models.SchemaFieldDataTypeClass(type=models.StringTypeClass()),
                    nativeDataType=native,
                    nullable=False,
                )
                for path, native in [
                    ("transaction_id", "VARCHAR(64)"),
                    ("customer_id", "VARCHAR(64)"),
                    ("transaction_amount", "DECIMAL(10,2)"),
                    ("merchant_id", "VARCHAR(64)"),
                    ("timestamp", "TIMESTAMP_NTZ"),
                ]
            ],
        ),
    )
    return resolver


def analyse(tmp_path, sql: str, source: StubSource | None = None):
    path = tmp_path / "model.sql"
    path.write_text(sql, encoding="utf-8")
    return analyse_sql(
        path, source=source or StubSource(), schema_resolver=resolver_with_raw()
    )


# --------------------------------------------------------------------------
# Downstream traversal
# --------------------------------------------------------------------------


def test_walks_through_features_to_reach_models() -> None:
    found = downstream_models(STAGING, StubSource())

    assert [m.model_urn for m in found] == [CHURN_MODEL, MODEL]


def test_the_route_names_every_hop() -> None:
    found = downstream_models(STAGING, StubSource())
    fraud = next(m for m in found if m.model_urn == MODEL)

    assert fraud.route() == "transaction_velocity_7d → fraud_detector_v3"
    assert fraud.owners == ("urn:li:corpuser:ml_eng_alex",)


def test_a_cycle_does_not_hang() -> None:
    """Lineage graphs are not guaranteed acyclic, and a gate that hangs is a
    gate somebody removes from CI."""
    source = StubSource(downstream={STAGING: [FEATURE], FEATURE: [STAGING, MODEL]})

    found = downstream_models(STAGING, source)

    assert [m.model_urn for m in found] == [MODEL]


def test_depth_is_bounded() -> None:
    source = StubSource(downstream={STAGING: [FEATURE], FEATURE: [MODEL]})

    assert downstream_models(STAGING, source, max_hops=1) == []
    assert len(downstream_models(STAGING, source, max_hops=2)) == 1


def test_an_unreachable_node_does_not_lose_the_rest_of_the_walk() -> None:
    class Flaky(StubSource):
        def get_lineage(self, urn, direction="UPSTREAM", hops=1):
            if urn == CHURN_FEATURE:
                raise RuntimeError("timeout")
            return super().get_lineage(urn, direction, hops)

    found = downstream_models(STAGING, Flaky())

    assert [m.model_urn for m in found] == [MODEL]


# --------------------------------------------------------------------------
# The comparison
# --------------------------------------------------------------------------


def test_a_dropped_column_is_detected_before_the_table_is_rebuilt(tmp_path) -> None:
    impact = analyse(tmp_path, DROPS_AMOUNT)

    assert impact is not None
    assert impact.dropped_columns == ("amount",)
    assert impact.is_breaking
    assert {m.model_urn for m in impact.impacted} == {MODEL, CHURN_MODEL}


def test_an_unchanged_statement_reports_nothing(tmp_path) -> None:
    impact = analyse(tmp_path, KEEPS_EVERYTHING)

    assert impact is not None
    assert impact.dropped_columns == ()
    assert impact.is_breaking is False


def test_an_added_column_is_not_breaking(tmp_path) -> None:
    """Additions cannot break a downstream reader, so no traversal is spent."""
    impact = analyse(tmp_path, ADDS_A_COLUMN)

    assert impact is not None
    assert impact.added_columns == ("merchant_id",)
    assert impact.dropped_columns == ()
    assert impact.impacted == ()


def test_a_brand_new_table_has_nothing_downstream(tmp_path) -> None:
    """A table absent from the catalog is a new model, not a change to one."""
    impact = analyse(tmp_path, KEEPS_EVERYTHING, StubSource(columns={}))

    assert impact is not None
    assert impact.dropped_columns == ()
    assert impact.is_breaking is False


def test_a_bare_select_builds_nothing_and_is_skipped(tmp_path) -> None:
    impact = analyse(tmp_path, "SELECT transaction_id FROM transactions.raw")

    assert impact is None


def test_dropping_a_column_nothing_reads_is_not_breaking(tmp_path) -> None:
    impact = analyse(tmp_path, DROPS_AMOUNT, StubSource(downstream={}))

    assert impact is not None
    assert impact.dropped_columns == ("amount",)
    assert impact.impacted == ()
    assert impact.is_breaking is False


# --------------------------------------------------------------------------
# The comment
# --------------------------------------------------------------------------


def test_the_comment_names_the_models_and_their_owners(tmp_path) -> None:
    impact = analyse(tmp_path, DROPS_AMOUNT)

    comment = format_pr_comment([impact], project_url="https://example.com/undertow")

    assert "removes columns that production models depend on" in comment
    assert "`amount`" in comment
    assert "@ml_eng_alex" in comment
    assert "@ml_eng_priya" in comment
    assert "transaction_velocity_7d → fraud_detector_v3" in comment


def test_a_clean_comment_says_so(tmp_path) -> None:
    impact = analyse(tmp_path, KEEPS_EVERYTHING)

    comment = format_pr_comment([impact], project_url="https://example.com/undertow")

    assert "no columns removed" in comment.lower()


def test_paths_in_the_comment_are_posix(tmp_path) -> None:
    """A PR comment is read on the web, not in a Windows shell."""
    impact = analyse(tmp_path, DROPS_AMOUNT)

    assert "\\" not in impact.sql_file
