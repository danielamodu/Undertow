"""Domain types for Undertow.

Nothing in this module does I/O, imports DataHub, or calls an LLM. The policy
engine is a pure function over these types, which is what makes verdicts
unit-testable and keeps a clever demo input from talking the gate out of a BLOCK.

Note the split between `Finding` and `RuledFinding`. A `Finding` is an observed
fact with no opinion about severity; the policy engine assigns severity and
records which rule fired. Differs are therefore not allowed to decide how
serious anything is, which is the whole point of the deterministic core.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

# Our domain is literally ML models, so `model_urn` is the honest field name.
# Pydantic v2 reserves the `model_` prefix; opt out rather than contort the
# vocabulary into `ml_model_urn` everywhere.
_BASE = ConfigDict(frozen=True, protected_namespaces=())


class Severity(enum.StrEnum):
    """What Undertow tells CI to do. Assigned by the policy engine, never by a differ."""

    CLEAR = "CLEAR"
    WARN = "WARN"
    BLOCK = "BLOCK"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]

    def escalate(self, other: Severity) -> Severity:
        """Aggregate two severities by taking the worse one."""
        return self if self.rank >= other.rank else other


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.CLEAR: 0,
    Severity.WARN: 1,
    Severity.BLOCK: 2,
}


class Confidence(enum.StrEnum):
    """How far a finding is allowed to be trusted.

    CERTAIN is reserved for facts read straight out of the graph: a column is
    present or it is not. PROBABLE covers every statistical inference.

    The separation is load-bearing, not decorative. Only CERTAIN findings may
    BLOCK under the default policy, because a false BLOCK is the one bug that
    gets a gate deleted from a real team's pipeline.
    """

    CERTAIN = "CERTAIN"
    PROBABLE = "PROBABLE"


class FindingKind(enum.StrEnum):
    """What changed. Grouped by the differ that produces it."""

    # Schema differ — read directly off `schemaMetadata`, therefore CERTAIN.
    COLUMN_DROPPED = "COLUMN_DROPPED"
    COLUMN_ADDED = "COLUMN_ADDED"
    COLUMN_TYPE_CHANGED = "COLUMN_TYPE_CHANGED"
    COLUMN_NULLABILITY_RELAXED = "COLUMN_NULLABILITY_RELAXED"

    # Governance differ — read off `deprecation` / `ownership` / `globalTags`.
    ASSET_DEPRECATED = "ASSET_DEPRECATED"
    OWNERSHIP_LOST = "OWNERSHIP_LOST"
    NEW_SENSITIVE_TAG = "NEW_SENSITIVE_TAG"

    # Statistical differ — available from DataHub's default profiling.
    NULL_RATE_JUMP = "NULL_RATE_JUMP"
    CARDINALITY_CHANGE = "CARDINALITY_CHANGE"
    MEAN_SHIFT = "MEAN_SHIFT"
    RANGE_VIOLATION = "RANGE_VIOLATION"
    ROW_COUNT_CHANGE = "ROW_COUNT_CHANGE"

    @property
    def confidence(self) -> Confidence:
        """Confidence is a property of *how we know*, so the kind determines it."""
        return (
            Confidence.PROBABLE
            if self in _PROBABLE_KINDS
            else Confidence.CERTAIN
        )


_PROBABLE_KINDS: frozenset[FindingKind] = frozenset(
    {
        FindingKind.NULL_RATE_JUMP,
        FindingKind.CARDINALITY_CHANGE,
        FindingKind.MEAN_SHIFT,
        FindingKind.RANGE_VIOLATION,
        FindingKind.ROW_COUNT_CHANGE,
    }
)


def short_urn(urn: str) -> str:
    """The human-readable name inside a DataHub URN.

    URNs carry platform and environment for uniqueness, none of which an
    engineer reading an incident report needs:

        urn:li:dataset:(urn:li:dataPlatform:snowflake,transactions.raw,PROD)
            -> transactions.raw
        urn:li:mlFeature:(fraud_detection,transaction_velocity_7d)
            -> transaction_velocity_7d
        urn:li:corpuser:data_eng_tom
            -> data_eng_tom

    Display only — every structured field keeps the full URN, so nothing that is
    written back to DataHub or matched against a policy is affected.
    """
    if not urn.startswith("urn:li:"):
        return urn

    # Flat URNs (corpuser, tag, glossaryTerm) have no parenthesised key.
    if "(" not in urn:
        return urn.rsplit(":", 1)[-1]

    inner = urn[urn.index("(") + 1 : urn.rindex(")")] if ")" in urn else urn[urn.index("(") + 1 :]

    # Split on top-level commas only; the platform URN contains colons but the
    # key itself is comma-delimited.
    depth = 0
    parts: list[str] = []
    current: list[str] = []
    for ch in inner:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    parts.append("".join(current))

    # Drop a trailing fabric (PROD/DEV/...) and any leading platform URN, then
    # take the last remaining segment — the entity's own name.
    meaningful = [p for p in parts if not p.startswith("urn:li:dataPlatform:")]
    if len(meaningful) > 1 and meaningful[-1].isupper():
        meaningful = meaningful[:-1]
    return meaningful[-1] if meaningful else urn


class AttributionHop(BaseModel):
    """One edge on the path from root cause to affected feature."""

    model_config = _BASE

    urn: str
    entity_type: str
    # The DataHub relationship traversed to *reach* this node. None on the root.
    # Verified relationship names: Consumes (mlModel->mlFeature),
    # DerivedFrom (mlFeature->dataset), DownstreamOf (dataset->dataset).
    via: str | None = None
    column: str | None = None

    def label(self) -> str:
        return f"{self.urn}.{self.column}" if self.column else self.urn

    def short_label(self) -> str:
        """`label()` with the URN reduced to its name. For terminal output."""
        name = short_urn(self.urn)
        return f"{name}.{self.column}" if self.column else name


class AttributionPath(BaseModel):
    """The graph path that makes a finding falsifiable.

    This is the product. A drift number without this is an alert nobody can
    action; with it, an engineer knows which commit to look at and who wrote it.
    """

    model_config = _BASE

    hops: tuple[AttributionHop, ...]
    owners: tuple[str, ...] = ()

    @property
    def root(self) -> AttributionHop:
        return self.hops[0]

    @property
    def leaf(self) -> AttributionHop:
        return self.hops[-1]

    @property
    def depth(self) -> int:
        """Edges traversed, not nodes visited."""
        return max(len(self.hops) - 1, 0)


class ColumnSnapshot(BaseModel):
    """One column, flattened from `schemaMetadata.fields`.

    `data_type` is DataHub's *logical* type — the union member name from
    `SchemaFieldDataType` (string, number, boolean, date, …). It is coarse:
    `NumberType` covers int8 through decimal(38,9). Precision therefore lives in
    `native_type`, which is the platform's own spelling (`DECIMAL(10,2)`,
    `bigint`, `varchar(64)`), and that is the only place a narrowing change is
    visible at all.
    """

    model_config = _BASE

    path: str  # DataHub's `fieldPath` — the identity of a column
    data_type: str = "unknown"
    native_type: str | None = None
    nullable: bool = False
    tags: tuple[str, ...] = ()


class FieldProfileSnapshot(BaseModel):
    """Profiled statistics for one column.

    Every field except `path` is optional, exactly as in the PDL. That is not
    defensive padding: DataHub's profiler is off by default, and quantiles,
    histograms, and distinct-value frequencies are each individually off even
    when it is on. A missing value means *cannot assess*, never *no drift*.

    `min`/`max`/`mean`/`median`/`stdev` are typed `str` because DataHub types
    them that way — so a non-numeric column can be described with the same
    shape. Parse defensively; never assume float-castable.
    """

    model_config = _BASE

    path: str
    null_count: int | None = None
    null_proportion: float | None = None
    unique_count: int | None = None
    unique_proportion: float | None = None
    min: str | None = None
    max: str | None = None
    mean: str | None = None
    median: str | None = None
    stdev: str | None = None
    sample_values: tuple[str, ...] = ()


class ProfileSnapshot(BaseModel):
    """A `datasetProfile` reduced to what the differ reads.

    `row_count` lives here rather than on the field profile — that asymmetry is
    DataHub's, and `tests/test_sdk_assumptions.py` pins it.
    """

    model_config = _BASE

    row_count: int | None = None
    column_count: int | None = None
    fields: tuple[FieldProfileSnapshot, ...] = ()

    def field_profile(self, path: str) -> FieldProfileSnapshot | None:
        return next((f for f in self.fields if f.path == path), None)


class AssetSnapshot(BaseModel):
    """One asset in the footprint, as it looked at capture time."""

    model_config = _BASE

    urn: str
    entity_type: str = "dataset"
    columns: tuple[ColumnSnapshot, ...] = ()
    profile: ProfileSnapshot | None = None
    tags: tuple[str, ...] = ()
    owners: tuple[str, ...] = ()
    deprecated: bool = False
    deprecation_note: str | None = None

    # mlFeature URNs this asset feeds. Asset-level, because DataHub's
    # `DerivedFrom` edge is asset-to-asset: `mlFeatureProperties.sources` holds
    # dataset URNs, not columns.
    feeds_features: tuple[str, ...] = ()
    # Column-level refinement, when fine-grained lineage resolved it. Empty
    # means *unknown*, and unknown falls back to the asset-level answer rather
    # than to silence — a gate that goes quiet when it loses resolution is worse
    # than one that is occasionally broad.
    column_features: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    def column(self, path: str) -> ColumnSnapshot | None:
        return next((c for c in self.columns if c.path == path), None)

    def field_profile(self, path: str) -> FieldProfileSnapshot | None:
        return self.profile.field_profile(path) if self.profile else None

    def features_for(self, column: str | None = None) -> tuple[str, ...]:
        """Features reachable from this asset, narrowed to one column when known."""
        if column is not None and self.column_features:
            return self.column_features.get(column, ())
        return self.feeds_features

    def primary_feature(self, column: str | None = None) -> str | None:
        """One feature to name in a report. Deterministic; the rest go in evidence."""
        features = self.features_for(column)
        return min(features) if features else None


class UndertowSnapshot(BaseModel):
    """The full footprint at a point in time — the unit the differs compare.

    Captured at the last CLEARED deploy and persisted both to `.undertow/` and
    back into DataHub, so a fresh CI runner with no local cache still has a
    baseline to diff against.
    """

    model_config = _BASE

    model_urn: str
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    assets: dict[str, AssetSnapshot] = Field(default_factory=dict)
    baseline_ref: str | None = None

    def asset(self, urn: str) -> AssetSnapshot | None:
        return self.assets.get(urn)

    def shared_urns(self, other: UndertowSnapshot) -> tuple[str, ...]:
        """Assets present in both, sorted.

        Sorted because a CI gate that reports findings in dict-insertion order
        produces a different diff on every run for no reason.
        """
        return tuple(sorted(self.assets.keys() & other.assets.keys()))


class Finding(BaseModel):
    """An observed fact. Carries no severity — see the module docstring."""

    model_config = _BASE

    kind: FindingKind
    # The asset the change was observed *on* (usually an upstream dataset).
    subject_urn: str
    subject_column: str | None = None
    # The mlFeature this change reaches, if the differ resolved one.
    affected_feature_urn: str | None = None
    summary: str
    # Raw before/after numbers. Kept structured so the narrator never has to
    # invent a figure and the reporter can render evidence verbatim.
    evidence: dict[str, str | float | int | bool | None] = Field(default_factory=dict)
    path: AttributionPath | None = None

    @property
    def confidence(self) -> Confidence:
        return self.kind.confidence


class RuledFinding(BaseModel):
    """A finding after the policy engine has judged it."""

    model_config = _BASE

    finding: Finding
    severity: Severity
    rule_id: str
    # Set when a rule matched but an exemption downgraded it, so the report can
    # show that someone consciously accepted this risk and when that expires.
    exempted_by: str | None = None
    exemption_expires: datetime | None = None


class Verdict(BaseModel):
    """The output of an Undertow run. Exit code and write-back both derive from this."""

    model_config = _BASE

    model_urn: str
    severity: Severity
    ruled_findings: tuple[RuledFinding, ...] = ()
    baseline_ref: str | None = None
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Assets walked but found unchanged. Surfaced because "we checked 8 datasets
    # and 12 features" is what makes a CLEAR verdict mean something.
    assets_checked: int = 0

    def exit_code(self, *, fail_on_warn: bool = False) -> int:
        """The process exit status for this verdict. The only place CI status is decided.

        Three codes, and the third is the one that matters:

            0  proceed   — CLEAR, or WARN when the caller hasn't opted in
            1  blocked   — the gate ran and said stop
            2  error     — the gate could not produce a verdict at all

        Separating 1 from 2 is deliberate. An earlier revision used 2 for both a
        BLOCK and a crash, which left a pipeline unable to distinguish "your
        upstream broke" from "Undertow broke" — the two outcomes that demand
        completely different responses.

        WARN exits 0 by default because the product says a warning annotates a
        deploy rather than stopping one. `fail_on_warn` is for teams that want
        the stricter reading, and it is a choice they make explicitly.
        """
        if self.severity is Severity.BLOCK:
            return 1
        if self.severity is Severity.WARN and fail_on_warn:
            return 1
        return 0

    def of_severity(self, severity: Severity) -> tuple[RuledFinding, ...]:
        return tuple(rf for rf in self.ruled_findings if rf.severity is severity)

    @property
    def blocking(self) -> tuple[RuledFinding, ...]:
        return self.of_severity(Severity.BLOCK)

    @property
    def warnings(self) -> tuple[RuledFinding, ...]:
        return self.of_severity(Severity.WARN)

    def headline(self) -> str:
        n_block, n_warn = len(self.blocking), len(self.warnings)
        if self.severity is Severity.CLEAR:
            return f"CLEAR — {self.assets_checked} assets checked, no material change"
        return f"{self.severity.value} — {n_block} blocking, {n_warn} warning"


class DependencyFootprint(BaseModel):
    """The dependency footprint of an mlModel, resolved from DataHub's lineage graph.

    Contains the full UndertowSnapshot (for diffing) and the AttributionPath mapping
    from each asset back to the root model.
    """

    model_config = _BASE

    model_urn: str
    snapshot: UndertowSnapshot
    baseline_snapshot: UndertowSnapshot | None = None
    paths: dict[str, AttributionPath] = Field(default_factory=dict)
    visited_urns: tuple[str, ...] = ()
    max_hops: int = 5

    # Assets that sat on the hop cap with upstreams we never walked. Empty means
    # the footprint is complete; non-empty means part of the graph was not
    # looked at, and therefore could not have produced a finding.
    #
    # This exists for the same reason `ProfileCoverage` does. An asset beyond
    # the cap is absent from `snapshot.assets`, so it drops out of
    # `shared_urns()`, so it yields no findings, so it contributes CLEAR — a
    # verdict indistinguishable from "we looked and nothing had changed". The
    # cap is a legitimate bound on traversal cost; being quiet about hitting it
    # is what turns it into a false negative.
    truncated_urns: tuple[str, ...] = ()

    @property
    def assets_checked(self) -> int:
        return len(self.snapshot.assets)

    @property
    def truncated(self) -> bool:
        """Did the hop cap stop the walk somewhere with more graph behind it?"""
        return bool(self.truncated_urns)

    def truncation_summary(self) -> str | None:
        """One line for a report, or None when the footprint is complete."""
        if not self.truncated_urns:
            return None
        n = len(self.truncated_urns)
        return (
            f"Traversal stopped at the {self.max_hops}-hop limit with "
            f"{n} asset(s) still having unwalked upstreams. Anything beyond them "
            f"was not examined and cannot appear as a finding — raise `max_hops` "
            f"in undertow.yaml to widen the walk."
        )

