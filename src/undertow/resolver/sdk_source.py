"""SDK LineageSource fallback implementation.

Provides DataHub lineage and metadata access through direct Python SDK / REST / GraphQL queries.
Acts as the resilience fallback path if MCP is unreachable or tool signatures shift.
"""

from __future__ import annotations

from typing import Any

from datahub.metadata.schema_classes import DatasetProfileClass

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
        # Why this is recorded rather than swallowed: a gate that cannot reach the
        # graph must fail closed. If construction quietly left `graph = None`, every
        # lineage call returns [], the footprint collapses to the model itself, and
        # the verdict comes back CLEAR — a green light produced by a blind check.
        self.connection_error: str | None = None
        self.graph: Any | None = graph
        if graph is None:
            try:
                from datahub.ingestion.graph.client import DataHubGraph, DataHubGraphConfig

                self.graph = DataHubGraph(
                    DataHubGraphConfig(server=gms_url, token=token, timeout_sec=10)
                )
            except Exception as exc:
                self.graph = None
                self.connection_error = f"{type(exc).__name__}: {exc}"

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
                    # `get_entity_semityped` returns an `AspectBag` — a TypedDict
                    # over known aspect names. `LineageNode.aspects` is deliberately
                    # open, because differs read aspects DataHub adds faster than
                    # this codebase tracks them, so it is widened here.
                    aspects: dict[str, Any] = dict(self.graph.get_entity_semityped(u) or {})
                except Exception:
                    aspects = {}
                results[u] = LineageNode(urn=u, entity_type=etype, aspects=aspects)
            return results

        for u in urns:
            results[u] = LineageNode(urn=u, entity_type=parse_entity_type(u))
        return results

    def get_latest_profile(self, urn: str) -> dict[str, Any] | None:
        """The most recent `datasetProfile`, read off the timeseries API.

        Timeseries aspects do not live on the entity snapshot. `get_entity_raw`
        and `get_entity_semityped` return versioned aspects only, so a profile
        emitted by an ingestion run is simply absent from everything the rest of
        the resolver looks at.

        That gap silently disabled the entire statistical differ against a real
        DataHub: every asset resolved with `profile=None`, every statistical
        comparison was skipped, and the verdict came back clean because nothing
        had been examined. The unit tests did not catch it because they hand the
        resolver synthetic aspects with `datasetProfile` already inlined — the
        one shape a live GMS never produces.

        Returns `None` when the asset has no profile, which is a normal state:
        profiling is opt-in per source. `profile_coverage` turns that into an
        explicit "could not assess" rather than a silent pass.
        """
        if self.graph is None:
            return None

        try:
            rows = self.graph.get_timeseries_values(
                entity_urn=urn,
                aspect_type=DatasetProfileClass,
                filter={},
                limit=1,
            )
        except Exception:
            # Profiles are an enrichment. Failing to read one must not fail a
            # gate — but it must not look like a clean profile either, and
            # returning None is what keeps coverage honest.
            return None

        if not rows:
            return None

        latest = rows[0]
        return latest.to_obj() if hasattr(latest, "to_obj") else None

    def get_lineage(
        self, urn: str, direction: str = "UPSTREAM", hops: int = 1
    ) -> list[LineageEdge]:
        """One hop of lineage, via the graph client's `scroll_lineage` endpoint.

        The previous implementation called `DataHubGraph.get_lineage`, which does
        not exist on this client — the call raised `AttributeError` on every
        invocation and a bare `except: pass` turned that into an empty list. The
        effect was that dataset-to-dataset lineage never resolved at all, so a
        multi-hop upstream chain looked identical to a clean one.

        `scroll_lineage` applies the entity registry's triplet filter, so only
        edges annotated as lineage come back — the same `isLineage` semantics the
        MCP server uses, which is what keeps the two sources agreeing.
        """
        if self.graph is None:
            raise RuntimeError(
                f"No DataHub graph client available to resolve lineage for {urn}. "
                f"{self.connection_error or 'Client was not initialised.'}"
            )

        from datahub.ingestion.graph.openapi import LineageDirection

        wanted = (
            LineageDirection.DOWNSTREAM
            if direction.upper() == "DOWNSTREAM"
            else LineageDirection.UPSTREAM
        )

        try:
            result = self.graph.scroll_lineage(urns=[urn], direction=wanted, count=100)
        except Exception as exc:
            # Raised, not swallowed: an empty edge list is read downstream as
            # "nothing upstream changed" and reported as CLEAR.
            raise RuntimeError(
                f"Lineage query failed for {urn}: {type(exc).__name__}: {exc}"
            ) from exc

        edges: list[LineageEdge] = []
        for rel in getattr(result, "relationships", None) or []:
            # Endpoints arrive pre-resolved into upstream/downstream, so the
            # neighbour is simply whichever end is not the anchor.
            neighbour = rel.upstream_urn if rel.downstream_urn == urn else rel.downstream_urn
            if not neighbour or neighbour == urn:
                continue
            edges.append(
                LineageEdge(
                    source_urn=urn,
                    target_urn=neighbour,
                    relationship=rel.relationship_type or "DownstreamOf",
                )
            )
        return edges

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
