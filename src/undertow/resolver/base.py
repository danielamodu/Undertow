"""LineageSource protocol and base types for DataHub resolver implementations.

Defines the abstraction for interacting with DataHub's lineage graph and entity
metadata catalog. Undertow supports both an MCP server source (primary showcase)
and a direct SDK/GraphQL source (resilience fallback).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

_BASE = ConfigDict(frozen=True, protected_namespaces=())


def parse_entity_type(urn: str) -> str:
    """Extract entity type from a DataHub URN.

    Examples:
        - `urn:li:mlModel:(urn:li:dataPlatform:mlflow,fraud_detector,PROD)` -> `mlModel`
        - `urn:li:mlFeature:(txn_aggregates,avg_txn_30d)` -> `mlFeature`
        - `urn:li:mlFeatureTable:(urn:li:dataPlatform:feast,txn_aggregates)` -> `mlFeatureTable`
        - `urn:li:dataset:(urn:li:dataPlatform:snowflake,raw.payments,PROD)` -> `dataset`
    """
    if not urn.startswith("urn:li:"):
        return "unknown"
    parts = urn.split(":", 3)
    if len(parts) >= 3:
        return parts[2]
    return "unknown"


def _member(obj: Any, key: str) -> Any:
    """Read `key` off a mapping or an object, without ever returning a method.

    Aspects arrive as plain dicts from a recording and as semityped schema
    classes from the SDK, and this has to read both. Deliberately local rather
    than imported from `traversal`, which imports this module.

    The callable guard mirrors `traversal._member`. It is not load-bearing on
    the lookups here — the dict branch uses `.get`, so the historical
    `getattr(some_dict, "values")` trap cannot fire — but two helpers with the
    same name in the same package must not differ in behaviour, or the next
    reader will reasonably assume the guarantee holds and it will not.
    """
    if isinstance(obj, dict):
        return obj.get(key)
    value = getattr(obj, key, None)
    return None if callable(value) else value


def fine_grained_upstreams(aspects: Any, column: str) -> list[str]:
    """Upstream `schemaField` URNs feeding `column`, read off `upstreamLineage`.

    `fineGrainedLineages` is where DataHub actually stores column-level lineage:
    each entry names one or more downstream fields and the upstream fields they
    are derived from. It rides on the *downstream* dataset's `upstreamLineage`
    aspect, so this is called with the aspects of the table being read from and
    returns the columns that produce the one asked about.

    Returning `[]` covers three different situations that a caller cannot and
    should not tell apart: the aspect is absent, the platform emitted no
    fine-grained lineage, or this particular column genuinely has no upstream.
    All three mean the same thing to the resolver — no column-level answer is
    available — and it falls back to asset-level attribution for each.
    """
    lineage = _member(aspects, "upstreamLineage")
    if lineage is None:
        return []

    upstreams: list[str] = []
    for entry in _member(lineage, "fineGrainedLineages") or []:
        downstreams = _member(entry, "downstreams") or []
        if not any(_schema_field_column(d) == column for d in downstreams):
            continue
        for up in _member(entry, "upstreams") or []:
            # Only schemaField URNs. `FineGrainedLineage.upstreamType` is one of
            # FIELD_SET, DATASET or NONE, so an entry can legitimately carry
            # whole-dataset upstreams — and this function promises columns.
            # Returning a table URN from it would have every caller either
            # re-filter or quietly emit a "column lineage" edge pointing at a
            # table.
            if split_schema_field(up) is not None and up not in upstreams:
                upstreams.append(up)
    return upstreams


def split_schema_field(urn: Any) -> tuple[str, str] | None:
    """`urn:li:schemaField:(<dataset urn>,amount)` -> `(<dataset urn>, "amount")`.

    None for anything that is not a well-formed schemaField URN. Strict about
    the closing parenthesis on purpose: a truncated or non-canonical URN that
    merely starts with the right prefix is not one, and treating it as one turns
    a malformed input into a confidently wrong column name.
    """
    if not isinstance(urn, str):
        return None
    if not urn.startswith("urn:li:schemaField:(") or not urn.endswith(")"):
        return None
    inner = urn[urn.index("(") + 1 : urn.rindex(")")]
    # The dataset URN carries its own commas; the column is after the last one.
    parent, _, column = inner.rpartition(",")
    if not parent or not column:
        return None
    return parent, column


def _schema_field_column(urn: Any) -> str | None:
    """The column name inside a `schemaField` URN, or None if it is not one."""
    parsed = split_schema_field(urn)
    return parsed[1] if parsed else None


class LineageNode(BaseModel):
    """An entity fetched from DataHub with raw or parsed aspect metadata."""

    model_config = _BASE

    urn: str
    entity_type: str
    aspects: dict[str, Any] = {}


class LineageEdge(BaseModel):
    """A directed edge in the lineage graph."""

    model_config = _BASE

    source_urn: str
    target_urn: str
    relationship: str  # e.g., "Consumes", "DerivedFrom", "DownstreamOf"
    via: str | None = None


class SchemaFieldInfo(BaseModel):
    """Simplified column schema info."""

    model_config = _BASE

    field_path: str
    data_type: str
    native_type: str | None = None
    nullable: bool = False
    tags: tuple[str, ...] = ()


@runtime_checkable
class LineageSource(Protocol):
    """Protocol for fetching metadata and lineage graph from DataHub."""

    def get_entity(self, urn: str) -> LineageNode | None:
        """Fetch entity metadata and aspects for a single URN."""
        ...

    def get_entities(self, urns: list[str]) -> dict[str, LineageNode]:
        """Batch-fetch entity metadata and aspects for multiple URNs."""
        ...

    def get_lineage(
        self, urn: str, direction: str = "UPSTREAM", hops: int = 1
    ) -> list[LineageEdge]:
        """Get lineage edges connected to `urn`."""
        ...

    def list_schema_fields(self, urn: str) -> list[SchemaFieldInfo]:
        """Enumerate column schema fields for an asset."""
        ...


__all__ = [
    "LineageNode",
    "LineageEdge",
    "SchemaFieldInfo",
    "LineageSource",
    "fine_grained_upstreams",
    "parse_entity_type",
    "split_schema_field",
]
