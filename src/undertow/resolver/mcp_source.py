"""MCP LineageSource implementation.

Provides DataHub lineage and metadata access through the DataHub MCP server tools.
Used as the primary showcase surface when running in an MCP environment.
"""

from __future__ import annotations

from typing import Any, Callable

from undertow.resolver.base import (
    LineageEdge,
    LineageNode,
    LineageSource,
    SchemaFieldInfo,
    parse_entity_type,
)


class McpLineageSource(LineageSource):
    """LineageSource backed by DataHub MCP tool calls.

    Can wrap an MCP client or tool dispatcher function `call_tool(name, args)`.
    """

    def __init__(self, tool_executor: Callable[[str, dict[str, Any]], Any] | None = None) -> None:
        self._executor = tool_executor

    def get_entity(self, urn: str) -> LineageNode | None:
        res = self.get_entities([urn])
        return res.get(urn)

    def get_entities(self, urns: list[str]) -> dict[str, LineageNode]:
        if not urns:
            return {}
        if self._executor is None:
            return {
                u: LineageNode(urn=u, entity_type=parse_entity_type(u))
                for u in urns
            }

        raw = self._executor("get_entities", {"urns": urns})
        results: dict[str, LineageNode] = {}
        if isinstance(raw, dict):
            for u, data in raw.items():
                if isinstance(data, dict):
                    etype = data.get("entity_type") or parse_entity_type(u)
                    aspects = data.get("aspects", {})
                    results[u] = LineageNode(urn=u, entity_type=etype, aspects=aspects)
                elif isinstance(data, LineageNode):
                    results[u] = data
        return results

    def get_lineage(
        self, urn: str, direction: str = "UPSTREAM", hops: int = 1
    ) -> list[LineageEdge]:
        if self._executor is None:
            return []
        raw = self._executor("get_lineage", {"urn": urn, "direction": direction, "hops": hops})
        edges: list[LineageEdge] = []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, LineageEdge):
                    edges.append(item)
                elif isinstance(item, dict):
                    edges.append(
                        LineageEdge(
                            source_urn=item.get("source_urn", urn),
                            target_urn=item.get("target_urn", ""),
                            relationship=item.get("relationship", "DownstreamOf"),
                            via=item.get("via"),
                        )
                    )
        return edges

    def list_schema_fields(self, urn: str) -> list[SchemaFieldInfo]:
        if self._executor is None:
            return []
        raw = self._executor("list_schema_fields", {"urn": urn})
        fields: list[SchemaFieldInfo] = []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, SchemaFieldInfo):
                    fields.append(item)
                elif isinstance(item, dict):
                    fields.append(
                        SchemaFieldInfo(
                            field_path=item.get("field_path", ""),
                            data_type=item.get("data_type", "unknown"),
                            native_type=item.get("native_type"),
                            nullable=item.get("nullable", False),
                            tags=tuple(item.get("tags", ())),
                        )
                    )
        return fields

    def get_lineage_paths_between(
        self, source_urn: str, target_urn: str
    ) -> list[list[str]]:
        if self._executor is None:
            return []
        raw = self._executor(
            "get_lineage_paths_between",
            {"source_urn": source_urn, "target_urn": target_urn},
        )
        if isinstance(raw, list):
            return raw
        return []

    def search(self, query: str, entity_types: list[str] | None = None) -> list[str]:
        if self._executor is None:
            return []
        raw = self._executor("search", {"query": query, "entity_types": entity_types})
        if isinstance(raw, list):
            return raw
        return []


__all__ = ["McpLineageSource"]
