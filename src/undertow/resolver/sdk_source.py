"""SDK LineageSource fallback implementation.

Provides DataHub lineage and metadata access through direct Python SDK / REST / GraphQL queries.
Acts as the resilience fallback path if MCP is unreachable or tool signatures shift.
"""

from __future__ import annotations

from typing import Any

from undertow.resolver.base import (
    LineageEdge,
    LineageNode,
    LineageSource,
    SchemaFieldInfo,
    parse_entity_type,
)


class SdkLineageSource(LineageSource):
    """LineageSource backed by acryl-datahub Python SDK / REST GMS client.

    Can be initialised with a `gms_url` and optional `token`, or an existing Graph client.
    """

    def __init__(
        self,
        gms_url: str = "http://localhost:8080",
        token: str | None = None,
        graph: Any = None,
    ) -> None:
        self.gms_url = gms_url
        self.token = token
        if graph is None:
            try:
                from datahub.ingestion.graph.client import DataHubGraph, DataHubGraphConfig
                self.graph = DataHubGraph(DataHubGraphConfig(server=gms_url, token=token))
            except Exception:
                self.graph = None
        else:
            self.graph = graph

    def get_entity(self, urn: str) -> LineageNode | None:
        res = self.get_entities([urn])
        return res.get(urn)

    def get_entities(self, urns: list[str]) -> dict[str, LineageNode]:
        if not urns:
            return {}

        results: dict[str, LineageNode] = {}

        if self.graph is not None:
            for u in urns:
                etype = parse_entity_type(u)
                try:
                    aspects = self.graph.get_entity_semityped(u) or {}
                except Exception:
                    aspects = {}
                results[u] = LineageNode(urn=u, entity_type=etype, aspects=aspects)
            return results

        for u in urns:
            results[u] = LineageNode(urn=u, entity_type=parse_entity_type(u))
        return results

    def get_lineage(
        self, urn: str, direction: str = "UPSTREAM", hops: int = 1
    ) -> list[LineageEdge]:
        if self.graph is not None:
            try:
                edges_raw = self.graph.get_lineage(urn, direction=direction, depth=hops)
                edges: list[LineageEdge] = []
                for edge in edges_raw:
                    edges.append(
                        LineageEdge(
                            source_urn=getattr(edge, "urn", urn),
                            target_urn=getattr(edge, "target_urn", ""),
                            relationship=getattr(edge, "type", "DownstreamOf"),
                            via=getattr(edge, "via", None),
                        )
                    )
                return edges
            except Exception:
                pass
        return []

    def list_schema_fields(self, urn: str) -> list[SchemaFieldInfo]:
        node = self.get_entity(urn)
        if node is None or "schemaMetadata" not in node.aspects:
            return []

        fields: list[SchemaFieldInfo] = []
        schema_aspect = node.aspects["schemaMetadata"]
        raw_fields = getattr(schema_aspect, "fields", []) or schema_aspect.get("fields", [])
        for f in raw_fields:
            path = getattr(f, "fieldPath", None) or f.get("fieldPath", "")
            dtype = getattr(f, "dataType", None) or f.get("dataType", "unknown")
            native = getattr(f, "nativeDataType", None) or f.get("nativeDataType")
            nullable = getattr(f, "nullable", False) or f.get("nullable", False)
            fields.append(
                SchemaFieldInfo(
                    field_path=path,
                    data_type=str(dtype),
                    native_type=native,
                    nullable=nullable,
                )
            )
        return fields


__all__ = ["SdkLineageSource"]
