"""A lineage source that replays a recorded graph instead of calling DataHub.

Undertow's whole argument is that it reads a real catalog, so an offline mode
needs care not to undermine it. This is not a mock and not a hand-built fixture:
`scripts/record_fixture.py` captures every answer a live DataHub gave the
resolver — entities, aspects, edges, schemas, profiles — and this class hands
those same answers back.

What that does and does not prove:

  * It proves the differs, attribution, policy engine, reporter and exit codes
    behave as claimed on real catalog data. Everything above the resolver is the
    same code running the same inputs.
  * It does not prove the resolver can talk to DataHub. Only `--mcp` or the SDK
    path against a live instance shows that, which is what the tests and CI do.

Being explicit about the seam is the point. `undertow demo` says on every run
that it is replaying a recording, because a gate that overstates what it checked
is the exact failure this project exists to prevent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from undertow.resolver.base import (
    LineageEdge,
    LineageNode,
    LineageSource,
    SchemaFieldInfo,
    fine_grained_upstreams,
    parse_entity_type,
)


class RecordedLineageSource(LineageSource):
    """Replays one recorded state of the graph.

    A recording holds two states — `before` and `after` a change — because a
    verdict is a comparison and one state on its own cannot produce one.
    """

    def __init__(self, recording: dict[str, Any], state: str) -> None:
        if state not in recording:
            available = sorted(k for k in recording if not k.startswith("_"))
            raise ValueError(f"recording has no {state!r} state; found {available}")
        self._state: dict[str, Any] = recording[state]
        self.state_name = state
        # Present so `_assert_not_blind` treats this like any other source.
        self.connection_error: str | None = None

    @classmethod
    def load(cls, path: str | Path, state: str) -> RecordedLineageSource:
        with open(path, encoding="utf-8") as handle:
            return cls(json.load(handle), state)

    # -- entities ----------------------------------------------------------

    def get_entity(self, urn: str) -> LineageNode | None:
        entry = self._state["entities"].get(urn)
        if entry is None:
            return None
        return LineageNode(
            urn=urn,
            entity_type=entry.get("entity_type") or parse_entity_type(urn),
            aspects=entry.get("aspects") or {},
        )

    def get_entities(self, urns: list[str]) -> dict[str, LineageNode]:
        found = {}
        for urn in urns:
            node = self.get_entity(urn)
            if node is not None:
                found[urn] = node
        return found

    # -- graph -------------------------------------------------------------

    def get_lineage(
        self, urn: str, direction: str = "UPSTREAM", hops: int = 1
    ) -> list[LineageEdge]:
        wanted = "DOWNSTREAM" if direction.upper() == "DOWNSTREAM" else "UPSTREAM"
        edges = (self._state["lineage"].get(urn) or {}).get(wanted) or []
        return [
            LineageEdge(
                source_urn=e["source_urn"],
                target_urn=e["target_urn"],
                relationship=e.get("relationship") or "DownstreamOf",
                via=e.get("via"),
            )
            for e in edges
        ]

    def get_column_lineage(self, urn: str, column: str, hops: int = 1) -> list[LineageEdge]:
        """Column-level upstreams, read from the recorded `upstreamLineage` aspect.

        No new capture was needed for this. `fineGrainedLineages` was already in
        the recording — `scripts/seed_datahub.py` derives it by running
        DataHub's own SQL parser over `scripts/sql/`, and the recorder captures
        the whole aspect — it just had nothing reading it. The MCP server
        answers its column-lineage tool from the same aspect, so replaying it
        here is the same data by a shorter route, not an approximation of it.
        """
        entry = self._state["entities"].get(urn) or {}
        return [
            LineageEdge(source_urn=urn, target_urn=upstream, relationship="DownstreamOf")
            for upstream in fine_grained_upstreams(entry.get("aspects") or {}, column)
        ]

    def list_schema_fields(self, urn: str) -> list[SchemaFieldInfo]:
        return [
            SchemaFieldInfo(
                field_path=f["field_path"],
                data_type=f.get("data_type") or "unknown",
                native_type=f.get("native_type"),
                nullable=bool(f.get("nullable")),
                tags=tuple(f.get("tags") or ()),
            )
            for f in self._state["schemas"].get(urn) or []
        ]

    # -- statistics --------------------------------------------------------

    def get_latest_profile(self, urn: str) -> dict[str, Any] | None:
        """Recorded from the timeseries API, the same as the live sources read."""
        profile: dict[str, Any] | None = self._state.get("profiles", {}).get(urn)
        return profile


__all__ = ["RecordedLineageSource"]
