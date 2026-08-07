"""Statistical differ — deliberately limited to what DataHub profiles by default.

Every finding here is `PROBABLE`, and the policy engine refuses to let a
PROBABLE finding BLOCK unless a team explicitly opts in. That is not timidity:
a distribution moving is evidence that something *may* be wrong, and stopping a
deploy on evidence that weak is how a gate loses its welcome.

## Why there is no PSI here

An earlier revision computed Population Stability Index from quantiles and
histograms. It was the largest module in the project and it could not fire on a
stock DataHub.

Verified in `ge_profiling_config.py`: profiling itself is
`enabled: bool = Field(default=False)`, and `include_field_quantiles`,
`include_field_histogram`, and `include_field_distinct_value_frequencies` are
each `default=False` on top of that. Even with every flag on,
`ge_data_profiler.py` gates quantiles and histograms behind numeric type AND
`Cardinality in {FEW, MANY, VERY_MANY}` — so string and datetime columns never
get them, and unique columns don't either.

So PSI worked beautifully on our own fixture and returned nothing on anyone
else's DataHub, in exchange for roughly 250 lines of bucketing and CDF
interpolation. That is a bad trade for a signal that can only ever WARN.

What remains runs on `nullCount`, `uniqueCount`, `mean`, `stdev`, `min`, `max`,
and `rowCount` — all `default=True`, all available on a stock instance.

## The rule that governs the whole module

**A missing statistic means "cannot assess", never "no drift."** Silence is not
evidence of stability. Absent values produce no finding *and* no reassurance —
`profile_coverage` exists so a CLEAR verdict can say honestly how much of the
footprint it was actually able to look at.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from undertow.differ._shared import as_float, relative_change, short_urn
from undertow.models import (
    AssetSnapshot,
    FieldProfileSnapshot,
    Finding,
    FindingKind,
    UndertowSnapshot,
)
from undertow.policy import Thresholds


@dataclass(frozen=True)
class ProfileCoverage:
    """How much of the footprint the statistical differ could actually assess.

    This exists so a CLEAR verdict can be honest. "No drift found" across 8
    datasets where 6 were unprofiled is not the same statement as "no drift
    found" across 8 that were fully profiled, and a gate that presents them
    identically is quietly misleading.
    """

    columns_total: int = 0
    columns_compared: int = 0
    assets_total: int = 0
    assets_profiled: int = 0

    @property
    def ratio(self) -> float:
        """Fraction of shared columns with comparable statistics on both sides."""
        return self.columns_compared / self.columns_total if self.columns_total else 0.0

    @property
    def is_blind(self) -> bool:
        """True when nothing could be assessed at all — worth saying out loud."""
        return self.columns_compared == 0

    def summary(self) -> str:
        if not self.columns_total:
            return "No overlapping columns to compare."
        if self.is_blind:
            return (
                f"No profiled statistics on either side of {self.assets_total} asset(s) — "
                "statistical checks could not run. Enable profiling in your ingestion "
                "recipe to activate them."
            )
        return (
            f"Compared statistics on {self.columns_compared}/{self.columns_total} columns "
            f"across {self.assets_profiled}/{self.assets_total} assets."
        )


def diff_statistics(
    baseline: UndertowSnapshot,
    current: UndertowSnapshot,
    *,
    thresholds: Thresholds | None = None,
) -> list[Finding]:
    """Compare profiled statistics across every asset present in both snapshots."""
    limits = thresholds or Thresholds()
    findings: list[Finding] = []
    for urn in baseline.shared_urns(current):
        before, after = baseline.assets[urn], current.assets[urn]
        findings.extend(_diff_asset(before, after, limits))
    return findings


def profile_coverage(baseline: UndertowSnapshot, current: UndertowSnapshot) -> ProfileCoverage:
    """Measure what `diff_statistics` was able to look at. Pure; no findings."""
    columns_total = columns_compared = 0
    assets_total = assets_profiled = 0

    for urn in baseline.shared_urns(current):
        before, after = baseline.assets[urn], current.assets[urn]
        assets_total += 1
        if before.profile is not None and after.profile is not None:
            assets_profiled += 1

        shared = {c.path for c in before.columns} & {c.path for c in after.columns}
        columns_total += len(shared)
        for path in shared:
            old, new = before.field_profile(path), after.field_profile(path)
            if old is None or new is None:
                continue
            if _has_comparable(old, new):
                columns_compared += 1

    return ProfileCoverage(
        columns_total=columns_total,
        columns_compared=columns_compared,
        assets_total=assets_total,
        assets_profiled=assets_profiled,
    )


def _diff_asset(
    before: AssetSnapshot, after: AssetSnapshot, limits: Thresholds
) -> list[Finding]:
    findings: list[Finding] = []

    if before.profile is None or after.profile is None:
        # Cannot assess. Deliberately silent — see the module docstring.
        return findings

    volume = _row_count_change(before, after, limits)
    if volume is not None:
        findings.append(volume)

    shared = sorted({c.path for c in before.columns} & {c.path for c in after.columns})
    for path in shared:
        old, new = before.field_profile(path), after.field_profile(path)
        if old is None or new is None:
            continue
        findings.extend(
            _diff_column(
                after,
                old,
                new,
                limits,
                old_rows=before.profile.row_count,
                new_rows=after.profile.row_count,
            )
        )

    return findings


def _diff_column(
    asset: AssetSnapshot,
    old: FieldProfileSnapshot,
    new: FieldProfileSnapshot,
    limits: Thresholds,
    *,
    old_rows: int | None = None,
    new_rows: int | None = None,
) -> list[Finding]:
    """One column, against statistics DataHub profiles by default."""
    findings: list[Finding] = []

    null_jump = _null_rate_jump(asset, old, new, limits, old_rows=old_rows, new_rows=new_rows)
    if null_jump is not None:
        findings.append(null_jump)

    cardinality = _cardinality_change(asset, old, new, limits)
    if cardinality is not None:
        findings.append(cardinality)

    mean_shift = _mean_shift(asset, old, new, limits)
    if mean_shift is not None:
        findings.append(mean_shift)

    range_violation = _range_violation(asset, old, new)
    if range_violation is not None:
        findings.append(range_violation)

    return findings


# ---------------------------------------------------------------------------
# Tier 1 — available from DataHub's default profiling
# ---------------------------------------------------------------------------


def _null_rate_jump(
    asset: AssetSnapshot,
    old: FieldProfileSnapshot,
    new: FieldProfileSnapshot,
    limits: Thresholds,
    *,
    old_rows: int | None = None,
    new_rows: int | None = None,
) -> Finding | None:
    """Null proportion rising by more than `null_rate_jump_pp` percentage points.

    Points, not percent: 1% -> 2% is a doubling but barely matters, while
    2% -> 30% is the same relative jump and means an upstream join broke. Only
    increases are reported — nulls disappearing is not a risk to a deploy.
    """
    old_rate, new_rate = _null_rate(old, old_rows), _null_rate(new, new_rows)
    if old_rate is None or new_rate is None:
        return None

    jump_pp = (new_rate - old_rate) * 100.0
    if jump_pp <= limits.null_rate_jump_pp:
        return None

    return _finding(
        FindingKind.NULL_RATE_JUMP,
        asset,
        old.path,
        summary=(
            f"Null rate on `{old.path}` rose {old_rate:.1%} → {new_rate:.1%} "
            f"({jump_pp:+.1f}pp) in {short_urn(asset.urn)}"
        ),
        evidence={
            "old_null_proportion": round(old_rate, 6),
            "new_null_proportion": round(new_rate, 6),
            "jump_pp": round(jump_pp, 3),
            "threshold_pp": limits.null_rate_jump_pp,
            "old_null_count": old.null_count,
            "new_null_count": new.null_count,
        },
    )


def _null_rate(profile: FieldProfileSnapshot, row_count: int | None = None) -> float | None:
    """Null rate as a fraction, from the stored proportion or derived from counts.

    `nullProportion` is preferred because DataHub computes it directly. The
    fallback matters more than it looks: some ingestion sources populate
    `nullCount` and leave `nullProportion` empty, and the denominator is only
    available from the *dataset* profile — `rowCount` is not on the field
    profile. Without the row count a bare `nullCount` is not comparable across a
    volume change, so it is dropped rather than guessed at.
    """
    if profile.null_proportion is not None:
        return profile.null_proportion
    if profile.null_count is not None and row_count:
        return profile.null_count / row_count
    return None


def _cardinality_change(
    asset: AssetSnapshot,
    old: FieldProfileSnapshot,
    new: FieldProfileSnapshot,
    limits: Thresholds,
) -> Finding | None:
    """Distinct-value count moving by more than `cardinality_change_pct`.

    Both directions matter, and they mean opposite things. A collapse says an
    upstream category got remapped or a join started dropping rows; an explosion
    says an ID leaked into a categorical column. Either invalidates a model
    trained on the old alphabet.
    """
    old_count, new_count = old.unique_count, new.unique_count
    if old_count is None or new_count is None:
        return None

    change = relative_change(float(old_count), float(new_count))
    if change is None:
        # Baseline of zero unique values — an empty or all-null column. There is
        # no meaningful ratio, and the null-rate check already covers the case.
        return None

    change_pct = change * 100.0
    if abs(change_pct) <= limits.cardinality_change_pct:
        return None

    direction = "collapsed" if change_pct < 0 else "exploded"
    return _finding(
        FindingKind.CARDINALITY_CHANGE,
        asset,
        old.path,
        summary=(
            f"Cardinality of `{old.path}` {direction} {old_count:,} → {new_count:,} "
            f"({change_pct:+.0f}%) in {short_urn(asset.urn)}"
        ),
        evidence={
            "old_unique_count": old_count,
            "new_unique_count": new_count,
            "change_pct": round(change_pct, 2),
            "threshold_pct": limits.cardinality_change_pct,
            "direction": direction,
        },
    )


def _mean_shift(
    asset: AssetSnapshot,
    old: FieldProfileSnapshot,
    new: FieldProfileSnapshot,
    limits: Thresholds,
) -> Finding | None:
    """Mean moving by more than `mean_shift_sigma` baseline standard deviations.

    Measured in baseline sigma so the threshold is scale-free: the same rule
    works on a column of cents and a column of probabilities without tuning.
    """
    old_mean, new_mean = as_float(old.mean), as_float(new.mean)
    old_stdev = as_float(old.stdev)
    if old_mean is None or new_mean is None or old_stdev is None:
        return None

    delta = new_mean - old_mean

    if old_stdev <= 0:
        # A constant column. Any movement at all is a real change, but there is
        # no sigma to express it in, so report only on actual movement and say
        # so in the evidence rather than dividing by zero.
        if delta == 0:
            return None
        sigma = math.inf
        magnitude = "from a previously constant value"
    else:
        sigma = delta / old_stdev
        if abs(sigma) <= limits.mean_shift_sigma:
            return None
        magnitude = f"{sigma:+.1f}σ"

    return _finding(
        FindingKind.MEAN_SHIFT,
        asset,
        old.path,
        summary=(
            f"Mean of `{old.path}` moved {old_mean:g} → {new_mean:g} ({magnitude}) "
            f"in {short_urn(asset.urn)}"
        ),
        evidence={
            "old_mean": old_mean,
            "new_mean": new_mean,
            "old_stdev": old_stdev,
            "new_stdev": as_float(new.stdev),
            "sigma": None if math.isinf(sigma) else round(sigma, 3),
            "threshold_sigma": limits.mean_shift_sigma,
        },
    )


def _range_violation(
    asset: AssetSnapshot, old: FieldProfileSnapshot, new: FieldProfileSnapshot
) -> Finding | None:
    """New min or max outside the baseline envelope.

    No threshold: leaving the observed range at all is the signal. A negative
    value in a column that has only ever held positives is not a 4% change, it
    is a different column. Non-numeric mins and maxes parse to `None` and are
    skipped — lexicographic bounds on a string column are not evidence of drift.
    """
    old_min, old_max = as_float(old.min), as_float(old.max)
    new_min, new_max = as_float(new.min), as_float(new.max)
    if old_min is None or old_max is None:
        return None

    breaches: list[str] = []
    evidence: dict[str, str | float | int | bool | None] = {
        "baseline_min": old_min,
        "baseline_max": old_max,
        "new_min": new_min,
        "new_max": new_max,
    }

    if new_min is not None and new_min < old_min:
        breaches.append(f"min {old_min:g} → {new_min:g}")
        evidence["min_breach"] = round(old_min - new_min, 6)
    if new_max is not None and new_max > old_max:
        breaches.append(f"max {old_max:g} → {new_max:g}")
        evidence["max_breach"] = round(new_max - old_max, 6)

    if not breaches:
        return None

    return _finding(
        FindingKind.RANGE_VIOLATION,
        asset,
        old.path,
        summary=(
            f"`{old.path}` left its baseline range in {short_urn(asset.urn)}: "
            + ", ".join(breaches)
        ),
        evidence=evidence,
    )


def _row_count_change(
    before: AssetSnapshot, after: AssetSnapshot, limits: Thresholds
) -> Finding | None:
    """Volume anomaly, from `rowCount` on the dataset profile.

    Asset-level, not column-level — `rowCount` lives on `DatasetProfile`, and
    `tests/test_sdk_assumptions.py` pins that asymmetry so an SDK bump cannot
    move it silently.
    """
    assert before.profile is not None and after.profile is not None
    old_rows, new_rows = before.profile.row_count, after.profile.row_count
    if old_rows is None or new_rows is None:
        return None

    change = relative_change(float(old_rows), float(new_rows))
    if change is None:
        # Baseline had zero rows. A table filling up is not an anomaly worth a
        # finding, and there is no ratio to report.
        return None

    change_pct = change * 100.0
    if abs(change_pct) <= limits.row_count_change_pct:
        return None

    direction = "dropped" if change_pct < 0 else "grew"
    return _finding(
        FindingKind.ROW_COUNT_CHANGE,
        after,
        None,
        summary=(
            f"Row count of {short_urn(after.urn)} {direction} {old_rows:,} → "
            f"{new_rows:,} ({change_pct:+.0f}%)"
        ),
        evidence={
            "old_row_count": old_rows,
            "new_row_count": new_rows,
            "change_pct": round(change_pct, 2),
            "threshold_pct": limits.row_count_change_pct,
            "direction": direction,
        },
    )


def _has_comparable(old: FieldProfileSnapshot, new: FieldProfileSnapshot) -> bool:
    """Is there at least one comparable statistic present on both sides?"""
    return any(
        getattr(old, attr) is not None and getattr(new, attr) is not None
        for attr in ("null_proportion", "unique_count", "mean", "min", "max")
    )


def _finding(
    kind: FindingKind,
    asset: AssetSnapshot,
    column: str | None,
    *,
    summary: str,
    evidence: dict[str, str | float | int | bool | None],
) -> Finding:
    features = asset.features_for(column)
    merged: dict[str, str | float | int | bool | None] = {
        "on_feature_path": bool(features),
        "features": ", ".join(sorted(features)) or None,
    }
    if column is not None:
        merged["column"] = column
    merged.update(evidence)

    return Finding(
        kind=kind,
        subject_urn=asset.urn,
        subject_column=column,
        affected_feature_urn=asset.primary_feature(column),
        summary=summary,
        evidence=merged,
    )


__all__ = ["ProfileCoverage", "diff_statistics", "profile_coverage"]
