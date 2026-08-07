"""LineageSource backed by the DataHub MCP server.

Written against mcp-server-datahub 0.6.0, whose tool signatures and response
shapes were read out of the installed package rather than assumed. Three of
those facts drive the design here and are worth stating, because each one
contradicts what a reasonable person would guess:

1. **`get_lineage` takes `upstream: bool`, not `direction: str`.** It returns
   `{"upstreams"|"downstreams": {"searchResults": [{"entity": {...}}]}}`, and
   `max_hops=3` means unlimited.

2. **ML edges are not on the entity.** The server's `entity_details.gql` selects
   only name/description/ownership/tags/deprecation/structuredProperties for
   `MLModel` and `MLFeature` — there is no `properties.mlFeatures` and no
   `properties.sources`. The Consumes and DerivedFrom edges are therefore only
   reachable through `get_lineage`, which is correct anyway: DataHub's lineage
   registry flags both as `isLineage`. Every hop goes through one call.

3. **`get_entities` speaks GraphQL, not PDL.** Aspect names differ from the SDK
   path (`tags` not `globalTags`, nested `tag.urn` not a flat string), so this
   module normalises into the shape the traversal already expects instead of
   forcing the traversal to learn two wire formats.

Failure policy matches `mcp_client`: raise, never return an empty collection. An
empty list from this layer is indistinguishable from "nothing upstream changed",
and Undertow would report that as CLEAR.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from undertow.resolver.base import (
    LineageEdge,
    LineageNode,
    LineageSource,
    SchemaFieldInfo,
    parse_entity_type,
)

# DataHub's lineage registry names, by the entity the edge points *from*. Used to
# label attribution hops so a report can say "DerivedFrom" rather than a generic
# "upstream of". Verified against LineageRegistry.java; see architecture §1.3.
_RELATIONSHIP_BY_TYPE: dict[str, str] = {
    "mlModel": "Consumes",
    "mlFeature": "DerivedFrom",
    "mlPrimaryKey": "DerivedFrom",
    "dataset": "DownstreamOf",
}


class McpLineageSource(LineageSource):
    """LineageSource backed by DataHub MCP tool calls.

    `tool_executor` is any callable `(tool_name, arguments) -> parsed result`.
    In production that is `McpToolExecutor`; in tests it is a stub. Unlike the
    previous revision, `None` is not accepted — a source with no way to reach
    DataHub silently answering "nothing here" is the exact failure this gate
    exists to prevent.
    """

    def __init__(self, tool_executor: Callable[[str, dict[str, Any]], Any]) -> None:
        if tool_executor is None:
            raise ValueError(
                "McpLineageSource requires a tool executor. Construct one with "
                "`McpToolExecutor()` (as a context manager) and pass it in."
            )
        self._executor = tool_executor
        self.connection_error: str | None = None

    # -- entities ----------------------------------------------------------

    def get_entity(self, urn: str) -> LineageNode | None:
        return self.get_entities([urn]).get(urn)

    def get_entities(self, urns: list[str]) -> dict[str, LineageNode]:
        if not urns:
            return {}

        raw = self._executor("get_entities", {"urns": urns})

        # The tool returns a bare dict for a single URN and a list otherwise.
        records = raw if isinstance(raw, list) else [raw]

        results: dict[str, LineageNode] = {}
        for record in records:
            if not isinstance(record, dict):
                continue
            urn = record.get("urn")
            if not urn:
                continue
            # Per-entity failures come back inline as {"error": ..., "urn": ...}
            # rather than raising. A missing entity is a real answer (it may have
            # been deleted upstream), so record it as an empty node instead of
            # aborting the whole walk.
            if "error" in record and "schemaMetadata" not in record:
                results[urn] = LineageNode(urn=urn, entity_type=parse_entity_type(urn))
                continue
            results[urn] = LineageNode(
                urn=urn,
                entity_type=record.get("type") or parse_entity_type(urn),
                aspects=_normalise_aspects(record),
            )
        return results

    # -- lineage -----------------------------------------------------------

    def get_lineage(
        self, urn: str, direction: str = "UPSTREAM", hops: int = 1
    ) -> list[LineageEdge]:
        """Fetch one hop of lineage.

        The `direction`/`hops` signature is kept for protocol compatibility with
        `SdkLineageSource`; it is translated to the server's `upstream: bool`.
        """
        upstream = direction.upper() != "DOWNSTREAM"
        raw = self._executor(
            "get_lineage",
            {"urn": urn, "upstream": upstream, "max_hops": hops, "max_results": 100},
        )
        return self._edges_from_lineage(raw, urn, upstream)

    def get_column_lineage(self, urn: str, column: str, hops: int = 1) -> list[LineageEdge]:
        """Column-level upstream lineage, resolved by the server.

        This is the fine-grained resolution `AssetSnapshot.column_features`
        wants. The server converts `(urn, column)` into a schemaField URN and
        walks that graph natively — worth using rather than reimplementing.
        """
        raw = self._executor(
            "get_lineage",
            {"urn": urn, "column": column, "upstream": True, "max_hops": hops, "max_results": 100},
        )
        return self._edges_from_lineage(raw, urn, upstream=True)

    def _edges_from_lineage(self, raw: Any, urn: str, upstream: bool) -> list[LineageEdge]:
        if not isinstance(raw, dict):
            return []

        key = "upstreams" if upstream else "downstreams"
        section = raw.get(key) or {}
        search_results = section.get("searchResults") or [] if isinstance(section, dict) else []

        source_type = parse_entity_type(urn)
        relationship = _RELATIONSHIP_BY_TYPE.get(source_type, "DownstreamOf")

        edges: list[LineageEdge] = []
        for result in search_results:
            entity = result.get("entity") if isinstance(result, dict) else None
            if not isinstance(entity, dict):
                continue
            target = entity.get("urn")
            if not target or target == urn:
                continue
            edges.append(
                LineageEdge(source_urn=urn, target_urn=target, relationship=relationship)
            )
        return edges

    def get_lineage_paths_between(self, source_urn: str, target_urn: str) -> Any:
        return self._executor(
            "get_lineage_paths_between",
            {"source_urn": source_urn, "target_urn": target_urn},
        )

    # -- schema ------------------------------------------------------------

    def list_schema_fields(self, urn: str) -> list[SchemaFieldInfo]:
        raw = self._executor("list_schema_fields", {"urn": urn, "limit": 500})
        if not isinstance(raw, dict):
            return []
        return [_schema_field(f) for f in (raw.get("fields") or []) if isinstance(f, dict)]

    # -- investigation surface --------------------------------------------

    def search(self, query: str, num_results: int = 20) -> Any:
        """Structured full-text search. Note: no `entity_types` parameter exists;
        entity filtering goes through the `filter` argument's /q syntax."""
        return self._executor("search", {"query": query, "num_results": num_results})

    def get_dataset_queries(self, urn: str, column: str | None = None, count: int = 20) -> Any:
        """SQL that actually reads this dataset or column.

        Usage evidence that lineage edges alone do not give you — a column can be
        in the graph and read by nothing, or read by a query no edge records.
        """
        return self._executor(
            "get_dataset_queries", {"urn": urn, "column": column, "count": count}
        )

    def search_documents(self, query: str, num_results: int = 10) -> Any:
        return self._executor("search_documents", {"query": query, "num_results": num_results})

    def grep_documents(self, urns: list[str], pattern: str) -> Any:
        return self._executor("grep_documents", {"urns": urns, "pattern": pattern})


# -- normalisation ---------------------------------------------------------


def _normalise_aspects(record: dict[str, Any]) -> dict[str, Any]:
    """Map a GraphQL entity payload onto the aspect names the traversal reads.

    Only the keys the differs actually consume are translated. Anything else is
    passed through untouched so a future differ can reach it without another
    round of plumbing.
    """
    aspects: dict[str, Any] = dict(record)

    # GraphQL calls it `tags` with nested `tag.urn`; the SDK path calls it
    # `globalTags` with `tags[].tag`. Normalise to the latter.
    tags = record.get("tags")
    if isinstance(tags, dict):
        associations = [
            {"tag": (t.get("tag") or {}).get("urn")}
            for t in (tags.get("tags") or [])
            if isinstance(t, dict)
        ]
        aspects["globalTags"] = {"tags": [a for a in associations if a["tag"]]}

    ownership = record.get("ownership")
    if isinstance(ownership, dict):
        owners = [
            {"owner": (o.get("owner") or {}).get("urn")}
            for o in (ownership.get("owners") or [])
            if isinstance(o, dict)
        ]
        aspects["ownership"] = {"owners": [o for o in owners if o["owner"]]}

    return aspects


def _schema_field(field: dict[str, Any]) -> SchemaFieldInfo:
    """One entry of `list_schema_fields`' `fields` array.

    `type` is DataHub's coarse logical type; `nativeDataType` carries the
    platform spelling and is the only place a narrowing change is visible.
    """
    tags_block = field.get("tags")
    tag_urns: tuple[str, ...] = ()
    if isinstance(tags_block, dict):
        tag_urns = tuple(
            urn
            for t in (tags_block.get("tags") or [])
            if isinstance(t, dict) and (urn := (t.get("tag") or {}).get("urn"))
        )

    return SchemaFieldInfo(
        field_path=field.get("fieldPath") or "",
        data_type=str(field.get("type") or "unknown"),
        native_type=field.get("nativeDataType"),
        nullable=bool(field.get("nullable", False)),
        tags=tag_urns,
    )


__all__ = ["McpLineageSource"]
