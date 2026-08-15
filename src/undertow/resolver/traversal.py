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

    truncated: list[str] = []

    while queue:
        curr_urn, depth, chain = queue.popleft()
        node = _fetch_entity(curr_urn, source, memo)
        entity_type = parse_entity_type(curr_urn)

        snapshot = _build_asset_snapshot(node, curr_urn, entity_type, source)
        assets[curr_urn] = snapshot

        attribution_hops = _build_attribution_hops(chain)
        path_hops[curr_urn] = attribution_hops

        # Asked before the depth check, not after. Knowing whether the cap cost
        # us anything means asking what was behind it, which is one extra
        # lineage query per node sitting on the boundary. That is the price of
        # not reporting a truncated walk as a clean one, and it is the same
        # trade `profile_coverage` makes: a gate is allowed to be bounded, it is
        # not allowed to be quietly bounded.
        upstreams = _find_upstreams(node, curr_urn, entity_type, source)
        unwalked = [
            urn
            for urn, _ in upstreams
            if urn not in visited and parse_entity_type(urn) != "mlFeatureTable"
        ]

        if depth >= max_hops:
            # Only when something was actually left behind. A genuine leaf at
            # the cap lost nothing, and warning about it would make the message
            # fire on healthy footprints until a team learned to ignore it —
            # which is how a real signal gets trained out of existence.
            if unwalked:
                truncated.append(curr_urn)
            continue

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

    _apply_column_features(assets, dataset_features, source)

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
        truncated_urns=tuple(sorted(truncated)),
    )


def _split_schema_field(urn: str) -> tuple[str, str] | None:
    """`urn:li:schemaField:(<dataset urn>,amount)` -> `(<dataset urn>, "amount")`.

    Returns None for anything that is not a schemaField URN, which is the
    common case: a column-lineage query can legitimately come back pointing at
    whole datasets when the platform never emitted fine-grained lineage.
    """
    if not urn.startswith("urn:li:schemaField:("):
        return None
    inner = urn[urn.index("(") + 1 : urn.rindex(")")] if urn.endswith(")") else None
    if inner is None:
        return None
    # The dataset URN contains its own commas, so split on the last one.
    parent, _, column = inner.rpartition(",")
    if not parent or not column:
        return None
    return parent, column


def _apply_column_features(
    assets: dict[str, AssetSnapshot],
    dataset_features: dict[str, set[str]],
    source: LineageSource,
) -> None:
    """Narrow "this asset feeds these features" down to "this *column* does".

    `feeds_features` is asset-level because DataHub's `DerivedFrom` edge is:
    `mlFeatureProperties.sources` holds dataset URNs, never columns. So without
    this pass, dropping any column of a table that feeds a feature implicates
    that feature — including columns nothing downstream ever reads.

    The refinement has to come from the other end. Fine-grained lineage between
    *datasets* is something DataHub models well, so for a dataset D that feeds
    features, each of D's columns can be walked upstream to the columns that
    produce it, and those columns inherit D's features. Propagation is
    transitive: a raw table three hops up gets its features through whatever
    chain of column lineage reaches it.

    Mutates `assets` in place. Silent no-op when the source cannot answer
    column-level questions — only the MCP source can today — and the empty
    `column_features` that leaves behind is read as *unknown*, falling back to
    the asset-level answer rather than to silence. That fallback is the reason
    this can be added without changing any existing verdict.

    Cost: one lineage query per column reached on the feature-feeding path.
    Deliberately uncapped. A cap would silently stop resolving partway through
    and leave findings attributed to the wrong features, which is the same
    class of quiet blindness `truncated_urns` exists to prevent.
    """
    fetch = getattr(source, "get_column_lineage", None)
    if not callable(fetch):
        return

    # (dataset_urn, column) -> features that column feeds.
    resolved: dict[tuple[str, str], set[str]] = {}

    # Seed from every dataset that directly feeds a feature. The seeding
    # dataset's own columns are deliberately not recorded: we know the table
    # feeds the feature but not which of its columns do, and inventing a
    # column-level answer there would be worse than falling back.
    frontier: list[tuple[str, str, frozenset[str]]] = []
    for ds_urn, feat_set in dataset_features.items():
        asset = assets.get(ds_urn)
        if asset is None:
            continue
        features = frozenset(feat_set)
        for col in asset.columns:
            frontier.append((ds_urn, col.path, features))

    seen: set[tuple[str, str, frozenset[str]]] = set()
    while frontier:
        ds_urn, column, features = frontier.pop()
        key = (ds_urn, column, features)
        if key in seen:
            continue
        seen.add(key)

        try:
            edges = fetch(ds_urn, column)
        except Exception:
            # Fine-grained lineage is an enrichment. A backend that errors on
            # one column must not take down a resolution that is otherwise
            # complete — the asset-level answer is still correct, just broader.
            continue

        for edge in edges or []:
            target = edge.target_urn if edge.source_urn == ds_urn else edge.source_urn
            parsed = _split_schema_field(target)
            if parsed is None:
                continue
            up_urn, up_column = parsed
            if up_urn not in assets:
                continue  # outside the footprint; not ours to attribute
            resolved.setdefault((up_urn, up_column), set()).update(features)
            frontier.append((up_urn, up_column, features))

    by_asset: dict[str, dict[str, tuple[str, ...]]] = {}
    for (urn, column), reached in resolved.items():
        by_asset.setdefault(urn, {})[column] = tuple(sorted(reached))

    for urn, mapping in by_asset.items():
        assets[urn] = assets[urn].model_copy(update={"column_features": mapping})


def _member(obj: Any, name: str) -> Any:
    """Read `name` off a mapping or an object, without ever returning a method.

    `getattr(some_dict, "values")` returns `dict.values` — a bound method, and
    truthy. Aspects arrive as schema classes from the SDK and as plain dicts
    from the MCP server and from recordings, so a naive
    `getattr(x, "values", None) or x.get("values")` silently picks up the
    method for every dict and then subscripts it.

    Mapping first, attributes second, and never a callable.
    """
    if isinstance(obj, dict):
        return obj.get(name)
    value = getattr(obj, name, None)
    return None if callable(value) else value


def _extract_baseline_snapshot(node: LineageNode | None) -> UndertowSnapshot | None:
    """Read the baseline snapshot out of structuredProperties, or institutionalMemory."""
    if not node or not node.aspects:
        return None

    # 1. Check structuredProperties
    sp_aspect = node.aspects.get("structuredProperties")
    if sp_aspect:
        props = _member(sp_aspect, "properties") or []
        for prop in props:
            p_urn = _member(prop, "propertyUrn") or ""
            values = _member(prop, "values") or []
            if "undertow_baseline" in str(p_urn) and values:
                raw_val = values[0]
                val_str = (
                    _member(raw_val, "value")
                    or _member(raw_val, "stringValue")
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
        elements = _member(im_aspect, "elements") or []
        for elem in elements:
            desc = _member(elem, "description") or ""
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
    #
    # Only datasets are asked. `datasetProfile` is a dataset aspect by
    # definition, and GMS does not answer quickly when asked for one about an
    # mlModel — it takes ~28 seconds to come back with nothing. A six-asset
    # footprint of three datasets, two features and a model spent 85 of its 87
    # seconds waiting for three of those, all to be told None. The guard is one
    # line; the cost of not having it grows with every feature a model consumes.
    if profile is None and entity_type == "dataset":
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
