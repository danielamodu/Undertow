"""Attributor package — attaches lineage provenance and owner attribution to findings.

Pure function: `(findings, footprint, query_evidence) -> list[Finding]`.
No network calls. Receives findings from the differs and footprint from the resolver,
and returns findings enriched with root-to-leaf AttributionPath objects, owners,
and optional query evidence.
"""

from __future__ import annotations

from undertow.models import (
    AttributionPath,
    DependencyFootprint,
    Finding,
)


def attribute_findings(
    findings: list[Finding],
    footprint: DependencyFootprint,
    *,
    query_evidence: dict[str, str] | None = None,
) -> list[Finding]:
    """Enrich each finding with its AttributionPath, resolved owners, and query evidence.

    For each finding:
    1. Looks up the recorded lineage path for `finding.subject_urn` in `footprint.paths`.
    2. Attaches `subject_column` to the root hop if present.
    3. Resolves owners from the root-cause asset snapshot.
    4. Attaches query/SQL evidence if available.
    5. Returns updated `Finding` instances with `path` populated.
    """
    enriched: list[Finding] = []
    queries = query_evidence or {}

    for finding in findings:
        path = footprint.paths.get(finding.subject_urn)
        if path is None:
            evidence = dict(finding.evidence)
            evidence["unattributed"] = True
            enriched.append(finding.model_copy(update={"evidence": evidence}))
            continue

        root_asset = footprint.snapshot.asset(finding.subject_urn)
        owners = path.owners
        if not owners and root_asset:
            owners = root_asset.owners

        hops = list(path.hops)
        if hops and finding.subject_column:
            root_hop = hops[0]
            if root_hop.column != finding.subject_column:
                hops[0] = root_hop.model_copy(update={"column": finding.subject_column})

        enriched_path = AttributionPath(hops=tuple(hops), owners=tuple(owners))

        evidence = dict(finding.evidence)
        if finding.subject_urn in queries:
            evidence["query_sql"] = queries[finding.subject_urn]

        # The path already runs root-cause -> ... -> mlFeature -> mlModel, so the
        # affected feature is on it and does not need a second lookup. Without
        # this the reporter fell back to "unknown_feature" in the headline while
        # printing the feature's URN two lines below — the single most visible
        # defect in the demo's output.
        update: dict[str, object] = {"path": enriched_path, "evidence": evidence}
        if finding.affected_feature_urn is None:
            feature_urn = _feature_on_path(enriched_path)
            if feature_urn is not None:
                update["affected_feature_urn"] = feature_urn

        enriched.append(finding.model_copy(update=update))

    return enriched


def _feature_on_path(path: AttributionPath) -> str | None:
    """The mlFeature this change reaches, read off the path itself.

    Walked from the leaf backwards: the hop nearest the model is the feature the
    model actually consumes, which is the one worth naming when a path fans
    through more than one.
    """
    for hop in reversed(path.hops):
        if hop.entity_type == "mlFeature":
            return hop.urn
    return None


__all__ = ["attribute_findings"]
