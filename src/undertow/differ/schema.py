"""Schema differ — deterministic set comparison over `schemaMetadata`.

This module produces the findings that BLOCK. A false BLOCK is the one bug that
gets a gate ripped out of a real pipeline, so the rules here are deliberately
asymmetric: a change is reported only when narrowing can be *demonstrated* from
the metadata, not when it merely cannot be ruled out.

Two consequences worth stating plainly, because both are deliberate:

1. **Compatible widenings are not findings.** `int32 → int64`,
   `varchar(64) → varchar(255)`, and `NullType → anything` are silent. There is
   one `COLUMN_TYPE_CHANGED` kind and policy maps it to BLOCK, so emitting a
   safe widening would stop a deploy for nothing.

2. **An unrecognised native type change within the same logical type is treated
   as compatible.** `VARCHAR → TEXT` is a rename, not a narrowing, and there is
   no way to tell those apart from the metadata alone. Blocking on every
   unparseable native string would make the gate fire on platform migrations.

Both trade a false negative for the absence of a false positive. The statistical
differ is the net that catches what this one lets through.

**Assets present in only one snapshot are skipped.** A dataset that vanished
from the footprint is a *lineage* change — the model no longer depends on it —
and reporting every one of its columns as dropped would bury the real finding.
Detecting an upstream that became unreachable belongs to the resolver.
"""

from __future__ import annotations

import re

from undertow.differ._shared import short_urn as _short
from undertow.models import AssetSnapshot, ColumnSnapshot, Finding, FindingKind, UndertowSnapshot

# ---------------------------------------------------------------------------
# Native type parsing
#
# DataHub's logical types are coarse: `NumberType` spans int8 to decimal(38,9).
# Precision only exists in the platform's own spelling, so narrowing detection
# has to parse it. Widths are in bits; parameterised types keep their arguments.
# ---------------------------------------------------------------------------

_INT_WIDTHS: dict[str, int] = {
    "tinyint": 8, "int8": 8, "byteint": 8, "byte": 8,
    "smallint": 16, "int16": 16, "short": 16,
    "mediumint": 24,
    "int": 32, "integer": 32, "int32": 32,
    "bigint": 64, "int64": 64, "long": 64,
}

_FLOAT_WIDTHS: dict[str, int] = {
    "real": 32, "float": 32, "float4": 32, "float32": 32,
    "double": 64, "double precision": 64, "float8": 64, "float64": 64,
}

_DECIMAL_NAMES = frozenset({"decimal", "numeric", "number", "dec"})
# `text` and friends are unbounded, which is why they belong here: it makes
# `varchar(64) -> text` a widening and `text -> varchar(64)` a narrowing, both
# correctly, without a length to compare.
_STRING_NAMES = frozenset(
    {
        "varchar", "varchar2", "char", "character", "nvarchar", "nchar", "string",
        "text", "longtext", "mediumtext", "tinytext", "clob", "nclob",
    }
)

_NATIVE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_ ]*?)\s*(?:\(([^)]*)\))?\s*$")


def _parse_native(native: str | None) -> tuple[str, tuple[int, ...]] | None:
    """`DECIMAL(10,2)` -> `("decimal", (10, 2))`; `bigint` -> `("bigint", ())`."""
    if not native:
        return None
    match = _NATIVE_RE.match(native)
    if match is None:
        return None
    base = match.group(1).strip().lower()
    raw_args = match.group(2)
    if raw_args is None:
        return base, ()
    args: list[int] = []
    for part in raw_args.split(","):
        text = part.strip()
        if not text.isdigit():
            return base, ()  # e.g. varchar(max) — a width we cannot compare
        args.append(int(text))
    return base, tuple(args)


def _normalise_logical(data_type: str) -> str:
    """`NumberType` and `number` are the same thing; pick one spelling."""
    lowered = data_type.strip().lower()
    return lowered[:-4] if lowered.endswith("type") and len(lowered) > 4 else lowered


def _native_compatible(old: str | None, new: str | None) -> bool:
    """Is `old -> new` lossless, judged from the native type strings alone?

    Unknown means compatible. See the module docstring — that asymmetry is the
    whole design of this differ.
    """
    if old is None or new is None:
        return True

    parsed_old, parsed_new = _parse_native(old), _parse_native(new)
    if parsed_old is None or parsed_new is None:
        return True

    old_base, old_args = parsed_old
    new_base, new_args = parsed_new

    old_family, new_family = _family(old_base), _family(new_base)
    if old_family is None or new_family is None:
        return True

    if old_family == "int" and new_family == "int":
        return _width(new_base, _INT_WIDTHS) >= _width(old_base, _INT_WIDTHS)

    if old_family == "float" and new_family == "float":
        return _width(new_base, _FLOAT_WIDTHS) >= _width(old_base, _FLOAT_WIDTHS)

    if old_family == "decimal" and new_family == "decimal":
        # Integral digits and fractional digits must both survive: (10,2) holds
        # 8 digits left of the point, so (8,4) loses two of them despite the
        # larger scale.
        old_p, old_s = _decimal_parts(old_args)
        new_p, new_s = _decimal_parts(new_args)
        return (new_p - new_s) >= (old_p - old_s) and new_s >= old_s

    if old_family == "string" and new_family == "string":
        old_len, new_len = _string_length(old_args), _string_length(new_args)
        return new_len is None or (old_len is not None and new_len >= old_len)

    # Cross-family, still inside DataHub's single NumberType.
    if old_family == "int" and new_family in {"float", "decimal"}:
        # int64 -> float64 loses exactness above 2^53. Reporting that as a
        # blocking change would fire on almost every warehouse migration, so it
        # is accepted as a widening. The mean-shift check would catch real damage.
        return True
    if old_family == "decimal" and new_family == "int":
        return _decimal_parts(old_args)[1] == 0
    return False


def _family(base: str) -> str | None:
    if base in _INT_WIDTHS:
        return "int"
    if base in _FLOAT_WIDTHS:
        return "float"
    if base in _DECIMAL_NAMES:
        return "decimal"
    if base in _STRING_NAMES:
        return "string"
    return None


def _width(base: str, table: dict[str, int]) -> int:
    """Storage width in bits, from the base name alone.

    Arguments are deliberately ignored here: MySQL's `int(11)` is a *display*
    width, not a storage width, so treating it as one would read `int(11)` →
    `int(20)` as a widening when both are the same 32-bit column.
    """
    return table[base]


def _decimal_parts(args: tuple[int, ...]) -> tuple[int, int]:
    """Precision and scale, with DataHub-plausible defaults for the unspecified case."""
    if not args:
        return 38, 9  # unparameterised decimal: assume the widest, so nothing looks narrower
    if len(args) == 1:
        return args[0], 0
    return args[0], args[1]


def _string_length(args: tuple[int, ...]) -> int | None:
    """`None` means unbounded, which is never a narrowing."""
    return args[0] if args else None


def is_compatible_change(old: ColumnSnapshot, new: ColumnSnapshot) -> bool:
    """Can every value valid under `old` still be stored under `new`?

    Public because it is the single most consequential judgement in Undertow and
    deserves to be callable — and testable — on its own.
    """
    old_logical, new_logical = _normalise_logical(old.data_type), _normalise_logical(new.data_type)

    if old_logical != new_logical:
        # A column typed `null` was one the profiler could not infer; it being
        # given a real type is the schema improving, not breaking. The reverse
        # is an inference gap, not evidence of data loss.
        if "null" in {old_logical, new_logical} or "unknown" in {old_logical, new_logical}:
            return True
        return False

    return _native_compatible(old.native_type, new.native_type)


# ---------------------------------------------------------------------------
# The differ
# ---------------------------------------------------------------------------


def diff_schema(baseline: UndertowSnapshot, current: UndertowSnapshot) -> list[Finding]:
    """Compare schemas across every asset present in both snapshots."""
    findings: list[Finding] = []
    for urn in baseline.shared_urns(current):
        before, after = baseline.assets[urn], current.assets[urn]
        findings.extend(_diff_asset(before, after))
    return findings


def _diff_asset(before: AssetSnapshot, after: AssetSnapshot) -> list[Finding]:
    findings: list[Finding] = []
    old_paths = {c.path for c in before.columns}
    new_paths = {c.path for c in after.columns}

    for path in sorted(old_paths - new_paths):
        column = before.column(path)
        assert column is not None  # membership of old_paths guarantees this
        findings.append(_dropped(after, column))

    for path in sorted(new_paths - old_paths):
        column = after.column(path)
        assert column is not None
        findings.append(_added(after, column))

    for path in sorted(old_paths & new_paths):
        old_col, new_col = before.column(path), after.column(path)
        assert old_col is not None and new_col is not None
        findings.extend(_diff_column(after, old_col, new_col))
    return findings


def _diff_column(
    asset: AssetSnapshot, old: ColumnSnapshot, new: ColumnSnapshot
) -> list[Finding]:
    findings: list[Finding] = []

    type_changed = (
        _normalise_logical(old.data_type) != _normalise_logical(new.data_type)
        or old.native_type != new.native_type
    )
    if type_changed and not is_compatible_change(old, new):
        findings.append(_type_changed(asset, old, new))

    if new.nullable and not old.nullable:
        findings.append(_nullability_relaxed(asset, new))

    return findings


# ---------------------------------------------------------------------------
# Finding constructors
# ---------------------------------------------------------------------------


def _dropped(asset: AssetSnapshot, column: ColumnSnapshot) -> Finding:
    return Finding(
        kind=FindingKind.COLUMN_DROPPED,
        subject_urn=asset.urn,
        subject_column=column.path,
        affected_feature_urn=asset.primary_feature(column.path),
        summary=(
            f"Column `{column.path}` was dropped from {_short(asset.urn)}"
            f"{_feature_clause(asset, column.path)}"
        ),
        evidence=_evidence(
            asset,
            column.path,
            data_type=column.data_type,
            native_type=column.native_type,
        ),
    )


def _added(asset: AssetSnapshot, column: ColumnSnapshot) -> Finding:
    return Finding(
        kind=FindingKind.COLUMN_ADDED,
        subject_urn=asset.urn,
        subject_column=column.path,
        affected_feature_urn=asset.primary_feature(column.path),
        summary=f"Column `{column.path}` was added to {_short(asset.urn)}",
        evidence=_evidence(
            asset,
            column.path,
            data_type=column.data_type,
            native_type=column.native_type,
        ),
    )


def _type_changed(asset: AssetSnapshot, old: ColumnSnapshot, new: ColumnSnapshot) -> Finding:
    old_type = old.native_type or old.data_type
    new_type = new.native_type or new.data_type
    return Finding(
        kind=FindingKind.COLUMN_TYPE_CHANGED,
        subject_urn=asset.urn,
        subject_column=new.path,
        affected_feature_urn=asset.primary_feature(new.path),
        summary=(
            f"Column `{new.path}` on {_short(asset.urn)} changed type "
            f"{old_type} → {new_type}, which is not a lossless widening"
            f"{_feature_clause(asset, new.path)}"
        ),
        evidence=_evidence(
            asset,
            new.path,
            old_data_type=old.data_type,
            new_data_type=new.data_type,
            old_native_type=old.native_type,
            new_native_type=new.native_type,
            compatible=False,
        ),
    )


def _nullability_relaxed(asset: AssetSnapshot, column: ColumnSnapshot) -> Finding:
    return Finding(
        kind=FindingKind.COLUMN_NULLABILITY_RELAXED,
        subject_urn=asset.urn,
        subject_column=column.path,
        affected_feature_urn=asset.primary_feature(column.path),
        summary=(
            f"Column `{column.path}` on {_short(asset.urn)} is now nullable; "
            f"nulls will reach anything computing an average over it"
            f"{_feature_clause(asset, column.path)}"
        ),
        evidence=_evidence(asset, column.path, old_nullable=False, new_nullable=True),
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _evidence(
    asset: AssetSnapshot,
    column: str,
    **extra: str | float | int | bool | None,
) -> dict[str, str | float | int | bool | None]:
    """Structured before/after values, so the narrator never invents a figure."""
    features = asset.features_for(column)
    evidence: dict[str, str | float | int | bool | None] = {
        "column": column,
        "on_feature_path": bool(features),
        "features": ", ".join(sorted(features)) or None,
        "column_level_lineage": bool(asset.column_features),
    }
    evidence.update(extra)
    return evidence


def _feature_clause(asset: AssetSnapshot, column: str) -> str:
    features = asset.features_for(column)
    if not features:
        return ""
    if len(features) == 1:
        return f", which feeds {_short(features[0])}"
    return f", which feeds {len(features)} live features"


__all__ = ["diff_schema", "is_compatible_change"]
