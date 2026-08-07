"""Differs — compare a footprint against the last approved deploy.

Three families, split by how much they can be trusted:

* `schema`     — set comparison over `schemaMetadata`.   CERTAIN.
* `governance` — deprecation, ownership, sensitive tags. CERTAIN.
* `statistical`— profiled statistics, two tiers.         PROBABLE.

Everything here is a pure function over `UndertowSnapshot` objects. No I/O, no
network, no DataHub import. That is what makes the blocking half of this system
testable without a running instance — and it is the reason the differs were
built before the resolver rather than after it.

**Differs observe; they do not judge.** Every function returns `Finding`
objects, which carry no severity. `undertow.engine.evaluate` assigns severity
from policy. A differ that decided its own severity would put the verdict
beyond the reach of a config file, which is the failure mode the deterministic
core exists to prevent.
"""

from __future__ import annotations

from undertow.differ.governance import diff_governance
from undertow.differ.schema import diff_schema, is_compatible_change
from undertow.differ.statistical import ProfileCoverage, diff_statistics, profile_coverage
from undertow.models import Finding, FindingKind, UndertowSnapshot
from undertow.policy import Policy


def diff_snapshots(
    baseline: UndertowSnapshot | None,
    current: UndertowSnapshot,
    policy: Policy | None = None,
) -> list[Finding]:
    """Run all three differs over baseline and current snapshots.

    If baseline is None, returns a finding indicating no baseline exists.
    """
    policy = policy or Policy.default()
    if baseline is None:
        return [
            Finding(
                kind=FindingKind.OWNERSHIP_LOST,
                subject_urn=current.model_urn,
                summary="No baseline found — run undertow baseline to capture one",
            )
        ]
    return [
        *diff_schema(baseline, current),
        *diff_governance(baseline, current, sensitive_tags=policy.sensitive_tags),
        *diff_statistics(baseline, current, thresholds=policy.thresholds),
    ]


diff_all = diff_snapshots

__all__ = [
    "ProfileCoverage",
    "diff_all",
    "diff_governance",
    "diff_schema",
    "diff_snapshots",
    "diff_statistics",
    "is_compatible_change",
    "profile_coverage",
]
