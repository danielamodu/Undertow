"""Cross-model incident correlation for `check --all`.

Sweeping an inventory gates each model independently, which is correct at the
level of a single deploy decision and misleading at the level of what an
on-call engineer actually needs on a bad morning: twenty models sharing one
broken upstream table read as twenty unrelated incidents when they are one.

Nothing here changes a verdict, a severity, or an exit code. This runs after
every model in a sweep has already been gated and rendered on its own — it
only re-groups verdicts that already exist by the root asset their blocking
or warning findings trace back to.
"""

from __future__ import annotations

from dataclasses import dataclass

from undertow.models import Severity, Verdict, short_urn

_SEVERITY_RANK = {Severity.CLEAR: 0, Severity.WARN: 1, Severity.BLOCK: 2}


@dataclass(frozen=True)
class Incident:
    """Two or more models blocked or warned by the same root-cause asset."""

    root_urn: str
    root_owners: tuple[str, ...]
    worst_severity: Severity
    # (model_urn, finding summary) — one entry per affected model. A model can
    # only contribute once per incident even if several of its findings trace
    # to the same root, since "how many models are affected" is the number
    # that matters here, not how many findings fired.
    affected: tuple[tuple[str, str], ...]

    @property
    def root_name(self) -> str:
        return short_urn(self.root_urn)

    @property
    def model_count(self) -> int:
        return len(self.affected)


def correlate(verdicts: list[Verdict]) -> list[Incident]:
    """Group ruled findings across a sweep by shared root cause.

    Only findings with an attribution path participate — a finding without
    one has no root to correlate on, and is exactly as visible as it already
    was in its own model's box either way.
    """
    # root_urn -> {model_urn -> (summary, severity)}. A dict of dicts rather
    # than a flat list so one model contributing two findings to the same
    # root still counts once, not twice.
    grouped: dict[str, dict[str, tuple[str, Severity]]] = {}
    owners_of: dict[str, tuple[str, ...]] = {}

    for verdict in verdicts:
        for rf in verdict.ruled_findings:
            if rf.severity is Severity.CLEAR:
                continue
            path = rf.finding.path
            if path is None or not path.hops:
                continue

            root_urn = path.root.urn
            bucket = grouped.setdefault(root_urn, {})
            existing = bucket.get(verdict.model_urn)
            if existing is None or _SEVERITY_RANK[rf.severity] > _SEVERITY_RANK[existing[1]]:
                bucket[verdict.model_urn] = (rf.finding.summary, rf.severity)
            owners_of.setdefault(root_urn, path.owners)

    incidents: list[Incident] = []
    for root_urn, by_model in grouped.items():
        if len(by_model) < 2:
            continue  # one model affected is a finding, not an incident

        worst = max((sev for _, sev in by_model.values()), key=lambda s: _SEVERITY_RANK[s])
        incidents.append(
            Incident(
                root_urn=root_urn,
                root_owners=owners_of.get(root_urn, ()),
                worst_severity=worst,
                affected=tuple(
                    (model_urn, summary) for model_urn, (summary, _) in sorted(by_model.items())
                ),
            )
        )

    return sorted(incidents, key=lambda i: (-i.model_count, i.root_urn))


def format_incident_lines(incident: Incident) -> list[str]:
    """Plain lines for the console reporter — no Rich markup baked in, so the
    caller decides styling rather than this module owning presentation.
    """
    owners = ", ".join(f"@{short_urn(o)}" for o in incident.root_owners) or "unassigned"
    lines = [f"{incident.root_name} ({owners}) — affects {incident.model_count} models"]
    for model_urn, summary in incident.affected:
        lines.append(f"    {short_urn(model_urn)} — {summary}")
    return lines


__all__ = ["Incident", "correlate", "format_incident_lines"]
