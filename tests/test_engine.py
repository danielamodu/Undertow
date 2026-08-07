"""Tests for the policy engine.

These matter more than their line count suggests. The engine is what decides
whether a deploy is blocked, and a false BLOCK is the one bug that gets a gate
ripped out of a real team's pipeline. Everything here is pure — no DataHub, no
network, no fixtures beyond plain objects.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pydantic
import pytest

from undertow.engine import PolicyViolation, evaluate, explain, validate_policy
from undertow.models import (
    AttributionHop,
    AttributionPath,
    Confidence,
    Finding,
    FindingKind,
    Severity,
    Verdict,
)
from undertow.policy import Exemption, Policy

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
DATASET = "urn:li:dataset:(urn:li:dataPlatform:snowflake,raw.payments,PROD)"
MODEL = "urn:li:mlModel:(urn:li:dataPlatform:mlflow,fraud_detector_v3,PROD)"
FEATURE = "urn:li:mlFeature:(txn_aggregates,avg_txn_30d)"


def finding(
    kind: FindingKind = FindingKind.COLUMN_DROPPED,
    *,
    urn: str = DATASET,
    column: str | None = "amount_usd",
) -> Finding:
    return Finding(
        kind=kind,
        subject_urn=urn,
        subject_column=column,
        affected_feature_urn=FEATURE,
        summary=f"{kind.value} on {urn}.{column}",
    )


def verdict_for(findings: list[Finding], policy: Policy | None = None) -> Verdict:
    return evaluate(
        findings,
        policy or Policy.default(),
        model_urn=MODEL,
        assets_checked=8,
        now=NOW,
    )


# --------------------------------------------------------------------------
# Core severity behaviour
# --------------------------------------------------------------------------


def test_no_findings_is_clear_and_exits_zero() -> None:
    v = verdict_for([])
    assert v.severity is Severity.CLEAR
    assert v.exit_code() == 0
    assert v.ruled_findings == ()


def test_dropped_column_blocks_and_exits_one() -> None:
    v = verdict_for([finding(FindingKind.COLUMN_DROPPED)])
    assert v.severity is Severity.BLOCK
    assert v.exit_code() == 1
    assert len(v.blocking) == 1


def test_verdict_takes_the_worst_severity() -> None:
    v = verdict_for(
        [
            finding(FindingKind.MEAN_SHIFT),        # WARN
            finding(FindingKind.COLUMN_DROPPED),    # BLOCK
            finding(FindingKind.OWNERSHIP_LOST),    # WARN
        ]
    )
    assert v.severity is Severity.BLOCK
    assert len(v.blocking) == 1
    assert len(v.warnings) == 2


def test_exit_codes_separate_a_block_from_a_broken_gate() -> None:
    """1 means the gate said stop. 2 is reserved for "no verdict was produced".

    An earlier revision returned 2 for both, so a pipeline could not tell an
    upstream breakage from Undertow itself falling over — two outcomes that
    demand completely different responses.
    """
    assert verdict_for([]).exit_code() == 0
    assert verdict_for([finding(FindingKind.MEAN_SHIFT)]).exit_code() == 0
    assert verdict_for([finding(FindingKind.COLUMN_DROPPED)]).exit_code() == 1


def test_fail_on_warn_is_opt_in() -> None:
    """A warning annotates a deploy unless a team explicitly says otherwise."""
    warned = verdict_for([finding(FindingKind.MEAN_SHIFT)])

    assert warned.severity is Severity.WARN
    assert warned.exit_code() == 0
    assert warned.exit_code(fail_on_warn=True) == 1

    # The flag must never soften a BLOCK in either direction.
    blocked = verdict_for([finding(FindingKind.COLUMN_DROPPED)])
    assert blocked.exit_code(fail_on_warn=True) == 1
    assert blocked.exit_code(fail_on_warn=False) == 1


def test_warnings_alone_do_not_fail_ci() -> None:
    # The whole point of separating WARN from BLOCK: a drift signal annotates
    # the PR, it does not stop the deploy.
    v = verdict_for([finding(FindingKind.MEAN_SHIFT), finding(FindingKind.NULL_RATE_JUMP)])
    assert v.severity is Severity.WARN
    assert v.exit_code() == 0


# --------------------------------------------------------------------------
# Invariant 1: statistics warn, facts block
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        FindingKind.NULL_RATE_JUMP,
        FindingKind.CARDINALITY_CHANGE,
        FindingKind.MEAN_SHIFT,
        FindingKind.RANGE_VIOLATION,
        FindingKind.ROW_COUNT_CHANGE,
    ],
)
def test_statistical_kinds_are_probable(kind: FindingKind) -> None:
    assert kind.confidence is Confidence.PROBABLE


@pytest.mark.parametrize(
    "kind",
    [
        FindingKind.COLUMN_DROPPED,
        FindingKind.COLUMN_TYPE_CHANGED,
        FindingKind.COLUMN_NULLABILITY_RELAXED,
        FindingKind.ASSET_DEPRECATED,
        FindingKind.OWNERSHIP_LOST,
        FindingKind.NEW_SENSITIVE_TAG,
    ],
)
def test_graph_read_kinds_are_certain(kind: FindingKind) -> None:
    assert kind.confidence is Confidence.CERTAIN


def test_probable_finding_cannot_block_by_default() -> None:
    # Even when a policy file explicitly asks for it. This is the guard that
    # keeps a noisy PSI threshold from ever halting a deploy by accident.
    policy = Policy(rules={FindingKind.MEAN_SHIFT.value: Severity.BLOCK})
    v = verdict_for([finding(FindingKind.MEAN_SHIFT)], policy)
    assert v.severity is Severity.WARN
    assert v.exit_code() == 0


def test_probable_can_block_when_explicitly_opted_in() -> None:
    policy = Policy(
        rules={FindingKind.MEAN_SHIFT.value: Severity.BLOCK},
        allow_probable_block=True,
    )
    v = verdict_for([finding(FindingKind.MEAN_SHIFT)], policy)
    assert v.severity is Severity.BLOCK


def test_certain_finding_downgraded_by_policy_is_respected() -> None:
    # The invariant is one-directional: it stops PROBABLE escalating to BLOCK.
    # A team lowering a CERTAIN rule to WARN is a legitimate choice.
    policy = Policy(rules={FindingKind.COLUMN_DROPPED.value: Severity.WARN})
    v = verdict_for([finding(FindingKind.COLUMN_DROPPED)], policy)
    assert v.severity is Severity.WARN


# --------------------------------------------------------------------------
# Invariant 2: exemptions expire, and never hide
# --------------------------------------------------------------------------


def test_active_exemption_downgrades_but_keeps_the_finding_visible() -> None:
    policy = Policy(
        exemptions=(
            Exemption(
                reason="migrating amount_usd to amount_cents, tracked in DATA-1421",
                expires=NOW + timedelta(days=7),
                kind=FindingKind.COLUMN_DROPPED,
                column="amount_usd",
            ),
        )
    )
    v = verdict_for([finding(FindingKind.COLUMN_DROPPED)], policy)

    assert v.severity is Severity.WARN
    assert v.exit_code() == 0
    # Downgraded, not deleted — accepted risk stays legible in the report.
    assert len(v.ruled_findings) == 1
    assert v.ruled_findings[0].exempted_by is not None
    assert "DATA-1421" in v.ruled_findings[0].exempted_by


def test_expired_exemption_stops_applying() -> None:
    policy = Policy(
        exemptions=(
            Exemption(
                reason="expired last week",
                expires=NOW - timedelta(days=7),
                kind=FindingKind.COLUMN_DROPPED,
            ),
        )
    )
    v = verdict_for([finding(FindingKind.COLUMN_DROPPED)], policy)
    assert v.severity is Severity.BLOCK
    assert v.ruled_findings[0].exempted_by is None


def test_exemption_matches_on_urn_glob() -> None:
    policy = Policy(
        exemptions=(
            Exemption(
                reason="staging is allowed to churn",
                expires=NOW + timedelta(days=30),
                urn_pattern="*staging*",
            ),
        )
    )
    staging = finding(urn="urn:li:dataset:(urn:li:dataPlatform:snowflake,staging.x,PROD)")
    assert verdict_for([staging], policy).severity is Severity.WARN
    # The production dataset is untouched by the staging exemption.
    assert verdict_for([finding()], policy).severity is Severity.BLOCK


def test_exemption_without_expiry_is_rejected_at_load_time() -> None:
    policy = Policy(exemptions=(Exemption(reason="forever", expires=None),))
    with pytest.raises(PolicyViolation, match="must expire"):
        validate_policy(policy)


def test_exemption_cannot_upgrade_to_block() -> None:
    policy = Policy(
        exemptions=(
            Exemption(
                reason="not a downgrade",
                expires=NOW + timedelta(days=1),
                downgrade_to=Severity.BLOCK,
            ),
        )
    )
    with pytest.raises(PolicyViolation, match="not a downgrade"):
        validate_policy(policy)


def test_naive_expiry_is_normalised_to_utc() -> None:
    # Guards a real crash: comparing a naive expiry against an aware `now`
    # raises TypeError mid-run, which would surface as a failed deploy check.
    ex = Exemption(reason="naive", expires=datetime(2026, 12, 1, 0, 0))
    assert ex.expires is not None and ex.expires.tzinfo is not None
    assert ex.is_active(NOW) is True


def test_unknown_finding_kind_in_policy_is_rejected() -> None:
    policy = Policy(rules={"COLUMN_VANISHED": Severity.BLOCK})
    with pytest.raises(PolicyViolation, match="unknown finding kinds"):
        validate_policy(policy)


def test_default_policy_validates() -> None:
    validate_policy(Policy.default())


# --------------------------------------------------------------------------
# Ordering, reporting, and attribution
# --------------------------------------------------------------------------


def test_blocking_findings_sort_before_warnings() -> None:
    v = verdict_for(
        [
            finding(FindingKind.MEAN_SHIFT),
            finding(FindingKind.COLUMN_DROPPED),
            finding(FindingKind.NULL_RATE_JUMP),
        ]
    )
    assert v.ruled_findings[0].severity is Severity.BLOCK
    assert [rf.severity for rf in v.ruled_findings[1:]] == [Severity.WARN, Severity.WARN]


def test_certain_sorts_before_probable_at_equal_severity() -> None:
    policy = Policy(rules={FindingKind.MEAN_SHIFT.value: Severity.WARN})
    v = verdict_for(
        [finding(FindingKind.MEAN_SHIFT), finding(FindingKind.OWNERSHIP_LOST)], policy
    )
    assert v.ruled_findings[0].finding.confidence is Confidence.CERTAIN


def test_rule_id_is_stable_and_greppable() -> None:
    v = verdict_for([finding(FindingKind.COLUMN_DROPPED)])
    assert v.ruled_findings[0].rule_id == "column-dropped"


def test_headline_reports_counts() -> None:
    v = verdict_for([finding(FindingKind.COLUMN_DROPPED), finding(FindingKind.MEAN_SHIFT)])
    assert v.headline() == "BLOCK — 1 blocking, 1 warning"


def test_clear_headline_mentions_assets_checked() -> None:
    # A CLEAR verdict has to show its work, or it reads as "didn't run".
    assert "8 assets checked" in verdict_for([]).headline()


def test_explain_is_deterministic_without_an_llm() -> None:
    v = verdict_for([finding(FindingKind.COLUMN_DROPPED)])
    text = explain(v)
    assert "Deploy blocked" in text
    assert "amount_usd" in text


def test_attribution_path_depth_counts_edges_not_nodes() -> None:
    path = AttributionPath(
        hops=(
            AttributionHop(urn=DATASET, entity_type="dataset", column="amount_usd"),
            AttributionHop(urn=FEATURE, entity_type="mlFeature", via="DerivedFrom"),
            AttributionHop(urn=MODEL, entity_type="mlModel", via="Consumes"),
        ),
        owners=("@data-eng-tom",),
    )
    assert path.depth == 2
    assert path.root.column == "amount_usd"
    assert path.leaf.urn == MODEL
    assert path.root.label().endswith(".amount_usd")


def test_findings_are_immutable() -> None:
    # Frozen models stop a differ or narrator from mutating a verdict after the
    # engine has ruled on it. Name the exception: a bare `Exception` here would
    # also pass on a typo'd attribute, proving nothing.
    with pytest.raises(pydantic.ValidationError):
        finding().summary = "tampered"
