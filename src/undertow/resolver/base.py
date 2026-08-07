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
    "parse_entity_type",
]
