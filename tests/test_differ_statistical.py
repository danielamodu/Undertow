"""Tests for the statistical differ.

Two things are being pinned here, and the second matters more than the first.

1. That each tier-1 signal fires at its threshold and not before.
2. **That an absent statistic produces no finding *and* no reassurance.** The
   rule the whole module is built on is that silence is not evidence of
   stability, and it is the rule a well-meaning refactor is most likely to
   break — a `or 0.0` in the wrong place turns "we could not look" into "we
   looked and it was fine".

Everything is a plain object. No DataHub, no network.
"""

from __future__ import annotations

import math

import pytest

from undertow.differ.statistical import (
    TIER_1,
    TIER_2,
    diff_statistics,
    profile_coverage,
    psi,
)
from undertow.models import (
    AssetSnapshot,
    ColumnSnapshot,
    Confidence,
    FieldProfileSnapshot,
    Finding,
    FindingKind,
    HistogramSnapshot,
    ProfileSnapshot,
    QuantileSnapshot,
    UndertowSnapshot,
    ValueFrequencySnapshot,
)
from undertow.policy import Thresholds

MODEL = "urn:li:mlModel:(urn:li:dataPlatform:mlflow,fraud_detector_v3,PROD)"
PAYMENTS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,raw.payments,PROD)"
AVG_TXN = "urn:li:mlFeature:(txn_aggregates,avg_txn_30d)"

COLUMN = "merchant_risk_score"


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def profile(path: str = COLUMN, **stats: object) -> FieldProfileSnapshot:
    """A field profile with only the statistics a test names.

    Defaults are absent rather than zero, on purpose: that is what DataHub
    actually gives you, and a builder that filled them in would hide exactly the
    bug these tests exist to catch.
    """
    return FieldProfileSnapshot(path=path, **stats)  # type: ignore[arg-type]


def asset(
    *profiles: FieldProfileSnapshot,
    rows: int | None = 1_000,
    urn: str = PAYMENTS,
    features: tuple[str, ...] = (AVG_TXN,),
    profiled: bool = True,
) -> AssetSnapshot:
    return AssetSnapshot(
        urn=urn,
        columns=tuple(ColumnSnapshot(path=p.path) for p in profiles),
        profile=ProfileSnapshot(row_count=rows, fields=profiles) if profiled else None,
        feeds_features=features,
    )


def snap(*assets: AssetSnapshot) -> UndertowSnapshot:
    return UndertowSnapshot(model_urn=MODEL, assets={a.urn: a for a in assets})


def quantiles(*pairs: tuple[str, float]) -> tuple[QuantileSnapshot, ...]:
    """DataHub stores both members as strings; the differ has to parse them."""
    return tuple(QuantileSnapshot(quantile=q, value=str(v)) for q, v in pairs)


def frequencies(**counts: int) -> tuple[ValueFrequencySnapshot, ...]:
    return tuple(ValueFrequencySnapshot(value=k, frequency=v) for k, v in counts.items())


def diff(
    before: AssetSnapshot, after: AssetSnapshot, *, thresholds: Thresholds | None = None
) -> list[Finding]:
    return diff_statistics(snap(before), snap(after), thresholds=thresholds or Thresholds())


# --------------------------------------------------------------------------
# Cannot assess ≠ no drift
# --------------------------------------------------------------------------


def test_no_profile_on_either_side_produces_no_findings() -> None:
    before = asset(profile(mean="100", stdev="10"), profiled=False)
    after = asset(profile(mean="900", stdev="10"), profiled=False)

    assert diff(before, after) == []


def test_missing_current_profile_is_silent_not_clear() -> None:
    before = asset(profile(mean="100", stdev="10"))
    after = asset(profile(mean="100", stdev="10"), profiled=False)

    assert diff(before, after) == []


def test_coverage_reports_what_could_not_be_assessed() -> None:
    before = asset(profile(mean="100", stdev="10"), profile("other"))
    after = asset(profile(mean="100", stdev="10"), profile("other"))

    coverage = profile_coverage(snap(before), snap(after))

    assert coverage.columns_total == 2
    assert coverage.columns_compared == 1  # `other` has no statistics at all
    assert coverage.assets_profiled == 1
    assert not coverage.is_blind
    assert "1/2 columns" in coverage.summary()


def test_coverage_says_so_when_it_is_completely_blind() -> None:
    before = asset(profile(), profiled=False)
    after = asset(profile(), profiled=False)

    coverage = profile_coverage(snap(before), snap(after))

    assert coverage.is_blind
    assert coverage.ratio == 0.0
    assert "could not run" in coverage.summary()


def test_coverage_counts_tier_2_columns_separately() -> None:
    points = quantiles(("0.25", 10), ("0.5", 20), ("0.75", 30))
    before = asset(profile(mean="20", quantiles=points))
    after = asset(profile(mean="20", quantiles=points))

    coverage = profile_coverage(snap(before), snap(after))

    assert coverage.columns_tier_2 == 1
    assert "tier 2" in coverage.summary()


# --------------------------------------------------------------------------
# Tier 1 — null rate
# --------------------------------------------------------------------------


def test_null_rate_jump_above_threshold_is_reported() -> None:
    before = asset(profile(null_proportion=0.01))
    after = asset(profile(null_proportion=0.42))

    findings = diff(before, after)

    assert len(findings) == 1
    assert findings[0].kind is FindingKind.NULL_RATE_JUMP
    assert findings[0].evidence["jump_pp"] == pytest.approx(41.0)
    assert findings[0].evidence["tier"] == TIER_1


def test_null_rate_jump_below_threshold_is_silent() -> None:
    before = asset(profile(null_proportion=0.01))
    after = asset(profile(null_proportion=0.09))

    assert diff(before, after) == []


def test_null_rate_falling_is_not_a_finding() -> None:
    # Nulls disappearing is not a risk to a deploy.
    before = asset(profile(null_proportion=0.50))
    after = asset(profile(null_proportion=0.01))

    assert diff(before, after) == []


def test_null_rate_is_derived_from_counts_when_the_proportion_is_absent() -> None:
    # Some sources populate nullCount and leave nullProportion empty. The
    # denominator only exists on the dataset profile.
    before = asset(profile(null_count=10), rows=1_000)
    after = asset(profile(null_count=500), rows=1_000)

    findings = diff(before, after)

    assert len(findings) == 1
    assert findings[0].kind is FindingKind.NULL_RATE_JUMP


def test_null_count_without_a_row_count_is_not_guessed_at() -> None:
    before = asset(profile(null_count=10), rows=None)
    after = asset(profile(null_count=500), rows=None)

    assert diff(before, after) == []


def test_null_rate_threshold_is_configurable() -> None:
    before = asset(profile(null_proportion=0.01))
    after = asset(profile(null_proportion=0.09))

    findings = diff(before, after, thresholds=Thresholds(null_rate_jump_pp=5.0))

    assert len(findings) == 1


# --------------------------------------------------------------------------
# Tier 1 — cardinality, mean, range, volume
# --------------------------------------------------------------------------


def test_cardinality_collapse_is_reported() -> None:
    before = asset(profile(unique_count=1_000))
    after = asset(profile(unique_count=12))

    findings = diff(before, after)

    assert findings[0].kind is FindingKind.CARDINALITY_CHANGE
    assert findings[0].evidence["direction"] == "collapsed"


def test_cardinality_explosion_is_reported() -> None:
    before = asset(profile(unique_count=12))
    after = asset(profile(unique_count=1_000))

    assert diff(before, after)[0].evidence["direction"] == "exploded"


def test_cardinality_within_threshold_is_silent() -> None:
    before = asset(profile(unique_count=100))
    after = asset(profile(unique_count=140))

    assert diff(before, after) == []


def test_cardinality_from_a_zero_baseline_is_not_a_ratio() -> None:
    # An all-null column gaining values has no meaningful percentage change.
    before = asset(profile(unique_count=0))
    after = asset(profile(unique_count=50))

    assert diff(before, after) == []


def test_mean_shift_beyond_three_sigma_is_reported() -> None:
    before = asset(profile(mean="500", stdev="10"))
    after = asset(profile(mean="560", stdev="10"))

    findings = diff(before, after)

    assert findings[0].kind is FindingKind.MEAN_SHIFT
    assert findings[0].evidence["sigma"] == pytest.approx(6.0)


def test_mean_shift_within_three_sigma_is_silent() -> None:
    before = asset(profile(mean="500", stdev="10"))
    after = asset(profile(mean="520", stdev="10"))

    assert diff(before, after) == []


def test_mean_shift_needs_a_baseline_stdev() -> None:
    before = asset(profile(mean="500"))
    after = asset(profile(mean="5000"))

    assert diff(before, after) == []


def test_constant_column_that_moves_is_reported_without_a_sigma() -> None:
    # Zero variance means there is no sigma to divide by, but the movement is
    # still real. Reporting it with sigma=None beats reporting an infinity into
    # a JSON artifact.
    before = asset(profile(mean="1", stdev="0"))
    after = asset(profile(mean="7", stdev="0"))

    findings = diff(before, after)

    assert findings[0].kind is FindingKind.MEAN_SHIFT
    assert findings[0].evidence["sigma"] is None
    assert "constant" in findings[0].summary


def test_non_numeric_statistics_are_skipped_not_crashed_on() -> None:
    # min/max/mean are typed `string` in the PDL precisely so a text column can
    # be described. Parsing has to fail softly.
    before = asset(profile(mean="alpha", stdev="beta", min="aaa", max="zzz"))
    after = asset(profile(mean="omega", stdev="gamma", min="000", max="999"))

    assert diff(before, after) == []


def test_new_minimum_outside_the_baseline_envelope_is_reported() -> None:
    before = asset(profile(min="0", max="1000"))
    after = asset(profile(min="-45", max="1000"))

    findings = diff(before, after)

    assert findings[0].kind is FindingKind.RANGE_VIOLATION
    assert findings[0].evidence["min_breach"] == pytest.approx(45.0)
    assert "max_breach" not in findings[0].evidence


def test_range_inside_the_baseline_envelope_is_silent() -> None:
    before = asset(profile(min="0", max="1000"))
    after = asset(profile(min="10", max="900"))

    assert diff(before, after) == []


def test_row_count_change_is_asset_level_not_column_level() -> None:
    before = asset(profile(), rows=1_000_000)
    after = asset(profile(), rows=100_000)

    findings = diff(before, after)

    assert len(findings) == 1
    assert findings[0].kind is FindingKind.ROW_COUNT_CHANGE
    assert findings[0].subject_column is None
    assert findings[0].evidence["direction"] == "dropped"


def test_row_count_within_threshold_is_silent() -> None:
    before = asset(profile(), rows=1_000)
    after = asset(profile(), rows=1_200)

    assert diff(before, after) == []


def test_every_statistical_finding_is_probable() -> None:
    # The engine refuses to let a PROBABLE finding block. This is the property
    # that makes that guarantee reach the statistical differ at all.
    before = asset(profile(null_proportion=0.0, unique_count=1_000, mean="1", stdev="1"))
    after = asset(profile(null_proportion=0.9, unique_count=2, mean="99", stdev="1"))

    findings = diff(before, after)

    assert len(findings) >= 2
    assert all(f.confidence is Confidence.PROBABLE for f in findings)


# --------------------------------------------------------------------------
# Tier 2 — PSI
# --------------------------------------------------------------------------


def test_psi_of_an_identical_distribution_is_zero() -> None:
    assert psi([0.2, 0.3, 0.5], [0.2, 0.3, 0.5]) == pytest.approx(0.0)


def test_psi_normalises_counts_and_proportions_alike() -> None:
    assert psi([20, 30, 50], [0.2, 0.3, 0.5]) == pytest.approx(0.0)


def test_psi_grows_with_divergence() -> None:
    mild = psi([0.25, 0.25, 0.25, 0.25], [0.30, 0.25, 0.25, 0.20])
    severe = psi([0.25, 0.25, 0.25, 0.25], [0.70, 0.15, 0.10, 0.05])

    assert mild is not None and severe is not None
    assert 0 < mild < severe


def test_psi_matches_a_hand_computed_value() -> None:
    # Two buckets, 0.5/0.5 -> 0.6/0.4:
    #   (0.6-0.5)·ln(0.6/0.5) + (0.4-0.5)·ln(0.4/0.5)
    expected = 0.1 * math.log(1.2) + (-0.1) * math.log(0.8)

    assert psi([0.5, 0.5], [0.6, 0.4]) == pytest.approx(expected)


def test_psi_is_symmetric() -> None:
    forward = psi([0.2, 0.8], [0.5, 0.5])
    backward = psi([0.5, 0.5], [0.2, 0.8])

    assert forward is not None and backward is not None
    assert forward == pytest.approx(backward)


@pytest.mark.parametrize(
    ("baseline", "current"),
    [
        ([0.5, 0.5], [1.0]),        # mismatched lengths
        ([], []),                   # empty
        ([0.0, 0.0], [0.5, 0.5]),   # nothing in the baseline
        ([0.5, 0.5], [0.0, 0.0]),   # nothing in the current
        ([-1.0, 2.0], [0.5, 0.5]),  # negative mass
    ],
)
def test_psi_returns_none_when_it_cannot_measure(
    baseline: list[float], current: list[float]
) -> None:
    # None, not 0.0. Returning zero would be indistinguishable from "measured,
    # and stable" — the exact confusion this module is built to avoid.
    assert psi(baseline, current) is None


def test_identical_quantiles_produce_no_distribution_shift() -> None:
    points = quantiles(("0.05", 10), ("0.25", 25), ("0.5", 50), ("0.75", 75), ("0.95", 95))
    before = asset(profile(quantiles=points))
    after = asset(profile(quantiles=points))

    assert diff(before, after) == []


def test_shifted_quantiles_are_reported_as_tier_2() -> None:
    before = asset(
        profile(quantiles=quantiles(("0.05", 10), ("0.25", 25), ("0.5", 50),
                                    ("0.75", 75), ("0.95", 95)))
    )
    after = asset(
        profile(quantiles=quantiles(("0.05", 60), ("0.25", 70), ("0.5", 80),
                                    ("0.75", 90), ("0.95", 99)))
    )

    findings = diff(before, after)

    assert len(findings) == 1
    assert findings[0].kind is FindingKind.DISTRIBUTION_SHIFT
    assert findings[0].evidence["tier"] == TIER_2
    assert findings[0].evidence["source"] == "quantile"
    assert isinstance(findings[0].evidence["psi"], float)


def test_categorical_psi_uses_the_union_of_categories() -> None:
    # A category vanishing entirely is the strongest drift signal there is;
    # intersecting the two sides would delete exactly that evidence.
    before = asset(profile(distinct_value_frequencies=frequencies(uk=500, us=480, de=20)))
    after = asset(profile(distinct_value_frequencies=frequencies(uk=990, us=10)))

    findings = diff(before, after)

    assert findings[0].kind is FindingKind.DISTRIBUTION_SHIFT
    assert findings[0].evidence["source"] == "categorical"
    assert findings[0].evidence["buckets"] == 3


def test_histogram_is_preferred_over_quantiles_when_bins_align() -> None:
    bins = ("0", "50", "100")
    points = quantiles(("0.25", 10), ("0.5", 20), ("0.75", 30))
    before = asset(
        profile(quantiles=points, histogram=HistogramSnapshot(boundaries=bins,
                                                              heights=(0.9, 0.1)))
    )
    after = asset(
        profile(quantiles=points, histogram=HistogramSnapshot(boundaries=bins,
                                                              heights=(0.1, 0.9)))
    )

    findings = diff(before, after)

    assert findings[0].evidence["source"] == "histogram"


def test_misaligned_histogram_bins_fall_back_rather_than_compare_nonsense() -> None:
    # DataHub re-profiles independently each run, so bins genuinely move.
    # Comparing masses across different boundaries would be a confident lie.
    points = quantiles(("0.25", 10), ("0.5", 20), ("0.75", 30))
    before = asset(
        profile(quantiles=points,
                histogram=HistogramSnapshot(boundaries=("0", "50"), heights=(1.0,)))
    )
    after = asset(
        profile(quantiles=points,
                histogram=HistogramSnapshot(boundaries=("0", "60"), heights=(1.0,)))
    )

    assert diff(before, after) == []  # quantiles are identical, so nothing to report


def test_psi_threshold_is_configurable() -> None:
    before = asset(
        profile(quantiles=quantiles(("0.25", 10), ("0.5", 20), ("0.75", 30)))
    )
    after = asset(
        profile(quantiles=quantiles(("0.25", 12), ("0.5", 22), ("0.75", 33)))
    )

    lenient = diff(before, after, thresholds=Thresholds(psi=0.9))
    strict = diff(before, after, thresholds=Thresholds(psi=0.0001))

    assert lenient == []
    assert len(strict) == 1


def test_tier_2_suppresses_the_coarser_tier_1_signals() -> None:
    # PSI already describes the shift. Emitting mean-shift and range-violation
    # alongside it would report one event three times.
    before = asset(
        profile(mean="50", stdev="1", min="10", max="95",
                quantiles=quantiles(("0.05", 10), ("0.25", 25), ("0.5", 50),
                                    ("0.75", 75), ("0.95", 95)))
    )
    after = asset(
        profile(mean="80", stdev="1", min="5", max="200",
                quantiles=quantiles(("0.05", 60), ("0.25", 70), ("0.5", 80),
                                    ("0.75", 90), ("0.95", 99)))
    )

    kinds = [f.kind for f in diff(before, after)]

    assert kinds == [FindingKind.DISTRIBUTION_SHIFT]


def test_null_rate_still_reports_alongside_tier_2() -> None:
    # Nulls are not a distribution shift in the profiled values — PSI is
    # computed over non-null data — so this one is not suppressed.
    points_before = quantiles(("0.25", 10), ("0.5", 20), ("0.75", 30))
    points_after = quantiles(("0.25", 90), ("0.5", 95), ("0.75", 99))
    before = asset(profile(null_proportion=0.0, quantiles=points_before))
    after = asset(profile(null_proportion=0.5, quantiles=points_after))

    kinds = {f.kind for f in diff(before, after)}

    assert kinds == {FindingKind.NULL_RATE_JUMP, FindingKind.DISTRIBUTION_SHIFT}


def test_too_few_quantile_points_falls_back_to_tier_1() -> None:
    # Two cut points give no interior structure; mean-shift describes it better.
    before = asset(profile(mean="10", stdev="1", quantiles=quantiles(("0.5", 10))))
    after = asset(profile(mean="90", stdev="1", quantiles=quantiles(("0.5", 90))))

    findings = diff(before, after)

    assert [f.kind for f in findings] == [FindingKind.MEAN_SHIFT]


def test_unparseable_quantile_values_do_not_crash_tier_2() -> None:
    before = asset(profile(quantiles=quantiles(("0.25", 10), ("0.5", 20), ("0.75", 30))))
    after = asset(
        profile(
            quantiles=(
                QuantileSnapshot(quantile="0.25", value="n/a"),
                QuantileSnapshot(quantile="0.5", value="n/a"),
                QuantileSnapshot(quantile="0.75", value="n/a"),
            )
        )
    )

    assert diff(before, after) == []


# --------------------------------------------------------------------------
# Report shape
# --------------------------------------------------------------------------


def test_findings_carry_the_feature_they_reach() -> None:
    before = asset(profile(null_proportion=0.0))
    after = asset(profile(null_proportion=0.9))

    finding = diff(before, after)[0]

    assert finding.affected_feature_urn == AVG_TXN
    assert finding.evidence["on_feature_path"] is True
    assert finding.subject_column == COLUMN
