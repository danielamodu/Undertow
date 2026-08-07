"""The policy engine: `list[Finding] -> Verdict`.

This is a pure function. No I/O, no network, no LLM. Feed it findings and a
policy, get a deterministic verdict — which is what lets the gate be tested and
what stops a clever demo input from arguing its way past a BLOCK.

Two invariants are enforced here rather than left to convention:

1. A PROBABLE finding cannot BLOCK unless the policy explicitly opts in via
   `allow_probable_block`. Statistics warn; facts block.
2. An exemption must carry a reason and an expiry. Permanent silent exemptions
   are how a gate rots into decoration, so an expired one simply stops applying.
"""

from __future__ import annotations

from datetime import datetime, timezone

from undertow.models import (
    Confidence,
    Finding,
    FindingKind,
    RuledFinding,
    Severity,
    Verdict,
)
from undertow.policy import Policy, Rule


class PolicyViolation(RuntimeError):
    """Raised when a policy is internally inconsistent. Fail loudly at load time."""


def evaluate(
    findings: list[Finding],
    policy: Policy,
    *,
    model_urn: str,
    assets_checked: int = 0,
    baseline_ref: str | None = None,
    now: datetime | None = None,
) -> Verdict:
    """Judge findings against a policy.

    `now` is injected so exemption-expiry behaviour is testable without
    freezing the clock globally.
    """
    now = now or datetime.now(timezone.utc)

    ruled = [_rule_one(f, policy, now) for f in findings]

    # A verdict is as bad as its worst finding. Empty means CLEAR.
    severity = Severity.CLEAR
    for rf in ruled:
        severity = severity.escalate(rf.severity)

    # Stable, human-useful ordering: worst first, then certain before probable
    # (a dropped column outranks a drift number), then by kind for determinism.
    ruled.sort(
        key=lambda rf: (
            -rf.severity.rank,
            0 if rf.finding.confidence is Confidence.CERTAIN else 1,
            rf.finding.kind.value,
            rf.finding.subject_urn,
            rf.finding.subject_column or "",
        )
    )

    return Verdict(
        model_urn=model_urn,
        severity=severity,
        ruled_findings=tuple(ruled),
        assets_checked=assets_checked,
        baseline_ref=baseline_ref,
        checked_at=now,
    )


def _rule_one(finding: Finding, policy: Policy, now: datetime) -> RuledFinding:
    rule = policy.rule_for(finding.kind)
    severity = rule.severity

    # Invariant 1: statistics warn, facts block.
    if (
        severity is Severity.BLOCK
        and finding.confidence is Confidence.PROBABLE
        and not policy.allow_probable_block
    ):
        severity = Severity.WARN

    exemption = policy.active_exemption_for(finding, now)
    if exemption is not None:
        # Downgrade rather than drop. A silenced finding still appears in the
        # report, with the reason and expiry visible, so accepted risk stays
        # legible instead of vanishing.
        return RuledFinding(
            finding=finding,
            severity=exemption.downgrade_to,
            rule_id=rule.id,
            exempted_by=exemption.reason,
            exemption_expires=exemption.expires,
        )

    return RuledFinding(finding=finding, severity=severity, rule_id=rule.id)


def explain(verdict: Verdict) -> str:
    """One-line rationale. Used by the template narrator when no LLM key is set."""
    if verdict.severity is Severity.CLEAR:
        return (
            f"No material change across {verdict.assets_checked} upstream assets."
        )

    blocking = verdict.blocking
    if blocking:
        lead = blocking[0].finding
        where = lead.subject_column or lead.subject_urn
        return (
            f"Deploy blocked: {lead.kind.value.lower().replace('_', ' ')} on {where}"
            + (f" and {len(blocking) - 1} other blocking change(s)." if len(blocking) > 1 else ".")
        )

    warned = verdict.warnings
    lead = warned[0].finding
    where = lead.subject_column or lead.subject_urn
    return (
        f"Proceed with caution: {lead.kind.value.lower().replace('_', ' ')} on {where}"
        + (f" and {len(warned) - 1} other warning(s)." if len(warned) > 1 else ".")
    )


def validate_policy(policy: Policy) -> None:
    """Fail fast on a policy that would behave surprisingly at runtime."""
    unknown = set(policy.rules) - {k.value for k in FindingKind}
    if unknown:
        raise PolicyViolation(
            f"Policy references unknown finding kinds: {sorted(unknown)}. "
            f"Valid kinds: {sorted(k.value for k in FindingKind)}"
        )

    for exemption in policy.exemptions:
        if exemption.expires is None:
            raise PolicyViolation(
                f"Exemption {exemption.reason!r} has no expiry. Every exemption must "
                "expire — permanent silent exemptions are how a gate rots."
            )
        if exemption.downgrade_to is Severity.BLOCK:
            raise PolicyViolation(
                f"Exemption {exemption.reason!r} downgrades to BLOCK, which is not a "
                "downgrade. Use WARN or CLEAR."
            )


__all__ = ["evaluate", "explain", "validate_policy", "PolicyViolation", "Rule"]
