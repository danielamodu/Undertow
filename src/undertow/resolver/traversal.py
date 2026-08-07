"""Graph traversal engine for resolving an mlModel's dependency footprint.

Traverses DataHub's lineage graph breadth-first using the two-hop ML path:
    mlModel --Consumes--> mlFeature --DerivedFrom--> dataset --upstream--> ...

Enforces cycle guarding, memoisation, and a maximum hop cap.
Assembles AssetSnapshots and AttributionPaths for each resolved asset.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from undertow.models import (
    AssetSnapshot,
    AttributionHop,
    AttributionPath,
    ColumnSnapshot,
    DependencyFootprint,
    FieldProfileSnapshot,
    ProfileSnapshot,
    UndertowSnapshot,
)
from undertow.resolver.base import LineageNode, LineageSource, parse_entity_type


def resolve_footprint(
    model_urn: str,
    source: LineageSource,
    *,
    max_hops: int = 5,
) -> DependencyFootprint:
    """Walk the graph upstream from `model_urn` and build a DependencyFootprint.

    BFS traversal logic:
    1. `mlModel` consumes `mlFeature`s via `mlModelProperties.mlFeatures` (Consumes).
    2. `mlFeature` derives from `dataset`s via `mlFeatureProperties.sources` (DerivedFrom).
    3. `dataset` depends on upstream `dataset`s via lineage (DownstreamOf).

    `mlFeatureTable` is excluded from the lineage graph path per architecture spec §1.3.
    """
    memo: dict[str, LineageNode] = {}
    visited: set[str] = set()
    assets: dict[str, AssetSnapshot] = {}

    path_hops: dict[str, tuple[AttributionHop, ...]] = {}
    queue: deque[tuple[str, int, list[tuple[str, str, str | None]]]] = deque()

    root_node = _fetch_entity(model_urn, source, memo)
    if root_node is None:
        root_node = LineageNode(urn=model_urn, entity_type="mlModel")
    visited.add(model_urn)

    root_hop = (model_urn, "mlModel", None)
    queue.append((model_urn, 0, [root_hop]))

    dataset_features: dict[str, set[str]] = {}

    while queue:
        curr_urn, depth, chain = queue.popleft()
        node = _fetch_entity(curr_urn, source, memo)
        entity_type = parse_entity_type(curr_urn)

        snapshot = _build_asset_snapshot(node, curr_urn, entity_type, source)
        assets[curr_urn] = snapshot

        attribution_hops = _build_attribution_hops(chain)
        path_hops[curr_urn] = attribution_hops

        if depth >= max_hops:
            continue

        upstreams = _find_upstreams(node, curr_urn, entity_type, source)

        for next_urn, relationship in upstreams:
            next_type = parse_entity_type(next_urn)
            if next_type == "mlFeatureTable":
                continue

            if entity_type == "mlFeature" and next_type == "dataset":
                dataset_features.setdefault(next_urn, set()).add(curr_urn)

            if next_urn not in visited:
                visited.add(next_urn)
                next_hop = (next_urn, next_type, relationship)
                queue.append((next_urn, depth + 1, chain + [next_hop]))

    for ds_urn, feat_set in dataset_features.items():
        if ds_urn in assets:
            snap = assets[ds_urn]
            updated_snap = snap.model_copy(
                update={"feeds_features": tuple(sorted(feat_set))}
            )
            assets[ds_urn] = updated_snap

    final_paths: dict[str, AttributionPath] = {}
    for urn, hops_tuple in path_hops.items():
        owners = assets[urn].owners if urn in assets else ()
        final_paths[urn] = AttributionPath(hops=hops_tuple, owners=owners)

    baseline_snap = _extract_baseline_snapshot(root_node)

    undertow_snapshot = UndertowSnapshot(
        model_urn=model_urn,
        assets=assets,
    )

    return DependencyFootprint(
        model_urn=model_urn,
        snapshot=undertow_snapshot,
        baseline_snapshot=baseline_snap,
        paths=final_paths,
        visited_urns=tuple(sorted(visited)),
        max_hops=max_hops,
    )


def _extract_baseline_snapshot(node: LineageNode | None) -> UndertowSnapshot | None:
    """Read the baseline snapshot out of structuredProperties, or institutionalMemory."""
    if not node or not node.aspects:
        return None

    # 1. Check structuredProperties
    sp_aspect = node.aspects.get("structuredProperties")
    if sp_aspect:
        props = (
            getattr(sp_aspect, "properties", None)
            or _get_dict_or_attr(sp_aspect, "properties")
            or []
        )
        for prop in props:
            p_urn = (
                getattr(prop, "propertyUrn", None)
                or _get_dict_or_attr(prop, "propertyUrn")
                or ""
            )
            values = getattr(prop, "values", None) or _get_dict_or_attr(prop, "values") or []
            if "undertow_baseline" in str(p_urn) and values:
                raw_val = values[0]
                val_str = (
                    getattr(raw_val, "value", None)
                    or getattr(raw_val, "stringValue", None)
                    or str(raw_val)
                )
                try:
                    return UndertowSnapshot.model_validate_json(val_str)
                except Exception:
                    try:
                        import json
                        return UndertowSnapshot.model_validate(json.loads(val_str))
                    except Exception:
                        pass

    # 2. Check institutionalMemory fallback
    im_aspect = node.aspects.get("institutionalMemory")
    if im_aspect:
        elements = (
            getattr(im_aspect, "elements", None)
            or _get_dict_or_attr(im_aspect, "elements")
            or []
        )
        for elem in elements:
            desc = (
                getattr(elem, "description", None)
                or _get_dict_or_attr(elem, "description")
                or ""
            )
            if "{" in desc and "model_urn" in desc:
                json_str = desc[desc.find("{"):desc.rfind("}") + 1]
                try:
                    return UndertowSnapshot.model_validate_json(json_str)
                except Exception:
                    pass

    return None


def _fetch_entity(
    urn: str, source: LineageSource, memo: dict[str, LineageNode]
) -> LineageNode | None:
    if urn in memo:
        return memo[urn]
    node = source.get_entity(urn)
    if node is not None:
        memo[urn] = node
    return node


def _find_upstreams(
    node: LineageNode | None, urn: str, entity_type: str, source: LineageSource
) -> list[tuple[str, str]]:
    """Determine upstream edges for an entity.

    Every hop goes through `get_lineage`, including `mlModel -> mlFeature` and
    `mlFeature -> dataset`. Two reasons this is the only workable shape:

    1. **The ML edges are not readable off the entity over MCP.** DataHub's MCP
       server selects name/description/ownership/tags/deprecation/structured
       properties for `MLModel` and `MLFeature` — there is no `mlFeatures` and no
       `sources` field to read. An aspect-based walk resolves zero features there.

    2. **The lineage registry already models them.** `Consumes` and `DerivedFrom`
       are annotated as lineage edges, so both backends return them from a plain
       lineage query, with the relationship name attached. Asking the graph what
       it is connected to beats reconstructing it from two different aspect
       spellings per backend.

    `mlFeatureTable` is filtered by the caller: its `Contains` edge is not
    flagged `isLineage`, so it should never appear here — but it is excluded
    explicitly rather than assumed absent.
    """
    edges = source.get_lineage(urn, direction="UPSTREAM", hops=1)

    results: list[tuple[str, str]] = []
    for edge in edges:
        target = edge.target_urn if edge.source_urn == urn else edge.source_urn
        if target and target != urn:
            results.append((target, edge.relationship or _default_relationship(entity_type)))
    return results


def _default_relationship(entity_type: str) -> str:
    """Fallback edge label when a backend omits the relationship type."""
    return {
        "mlModel": "Consumes",
        "mlFeature": "DerivedFrom",
        "mlPrimaryKey": "DerivedFrom",
    }.get(entity_type, "DownstreamOf")


def _build_attribution_hops(
    chain: list[tuple[str, str, str | None]]
) -> tuple[AttributionHop, ...]:
    """Convert chain [(root_model, 'mlModel', None), (feature, 'mlFeature', 'Consumes'), ...]
    into AttributionHop sequence ordered from root cause (asset) to leaf (mlModel).
    """
    if not chain:
        return ()

    reversed_chain = list(reversed(chain))
    hops: list[AttributionHop] = []

    for idx, (urn, etype, _old_via) in enumerate(reversed_chain):
        # The relationship that reached a hop is recorded on the edge *into* it,
        # so after reversing, each hop takes its predecessor's label. The root
        # has no predecessor and therefore no `via`.
        via = None if idx == 0 else reversed_chain[idx - 1][2]
        hops.append(AttributionHop(urn=urn, entity_type=etype, via=via))

    return tuple(hops)


def _build_asset_snapshot(
    node: LineageNode | None, urn: str, entity_type: str, source: LineageSource
) -> AssetSnapshot:
    """Build AssetSnapshot for an entity from its node aspects or schema fields query."""
    columns: list[ColumnSnapshot] = []
    profile: ProfileSnapshot | None = None
    tags: list[str] = []
    owners: list[str] = []
    deprecated: bool = False
    note: str | None = None

    schema_fields = source.list_schema_fields(urn)
    if schema_fields:
        for sf in schema_fields:
            columns.append(
                ColumnSnapshot(
                    path=sf.field_path,
                    data_type=sf.data_type,
                    native_type=sf.native_type,
                    nullable=sf.nullable,
                    tags=sf.tags,
                )
            )

    if node and node.aspects:
        aspects = node.aspects

        if not columns and "schemaMetadata" in aspects:
            sm = aspects["schemaMetadata"]
            for f in _field(sm, "fields") or []:
                columns.append(
                    ColumnSnapshot(
                        path=_field(f, "fieldPath") or "",
                        data_type=str(_field(f, "dataType") or "unknown"),
                        native_type=_field(f, "nativeDataType"),
                        nullable=bool(_field(f, "nullable") or False),
                    )
                )

        if "datasetProfile" in aspects:
            profile = _profile_snapshot(aspects["datasetProfile"])

        if "globalTags" in aspects:
            for t in _field(aspects["globalTags"], "tags") or []:
                tag_urn = _field(t, "tag")
                tags.append(str(tag_urn if tag_urn is not None else t))

        if "ownership" in aspects:
            for o in _field(aspects["ownership"], "owners") or []:
                owner_urn = _field(o, "owner")
                owners.append(str(owner_urn if owner_urn is not None else o))

        if "deprecation" in aspects:
            dep = aspects["deprecation"]
            deprecated = bool(_field(dep, "deprecated") or False)
            note = _field(dep, "note")

    # `datasetProfile` is a *timeseries* aspect. It never appears in an entity
    # snapshot, so the branch above cannot fire against a live GMS — only
    # against a source that hands back synthetic aspects, which is exactly what
    # the tests did. Sources that can reach the timeseries API expose
    # `get_latest_profile`; without it the footprint is simply unprofiled, and
    # `profile_coverage` reports that honestly rather than reading silence as
    # "no drift".
    if profile is None:
        fetch = getattr(source, "get_latest_profile", None)
        if callable(fetch):
            raw = fetch(urn)
            if raw:
                profile = _profile_snapshot(raw)

    return AssetSnapshot(
        urn=urn,
        entity_type=entity_type,
        columns=tuple(columns),
        profile=profile,
        tags=tuple(tags),
        owners=tuple(owners),
        deprecated=deprecated,
        deprecation_note=note,
    )


def _profile_snapshot(dp: Any) -> ProfileSnapshot:
    """Build a ProfileSnapshot from a datasetProfile aspect, dict or class."""
    fps = [
        FieldProfileSnapshot(
            path=_field(fp, "fieldPath") or "",
            null_count=_field(fp, "nullCount"),
            null_proportion=_field(fp, "nullProportion"),
            unique_count=_field(fp, "uniqueCount"),
            unique_proportion=_field(fp, "uniqueProportion"),
            min=_as_str(_field(fp, "min")),
            max=_as_str(_field(fp, "max")),
            mean=_as_str(_field(fp, "mean")),
            median=_as_str(_field(fp, "median")),
            stdev=_as_str(_field(fp, "stdev")),
        )
        for fp in _field(dp, "fieldProfiles") or []
    ]
    return ProfileSnapshot(
        row_count=_field(dp, "rowCount"),
        column_count=_field(dp, "columnCount"),
        fields=tuple(fps),
    )


def _field(obj: Any, key: str) -> Any:
    """Read `key` off a dict or an object, returning None only when truly absent.

    This replaces a `getattr(x, k, None) or _field(x, k)` idiom that was applied
    to every profile statistic. The `or` collapsed legitimate falsy values —
    `rowCount=0`, `nullCount=0`, `min="0"` — into None, which the differ reads as
    *cannot assess*. Those are exactly the boundary values a drift check cares
    about, so the bug silently blinded the statistical differ where it mattered
    most.
    """
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _as_str(value: Any) -> str | None:
    """Stringify a profile statistic, preserving zero.

    DataHub types min/max/mean/median/stdev as strings so non-numeric columns fit
    the same shape. `str(0)` is "0"; `0 if 0 else None` is None.
    """
    return None if value is None else str(value)


# Retained under the old name: `_get_dict_or_attr` is referenced by
# `_extract_baseline_snapshot` above and by tests.
_get_dict_or_attr = _field


__all__ = ["resolve_footprint"]
