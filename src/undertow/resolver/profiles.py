"""Reading dataset profiles, which no lineage API will hand you.

`datasetProfile` is a timeseries aspect. It is absent from entity snapshots, so
`get_entity_semityped` never returns it, and the OSS MCP server exposes no tool
for it either — its eight read-only tools cover entities, lineage, schema
fields, search and documents, and nothing statistical.

So statistics come from the timeseries REST endpoint regardless of which lineage
source is in use. That is the same shape as write-back, which goes through the
REST emitter because the MCP server's mutation tools are gated off in OSS: reads
come from MCP where MCP has them, and take the path that exists where it does
not.

Keeping this in one place is deliberate. It lived on `SdkLineageSource` first,
which quietly meant `--mcp` had no drift detection at all — the two paths
returned different verdicts on the same graph while the README claimed they
agreed.
"""

from __future__ import annotations

from typing import Any

from datahub.metadata.schema_classes import DatasetProfileClass


class TimeseriesProfileReader:
    """Fetches the most recent `datasetProfile` for an asset.

    Lazily builds its own graph client so constructing one costs nothing until a
    profile is actually wanted, and so a source that never needs statistics
    never opens a connection.
    """

    def __init__(self, *, gms_url: str, token: str | None = None, timeout_sec: int = 15) -> None:
        self.gms_url = gms_url
        self.token = token
        self.timeout_sec = timeout_sec
        self._graph: Any = None
        self._unavailable = False

    def _client(self) -> Any:
        if self._graph is not None or self._unavailable:
            return self._graph

        try:
            from datahub.ingestion.graph.client import DataHubGraph, DataHubGraphConfig

            self._graph = DataHubGraph(
                DataHubGraphConfig(
                    server=self.gms_url, token=self.token, timeout_sec=self.timeout_sec
                )
            )
        except Exception:
            # Statistics are an enrichment on top of the schema and governance
            # checks. Losing them must not fail a gate — but it must read as
            # "could not assess", which is what returning None produces.
            self._unavailable = True
        return self._graph

    def __call__(self, urn: str) -> dict[str, Any] | None:
        return self.get_latest_profile(urn)

    def get_latest_profile(self, urn: str) -> dict[str, Any] | None:
        """The latest profile as a plain dict, or `None` if there is not one.

        `None` is a normal answer: profiling is opt-in per source, so most real
        footprints are partly unprofiled. `profile_coverage` turns that into an
        explicit "cannot assess" rather than letting silence read as "no drift".
        """
        graph = self._client()
        if graph is None:
            return None

        try:
            rows = graph.get_timeseries_values(
                entity_urn=urn,
                aspect_type=DatasetProfileClass,
                filter={},
                limit=1,
            )
        except Exception:
            return None

        if not rows:
            return None

        latest = rows[0]
        return latest.to_obj() if hasattr(latest, "to_obj") else None


__all__ = ["TimeseriesProfileReader"]
