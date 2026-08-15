"""SDK LineageSource fallback implementation.

Provides DataHub lineage and metadata access through direct Python SDK / REST / GraphQL queries.
Acts as the resilience fallback path if MCP is unreachable or tool signatures shift.
"""

from __future__ import annotations

from typing import Any

import requests

from undertow.resolver.base import (
    LineageEdge,
    LineageNode,
    LineageSource,
    SchemaFieldInfo,
    fine_grained_upstreams,
    parse_entity_type,
)
from undertow.resolver.profiles import TimeseriesProfileReader


def _describe_connection_failure(exc: BaseException, gms_url: str) -> str | None:
    """One plain sentence for the three ways a GMS check actually fails.

    `None` means: this was not a connection-shaped failure, so the caller
    should fall back to its own message rather than let a real bug elsewhere
    get relabelled as "could not reach DataHub". Walks `__cause__` because the
    SDK wraps `requests` exceptions in its own error types, and it is the
    `requests` exception underneath that says which of these it was.
    """
    cursor: BaseException | None = exc
    while cursor is not None:
        if isinstance(cursor, requests.exceptions.ConnectionError):
            return f"could not connect to {gms_url} — is DataHub running?"
        if isinstance(cursor, requests.exceptions.Timeout):
            return f"{gms_url} did not respond in time — is DataHub still starting up?"
        if isinstance(cursor, requests.exceptions.HTTPError):
            status = getattr(cursor.response, "status_code", None)
            if status in (401, 403):
                return f"{gms_url} rejected the request ({status}) — check DATAHUB_GMS_TOKEN."
            return f"{gms_url} returned an error ({status or 'unknown status'})."
        cursor = cursor.__cause__
    return None


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
        self._profiles = TimeseriesProfileReader(gms_url=gms_url, token=token)
        if graph is None:
            try:
                from datahub.ingestion.graph.client import DataHubGraph, DataHubGraphConfig

                self.graph = DataHubGraph(
                    DataHubGraphConfig(
                        server=gms_url,
                        token=token,
                        timeout_sec=10,
                        # A refused connection is not a blip worth retrying —
                        # if GMS were merely slow this would be a Timeout, not a
                        # ConnectionError. Left at its default, the SDK's own
                        # retry-with-backoff turned "is anyone home" into a
                        # 50-second question on a connection that failed
                        # instantly the first time.
                        retry_max_times=0,
                    )
                )
                # `DataHubGraph(...)` never talks to the server — it only builds a
                # client. An unreachable GMS is discovered later, the first time
                # something calls it, and by then it is `scroll_lineage` doing the
                # discovering: through several minutes of the SDK's own retry and
                # backoff before it gives up. Measured against a GMS that was
                # simply not running: 148 seconds of silence, then a raw
                # `ConnectionError`. `test_connection()` is the same round trip
                # this constructor is really promising, done immediately and on a
                # timeout this class controls, so "DataHub is unreachable" surfaces
                # in about a second instead of two and a half minutes.
                self.graph.test_connection()
            except Exception as exc:
                self.graph = None
                # `str(exc)` on a requests `ConnectionError` is the SDK's internal
                # retry machinery narrating itself — WinError codes, adapter names,
                # the literal URL it gave up on. None of that is what "Undertow
                # fails closed" was supposed to look like on a terminal; a short,
                # human sentence covers what the reader needs. Construction can
                # only fail this way — anything that is not connection-shaped here
                # would itself be a bug worth seeing in full, so the raw message is
                # kept as the fallback rather than hidden behind a guess.
                self.connection_error = _describe_connection_failure(
                    exc, gms_url
                ) or f"{type(exc).__name__}: {exc}"

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

        Timeseries aspects do not live on the entity snapshot, so a profile
        emitted by an ingestion run is absent from everything else the resolver
        looks at. Shared with the MCP source — see `resolver/profiles.py` for
        why it is not a method on either of them.
        """
        return self._profiles.get_latest_profile(urn)

    def get_column_lineage(self, urn: str, column: str, hops: int = 1) -> list[LineageEdge]:
        """Column-level upstreams, read off this dataset's own `upstreamLineage`.

        No extra query shape and no new endpoint: `fineGrainedLineages` rides on
        an aspect `get_entities` already fetches, so this is a read of something
        the traversal has usually pulled a moment earlier — and behind
        `CachingLineageSource` it is free after the first hit.

        Without this the SDK path resolved no `column_features` at all, so a
        dropped column implicated every feature its table's descendants fed
        while the MCP path narrowed to the ones it actually reached. Same
        catalog, same fine-grained lineage sitting in it, different answer
        depending on which client you happened to run — the README promises
        table-level attribution when *DataHub* lacks column lineage, not when
        the client does.
        """
        node = self.get_entity(urn)
        if node is None:
            return []
        return [
            LineageEdge(source_urn=urn, target_urn=upstream, relationship="DownstreamOf")
            for upstream in fine_grained_upstreams(node.aspects, column)
        ]

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
