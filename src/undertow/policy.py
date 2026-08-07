"""Policy: the rules that turn findings into severities, loaded from `undertow.yaml`.

Defaults are deliberately opinionated so Undertow is useful with no config file
at all. The shipped defaults encode one judgement: **a column disappearing under
a live feature blocks; a distribution moving warns.** Teams can override, but
they have to say so in writing.
"""

from __future__ import annotations

import fnmatch
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from undertow.models import Finding, FindingKind, Severity

_BASE = ConfigDict(frozen=True, protected_namespaces=())


class Rule(BaseModel):
    """Maps a finding kind to a severity."""

    model_config = _BASE

    id: str
    kind: FindingKind
    severity: Severity


class Exemption(BaseModel):
    """A consciously accepted risk, with a reason and an end date.

    Both fields are mandatory by design. An exemption without a reason is
    unreviewable; one without an expiry never gets revisited. `validate_policy`
    rejects violations at load time rather than at verdict time.
    """

    model_config = _BASE

    reason: str
    expires: datetime | None = None
    downgrade_to: Severity = Severity.WARN

    # Match criteria. Unset means "any". Glob syntax, because URNs are long and
    # nobody wants to paste a full mlFeature URN into a config file.
    kind: FindingKind | None = None
    urn_pattern: str | None = None
    column: str | None = None

    @field_validator("expires")
    @classmethod
    def _tz_aware(cls, v: datetime | None) -> datetime | None:
        # A naive datetime compared against an aware `now` raises TypeError at
        # the worst possible moment. Normalise on the way in.
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v

    def matches(self, finding: Finding) -> bool:
        if self.kind is not None and self.kind is not finding.kind:
            return False
        if self.urn_pattern is not None and not fnmatch.fnmatch(
            finding.subject_urn, self.urn_pattern
        ):
            return False
        return self.column is None or self.column == finding.subject_column

    def is_active(self, now: datetime) -> bool:
        return self.expires is None or now < self.expires


class Thresholds(BaseModel):
    """Statistical trigger points.

    All of these read statistics DataHub profiles by default, so they work on a
    stock instance. PSI was removed deliberately — it needs quantiles or
    histograms, which are `default=False` in `ge_profiling_config.py`, so it
    could never fire outside our own fixture. See `differ/statistical.py`.
    """

    model_config = _BASE

    null_rate_jump_pp: float = 10.0      # percentage points, absolute
    cardinality_change_pct: float = 50.0
    mean_shift_sigma: float = 3.0
    row_count_change_pct: float = 50.0


_DEFAULT_SEVERITIES: dict[FindingKind, Severity] = {
    # CERTAIN — read straight off the graph. These block.
    FindingKind.COLUMN_DROPPED: Severity.BLOCK,
    FindingKind.COLUMN_TYPE_CHANGED: Severity.BLOCK,
    FindingKind.ASSET_DEPRECATED: Severity.BLOCK,
    # Relaxed nullability doesn't break a read, it silently poisons a mean.
    # Serious, but not "stop the deploy" without evidence it landed.
    FindingKind.COLUMN_NULLABILITY_RELAXED: Severity.WARN,
    FindingKind.OWNERSHIP_LOST: Severity.WARN,
    FindingKind.NEW_SENSITIVE_TAG: Severity.WARN,
    # Informational. A new upstream column breaks nothing, but "4 added, 1
    # dropped" is a more useful report than "1 dropped", and CLEAR findings can
    # never move a verdict — `escalate` treats them as the identity.
    FindingKind.COLUMN_ADDED: Severity.CLEAR,
    # PROBABLE — statistical. These warn. The engine enforces this even if a
    # policy file tries to set BLOCK, unless `allow_probable_block` is set.
    FindingKind.NULL_RATE_JUMP: Severity.WARN,
    FindingKind.CARDINALITY_CHANGE: Severity.WARN,
    FindingKind.MEAN_SHIFT: Severity.WARN,
    FindingKind.RANGE_VIOLATION: Severity.WARN,
    FindingKind.ROW_COUNT_CHANGE: Severity.WARN,
}


DEFAULT_SENSITIVE_TAGS: tuple[str, ...] = (
    "PII",
    "Sensitive",
    "Confidential",
    "Restricted",
    "PCI",
    "PHI",
    "GDPR",
    "HIPAA",
)


class Policy(BaseModel):
    """A loaded `undertow.yaml`, or the defaults."""

    model_config = ConfigDict(protected_namespaces=())

    rules: dict[str, Severity] = Field(default_factory=dict)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    exemptions: tuple[Exemption, ...] = ()

    # Tags and glossary terms whose *appearance* on a feature input is worth
    # flagging. Matched case-insensitively against the name portion of the URN,
    # since every organisation spells its classification scheme differently.
    sensitive_tags: tuple[str, ...] = DEFAULT_SENSITIVE_TAGS

    # Escape hatch for teams who want statistics to be able to block. Off by
    # default: a false BLOCK is the one bug that gets a gate deleted.
    allow_probable_block: bool = False

    # How deep to walk upstream from a feature's source dataset.
    max_hops: int = 5

    def rule_for(self, kind: FindingKind) -> Rule:
        severity = self.rules.get(kind.value, _DEFAULT_SEVERITIES[kind])
        return Rule(id=_rule_id(kind), kind=kind, severity=severity)

    def active_exemption_for(self, finding: Finding, now: datetime) -> Exemption | None:
        for exemption in self.exemptions:
            if exemption.is_active(now) and exemption.matches(finding):
                return exemption
        return None

    @classmethod
    def default(cls) -> Policy:
        return cls()

    @classmethod
    def load(cls, path: Path | str | None) -> Policy:
        """Load a policy file, or return defaults if it is absent.

        A missing file is fine — Undertow works out of the box. A *malformed*
        file is not: silently falling back to defaults would mean a team's
        BLOCK rules quietly stop applying.
        """
        if path is None:
            return cls.default()

        p = Path(path)
        if not p.exists():
            return cls.default()

        raw: Any = yaml.safe_load(p.read_text(encoding="utf-8"))
        if raw is None:
            return cls.default()
        if not isinstance(raw, dict):
            raise ValueError(f"{p}: expected a YAML mapping, got {type(raw).__name__}")

        return cls.model_validate(raw)


def _rule_id(kind: FindingKind) -> str:
    """Stable, greppable rule identifier, e.g. COLUMN_DROPPED -> column-dropped."""
    return kind.value.lower().replace("_", "-")


__all__ = ["Policy", "Rule", "Exemption", "Thresholds", "DEFAULT_SENSITIVE_TAGS"]
