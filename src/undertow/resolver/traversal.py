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
    """Extract baseline UndertowSnapshot from model's structuredProperties or institutionalMemory."""
    if not node or not node.aspects:
        return None

    # 1. Check structuredProperties
    sp_aspect = node.aspects.get("structuredProperties")
    if sp_aspect:
        props = getattr(sp_aspect, "properties", None) or _get_dict_or_attr(sp_aspect, "properties") or []
        for prop in props:
            p_urn = getattr(prop, "propertyUrn", None) or _get_dict_or_attr(prop, "propertyUrn") or ""
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
        elements = getattr(im_aspect, "elements", None) or _get_dict_or_attr(im_aspect, "elements") or []
        for elem in elements:
            desc = getattr(elem, "description", None) or _get_dict_or_attr(elem, "description") or ""
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
    """Determine upstream edges for an entity."""
    results: list[tuple[str, str]] = []

    if entity_type == "mlModel":
        if node and "mlModelProperties" in node.aspects:
            props = node.aspects["mlModelProperties"]
            features = getattr(props, "mlFeatures", None) or _get_dict_or_attr(props, "mlFeatures") or []
            for f in features:
                f_urn = str(f)
                results.append((f_urn, "Consumes"))

    elif entity_type == "mlFeature":
        if node and "mlFeatureProperties" in node.aspects:
            props = node.aspects["mlFeatureProperties"]
            sources = getattr(props, "sources", None) or _get_dict_or_attr(props, "sources") or []
            for s in sources:
                s_urn = str(s)
                results.append((s_urn, "DerivedFrom"))

    elif entity_type == "dataset":
        lineage_edges = source.get_lineage(urn, direction="UPSTREAM", hops=1)
        for edge in lineage_edges:
            target = edge.target_urn if edge.source_urn == urn else edge.source_urn
            if target and target != urn:
                rel = edge.relationship or "DownstreamOf"
                results.append((target, rel))

    return results


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
        if idx == 0:
            via = None
        else:
            via = reversed_chain[idx - 1][2]
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
            raw_fields = getattr(sm, "fields", None) or _get_dict_or_attr(sm, "fields") or []
            for f in raw_fields:
                path = getattr(f, "fieldPath", None) or _get_dict_or_attr(f, "fieldPath") or ""
                dtype = getattr(f, "dataType", None) or _get_dict_or_attr(f, "dataType") or "unknown"
                native = getattr(f, "nativeDataType", None) or _get_dict_or_attr(f, "nativeDataType")
                nullable = getattr(f, "nullable", False) or _get_dict_or_attr(f, "nullable") or False
                columns.append(
                    ColumnSnapshot(
                        path=path,
                        data_type=str(dtype),
                        native_type=native,
                        nullable=nullable,
                    )
                )

        if "datasetProfile" in aspects:
            dp = aspects["datasetProfile"]
            row_count = getattr(dp, "rowCount", None) or _get_dict_or_attr(dp, "rowCount")
            col_count = getattr(dp, "columnCount", None) or _get_dict_or_attr(dp, "columnCount")
            raw_fps = getattr(dp, "fieldProfiles", None) or _get_dict_or_attr(dp, "fieldProfiles") or []
            fps: list[FieldProfileSnapshot] = []
            for fp in raw_fps:
                path = getattr(fp, "fieldPath", None) or _get_dict_or_attr(fp, "fieldPath") or ""
                fps.append(
                    FieldProfileSnapshot(
                        path=path,
                        null_count=getattr(fp, "nullCount", None) or _get_dict_or_attr(fp, "nullCount"),
                        null_proportion=getattr(fp, "nullProportion", None) or _get_dict_or_attr(fp, "nullProportion"),
                        unique_count=getattr(fp, "uniqueCount", None) or _get_dict_or_attr(fp, "uniqueCount"),
                        unique_proportion=getattr(fp, "uniqueProportion", None) or _get_dict_or_attr(fp, "uniqueProportion"),
                        min=str(getattr(fp, "min", None) or _get_dict_or_attr(fp, "min")) if getattr(fp, "min", None) or _get_dict_or_attr(fp, "min") else None,
                        max=str(getattr(fp, "max", None) or _get_dict_or_attr(fp, "max")) if getattr(fp, "max", None) or _get_dict_or_attr(fp, "max") else None,
                        mean=str(getattr(fp, "mean", None) or _get_dict_or_attr(fp, "mean")) if getattr(fp, "mean", None) or _get_dict_or_attr(fp, "mean") else None,
                        median=str(getattr(fp, "median", None) or _get_dict_or_attr(fp, "median")) if getattr(fp, "median", None) or _get_dict_or_attr(fp, "median") else None,
                        stdev=str(getattr(fp, "stdev", None) or _get_dict_or_attr(fp, "stdev")) if getattr(fp, "stdev", None) or _get_dict_or_attr(fp, "stdev") else None,
                    )
                )
            profile = ProfileSnapshot(row_count=row_count, column_count=col_count, fields=tuple(fps))

        if "globalTags" in aspects:
            gt = aspects["globalTags"]
            tag_list = getattr(gt, "tags", None) or _get_dict_or_attr(gt, "tags") or []
            for t in tag_list:
                tag_urn = getattr(t, "tag", None) or _get_dict_or_attr(t, "tag") or str(t)
                tags.append(str(tag_urn))

        if "ownership" in aspects:
            ow = aspects["ownership"]
            owner_list = getattr(ow, "owners", None) or _get_dict_or_attr(ow, "owners") or []
            for o in owner_list:
                owner_urn = getattr(o, "owner", None) or _get_dict_or_attr(o, "owner") or str(o)
                owners.append(str(owner_urn))

        if "deprecation" in aspects:
            dep = aspects["deprecation"]
            deprecated = bool(getattr(dep, "deprecated", False) or _get_dict_or_attr(dep, "deprecated") or False)
            note = getattr(dep, "note", None) or _get_dict_or_attr(dep, "note")

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


def _get_dict_or_attr(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


__all__ = ["resolve_footprint"]
