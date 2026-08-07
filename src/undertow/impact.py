"""Gate the pull request that changes the SQL, not the deploy that trips over it.

`undertow check` runs at deploy time: the table has already been rebuilt, the
column is already gone, and the model is already broken — the gate just stops it
reaching production. Useful, but late. The engineer who caused it has moved on to
something else, and the ML team finds out by being blocked.

This module runs earlier. Given the SQL a pull request changes, it parses the new
statement, compares the columns it *would* produce against the columns the table
has in DataHub today, and walks downstream from anything that disappears. The
answer lands as a comment on the PR that removes the column, addressed to the
person removing it, before it merges:

    Removing `amount` from staging.transactions_clean breaks
    transaction_velocity_7d, which feeds fraud_detector_v3 (@ml_eng_alex).

The comparison is a schema diff, so it is `CERTAIN` in the same sense the deploy
gate's schema findings are — this is not a guess about what might break. What it
cannot see is whether the change is *intended*; a column removed on purpose is
still a column removed, and the answer to that is a human reading the comment.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from undertow.models import short_urn
from undertow.resolver.base import LineageSource, parse_entity_type

# Datasets and features are worth walking through; anything else is a leaf as far
# as "does this reach a model" is concerned.
_TRAVERSABLE = {"dataset", "mlFeature", "mlFeatureTable", "mlPrimaryKey"}


@dataclass(frozen=True)
class ImpactedModel:
    """A production model reachable from a column this PR removes."""

    model_urn: str
    owners: tuple[str, ...]
    path: tuple[str, ...]

    @property
    def name(self) -> str:
        return short_urn(self.model_urn)

    def route(self) -> str:
        """The hops between the changed table and this model, names only."""
        return " → ".join(short_urn(u) for u in self.path[1:])


@dataclass(frozen=True)
class SqlImpact:
    """What one changed SQL file does to the catalog."""

    sql_file: str
    table_urn: str
    dropped_columns: tuple[str, ...]
    added_columns: tuple[str, ...]
    impacted: tuple[ImpactedModel, ...]
    parse_error: str | None = None

    @property
    def table_name(self) -> str:
        return short_urn(self.table_urn)

    @property
    def is_breaking(self) -> bool:
        """A dropped column that reaches a model. Additions never break anything."""
        return bool(self.dropped_columns and self.impacted)


def downstream_models(
    seed_urn: str, source: LineageSource, *, max_hops: int = 6
) -> list[ImpactedModel]:
    """Breadth-first walk downstream from `seed_urn`, collecting mlModels.

    Depth-bounded and cycle-guarded for the same reason the upstream traversal
    is: a lineage graph is not guaranteed acyclic, and a gate that hangs is a
    gate that gets removed from CI.
    """
    seen = {seed_urn}
    frontier: list[tuple[str, tuple[str, ...]]] = [(seed_urn, (seed_urn,))]
    found: list[ImpactedModel] = []

    for _ in range(max_hops):
        if not frontier:
            break
        next_frontier: list[tuple[str, tuple[str, ...]]] = []

        for urn, path in frontier:
            try:
                edges = source.get_lineage(urn, direction="DOWNSTREAM")
            except Exception:
                # One unreachable node must not lose the rest of the walk. The
                # cost is a possibly incomplete list, which the reporter states.
                continue

            for edge in edges:
                target = edge.target_urn
                if target in seen:
                    continue
                seen.add(target)
                target_path = (*path, target)
                entity = parse_entity_type(target)

                if entity == "mlModel":
                    found.append(
                        ImpactedModel(
                            model_urn=target,
                            owners=_owners_of(target, source),
                            path=target_path,
                        )
                    )
                elif entity in _TRAVERSABLE:
                    next_frontier.append((target, target_path))

        frontier = next_frontier

    return sorted(found, key=lambda m: m.model_urn)


def _owners_of(urn: str, source: LineageSource) -> tuple[str, ...]:
    try:
        node = source.get_entity(urn)
    except Exception:
        return ()
    if node is None or not node.aspects:
        return ()

    ownership = node.aspects.get("ownership")
    if ownership is None:
        return ()

    owners = getattr(ownership, "owners", None)
    if owners is None and isinstance(ownership, dict):
        owners = ownership.get("owners")

    result = []
    for owner in owners or []:
        value = getattr(owner, "owner", None)
        if value is None and isinstance(owner, dict):
            value = owner.get("owner")
        if value:
            result.append(str(value))
    return tuple(result)


def analyse_sql(
    sql_path: str | Path,
    *,
    source: LineageSource,
    schema_resolver: Any,
    max_hops: int = 6,
) -> SqlImpact | None:
    """Compare one changed SQL file against the catalog. `None` if it builds nothing.

    `schema_resolver` is DataHub's, backed by a live graph, so the parse binds
    columns against the schemas the catalog actually holds rather than against a
    second description of them.
    """
    from datahub.sql_parsing.sqlglot_lineage import infer_output_schema, sqlglot_lineage

    path = Path(sql_path)
    sql = path.read_text(encoding="utf-8")
    result = sqlglot_lineage(sql, schema_resolver=schema_resolver)

    if not result.out_tables:
        # A SELECT that creates nothing cannot change a table's shape.
        return None

    table_urn = result.out_tables[0]

    if result.debug_info.error:
        return SqlImpact(
            sql_file=path.as_posix(),
            table_urn=table_urn,
            dropped_columns=(),
            added_columns=(),
            impacted=(),
            parse_error=f"{type(result.debug_info.error).__name__}: {result.debug_info.error}",
        )

    proposed = [f.fieldPath for f in (infer_output_schema(result) or [])]
    if not proposed:
        return SqlImpact(
            sql_file=path.as_posix(),
            table_urn=table_urn,
            dropped_columns=(),
            added_columns=(),
            impacted=(),
            parse_error="could not infer an output schema from this statement",
        )

    current = [f.field_path for f in source.list_schema_fields(table_urn)]
    if not current:
        # The table is not in the catalog yet — a new model, not a change to an
        # existing one. Nothing downstream can depend on it.
        return SqlImpact(
            sql_file=path.as_posix(),
            table_urn=table_urn,
            dropped_columns=(),
            added_columns=tuple(proposed),
            impacted=(),
        )

    dropped = tuple(c for c in current if c not in proposed)
    added = tuple(c for c in proposed if c not in current)

    impacted = tuple(downstream_models(table_urn, source, max_hops=max_hops)) if dropped else ()

    return SqlImpact(
        sql_file=path.as_posix(),
        table_urn=table_urn,
        dropped_columns=dropped,
        added_columns=added,
        impacted=impacted,
    )


def format_pr_comment(impacts: list[SqlImpact], *, project_url: str) -> str:
    """Markdown for the pull request that changes the SQL."""
    breaking = [i for i in impacts if i.is_breaking]
    changed = [i for i in impacts if i.dropped_columns and not i.impacted]

    lines: list[str] = []
    if breaking:
        lines.append("## 🔴 Undertow: this PR removes columns that production models depend on")
    elif changed:
        lines.append("## 🟡 Undertow: this PR removes columns")
    else:
        lines.append("## 🟢 Undertow: no columns removed")
    lines.append("")

    for impact in impacts:
        if impact.parse_error:
            lines.append(f"- `{impact.sql_file}` — could not parse: {impact.parse_error}")
            continue
        if not impact.dropped_columns:
            continue

        dropped = ", ".join(f"`{c}`" for c in impact.dropped_columns)
        lines.append(f"### `{impact.table_name}` — drops {dropped}")
        lines.append("")
        lines.append(f"Defined by `{impact.sql_file}`.")
        lines.append("")

        if not impact.impacted:
            lines.append("No production models were found downstream of this table.")
            lines.append("")
            continue

        lines.append("| Model | Owner | Reached via |")
        lines.append("| --- | --- | --- |")
        for model in impact.impacted:
            owners = ", ".join(f"@{short_urn(o)}" for o in model.owners) or "_unassigned_"
            lines.append(f"| `{model.name}` | {owners} | {model.route()} |")
        lines.append("")

    if breaking:
        lines.append(
            "These models are gated on the columns above. Merging this will block "
            "their next deploy until their baselines are re-approved."
        )
        lines.append("")

    lines.append("---")
    lines.append(f"*[Undertow]({project_url}) — checked before merge, not after deploy*")
    return "\n".join(lines)


__all__ = [
    "ImpactedModel",
    "SqlImpact",
    "analyse_sql",
    "downstream_models",
    "format_pr_comment",
]
