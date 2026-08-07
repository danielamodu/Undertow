"""Tests that the fixture's lineage is parsed from SQL, not asserted by hand.

The claim these defend is a credibility claim, not a correctness one. A demo
whose column-level lineage was typed out by the person writing the demo proves
only that Undertow can read aspects written to make it look good. These assert
that `staging.transactions_clean`'s schema and its
`transaction_amount -> amount` edge both fall out of running DataHub's own SQL
parser over `scripts/sql/`, so the path Undertow attributes a failure along is
the path the SQL actually creates.

No DataHub instance and no network: the parser runs locally against schemas held
in memory.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

pytest.importorskip("sqlglot", reason="needs acryl-datahub[sql-parser]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from seed_datahub import (  # noqa: E402
    DS_STAGING_URN,
    DS_TXN_URN,
    SQL_DIR,
    parse_transform,
    source_schemas,
    to_column_snapshots,
)

STAGING_SQL = "staging_transactions_clean.sql"


@pytest.fixture(scope="module")
def parsed():
    return parse_transform(STAGING_SQL, source_schemas())


def column_map(result) -> dict[str, list[tuple[str, str]]]:
    """downstream column -> [(upstream table, upstream column), ...]"""
    return {
        cl.downstream.column: [(u.table, u.column) for u in cl.upstreams]
        for cl in (result.column_lineage or [])
    }


def test_the_sql_file_is_the_only_place_the_transform_is_written_down() -> None:
    """If the SQL disappears there is no fallback description to fall back on."""
    sql_path = SQL_DIR / STAGING_SQL

    assert sql_path.exists()
    assert "FROM transactions.raw" in sql_path.read_text(encoding="utf-8")


def test_lineage_resolves_to_the_urns_the_demo_uses(parsed) -> None:
    _, result = parsed

    assert result.in_tables == [DS_TXN_URN]
    assert result.out_tables == [DS_STAGING_URN]


def test_the_staging_schema_comes_out_of_the_select_list(parsed) -> None:
    """The four staging columns are not declared anywhere — they are parsed."""
    _, result = parsed

    assert list(column_map(result)) == [
        "transaction_id",
        "customer_id",
        "amount",
        "event_date",
    ]


def test_the_load_bearing_column_edge_is_parsed_not_asserted(parsed) -> None:
    """`transaction_amount -> amount` is the edge the whole demo turns on.

    `make break` drops `transaction_amount` from the source table. Undertow can
    only name the affected feature because this edge exists, and it exists here
    because `CAST(transaction_amount AS DECIMAL(10, 2)) AS amount` was parsed.
    """
    _, result = parsed

    assert column_map(result)["amount"] == [(DS_TXN_URN, "transaction_amount")]


def test_renames_and_casts_are_distinguished_from_straight_copies(parsed) -> None:
    """The parser decides which is which; the seed script does not label them."""
    _, result = parsed
    by_column = {cl.downstream.column: cl for cl in result.column_lineage or []}

    assert by_column["customer_id"].logic.is_direct_copy is True
    assert by_column["amount"].logic.is_direct_copy is False
    assert "CAST" in by_column["amount"].logic.column_logic.upper()


def test_the_parse_is_confident_enough_to_publish(parsed) -> None:
    """Two-pass parsing exists to earn this number.

    A single pass cannot see the table the statement creates and reports 0.35.
    Publishing column-level lineage the parser is unsure of would undercut the
    attribution built on top of it.
    """
    _, result = parsed

    assert result.debug_info.error is None
    assert result.debug_info.confidence >= 0.9


def test_baseline_columns_are_derived_from_the_same_parse(parsed) -> None:
    """The seeded baseline must not restate what the SQL already determines.

    If these drift, `make seed` followed by `make check` reports a schema change
    on an untouched graph — a false BLOCK produced entirely by bookkeeping.
    """
    _, result = parsed
    from seed_datahub import infer_output_schema

    columns = to_column_snapshots(infer_output_schema(result) or [])

    assert [c.path for c in columns] == list(column_map(result))
    assert next(c for c in columns if c.path == "amount").native_type == "DECIMAL(10, 2)"
