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
    """Read `key` off a mapping or an object.

    Aspects arrive as plain dicts from a recording and as semityped schema
    classes from the SDK, and this has to read both. Deliberately local rather
    than imported from `traversal`, which imports this module.
    """
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


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
            if isinstance(up, str) and up not in upstreams:
                upstreams.append(up)
    return upstreams


def _schema_field_column(urn: Any) -> str | None:
    """The column name inside a `schemaField` URN, or None if it is not one."""
    if not isinstance(urn, str) or not urn.startswith("urn:li:schemaField:("):
        return None
    inner = urn[urn.index("(") + 1 : urn.rindex(")")] if urn.endswith(")") else urn
    # The dataset URN carries its own commas; the column is after the last one.
    _, _, column = inner.rpartition(",")
    return column or None


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
]
