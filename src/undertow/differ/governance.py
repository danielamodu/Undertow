"""Governance differ — deprecation, ownership, and sensitive classification.

CERTAIN, like the schema differ: every signal here is a flag read straight off
an aspect, not an inference. `deprecation.deprecated` is a boolean; either it
flipped or it did not.

The three signals answer three questions a deploy review should ask and usually
does not:

* **Deprecated** — is this model about to ship on data someone has already
  scheduled for deletion? An upstream marked deprecated is a dated warning that
  nothing else in the pipeline reads.
* **Orphaned** — did the last owner leave? A dependency with no owner is one
  nobody will fix when it breaks, which converts a WARN into an outage later.
* **Newly sensitive** — did a PII tag appear on something this model consumes?
  That is a compliance event, and it is invisible from the model's own page.

Ownership is reported only when it drops to *zero*. Owners rotate constantly;
flagging every reassignment would train people to ignore the finding, and
"orphaned" is the condition that actually matters.
"""

from __future__ import annotations

from collections.abc import Collection

from undertow.differ._shared import short_urn
from undertow.models import AssetSnapshot, Finding, FindingKind, UndertowSnapshot
from undertow.policy import DEFAULT_SENSITIVE_TAGS


def diff_governance(
    baseline: UndertowSnapshot,
    current: UndertowSnapshot,
    *,
    sensitive_tags: Collection[str] | None = None,
) -> list[Finding]:
    """Compare governance state across every asset present in both snapshots."""
    configured = sensitive_tags if sensitive_tags is not None else DEFAULT_SENSITIVE_TAGS
    sensitive = _normalise_all(configured)

    findings: list[Finding] = []
    for urn in baseline.shared_urns(current):
        before, after = baseline.assets[urn], current.assets[urn]
        findings.extend(_diff_asset(before, after, sensitive))
    return findings


def _diff_asset(
    before: AssetSnapshot, after: AssetSnapshot, sensitive: frozenset[str]
) -> list[Finding]:
    findings: list[Finding] = []

    # Only the transition matters. An asset that was already deprecated at the
    # last approved deploy is a known, accepted condition — re-reporting it every
    # run is how a gate becomes background noise.
    if after.deprecated and not before.deprecated:
        findings.append(_deprecated(after))

    if before.owners and not after.owners:
        findings.append(_ownership_lost(before, after))

    findings.extend(_new_sensitive_tags(before, after, sensitive))
    return findings


def _new_sensitive_tags(
    before: AssetSnapshot, after: AssetSnapshot, sensitive: frozenset[str]
) -> list[Finding]:
    findings: list[Finding] = []

    for tag in sorted(set(after.tags) - set(before.tags)):
        if _is_sensitive(tag, sensitive):
            findings.append(_sensitive_tag(after, tag, column=None))

    for column in after.columns:
        previous = before.column(column.path)
        if previous is None:
            continue  # a brand-new column is the schema differ's finding, not ours
        for tag in sorted(set(column.tags) - set(previous.tags)):
            if _is_sensitive(tag, sensitive):
                findings.append(_sensitive_tag(after, tag, column=column.path))

    return findings


# ---------------------------------------------------------------------------
# Tag matching
# ---------------------------------------------------------------------------


def normalise_tag(tag: str) -> str:
    """`urn:li:tag:PII` and `urn:li:glossaryTerm:Classification.PII` both -> `pii`.

    Tags and glossary terms are treated alike deliberately. DataHub deployments
    split classification between the two more or less arbitrarily, and a differ
    that only understood one of them would miss half the real instances.
    """
    name = tag.rpartition(":")[2] if tag.startswith("urn:li:") else tag
    return name.strip().rpartition(".")[2].casefold() if "." in name else name.strip().casefold()


def _normalise_all(tags: Collection[str]) -> frozenset[str]:
    return frozenset(normalise_tag(t) for t in tags)


def _is_sensitive(tag: str, sensitive: frozenset[str]) -> bool:
    return normalise_tag(tag) in sensitive


# ---------------------------------------------------------------------------
# Finding constructors
# ---------------------------------------------------------------------------


def _deprecated(asset: AssetSnapshot) -> Finding:
    note = asset.deprecation_note
    return Finding(
        kind=FindingKind.ASSET_DEPRECATED,
        subject_urn=asset.urn,
        affected_feature_urn=asset.primary_feature(),
        summary=(
            f"{_kind_label(asset)} {short_urn(asset.urn)} is now marked deprecated"
            f"{_feature_clause(asset)}"
            + (f" — {note}" if note else "")
        ),
        evidence=_evidence(
            asset,
            was_deprecated=False,
            is_deprecated=True,
            note=note,
        ),
    )


def _ownership_lost(before: AssetSnapshot, after: AssetSnapshot) -> Finding:
    return Finding(
        kind=FindingKind.OWNERSHIP_LOST,
        subject_urn=after.urn,
        affected_feature_urn=after.primary_feature(),
        summary=(
            f"{_kind_label(after)} {short_urn(after.urn)} lost its last owner"
            f"{_feature_clause(after)}; nobody is on the hook when it breaks"
        ),
        evidence=_evidence(
            after,
            previous_owners=", ".join(sorted(before.owners)),
            previous_owner_count=len(before.owners),
            current_owner_count=0,
        ),
    )


def _sensitive_tag(asset: AssetSnapshot, tag: str, *, column: str | None) -> Finding:
    where = f"`{column}` on {short_urn(asset.urn)}" if column else short_urn(asset.urn)
    return Finding(
        kind=FindingKind.NEW_SENSITIVE_TAG,
        subject_urn=asset.urn,
        subject_column=column,
        affected_feature_urn=asset.primary_feature(column),
        summary=(
            f"{short_urn(tag)} was applied to {where}"
            f"{_feature_clause(asset, column)}; sensitive data now reaches this model"
        ),
        evidence=_evidence(asset, column, tag=short_urn(tag), tag_urn=tag),
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _evidence(
    asset: AssetSnapshot,
    column: str | None = None,
    **extra: str | float | int | bool | None,
) -> dict[str, str | float | int | bool | None]:
    features = asset.features_for(column)
    evidence: dict[str, str | float | int | bool | None] = {
        "entity_type": asset.entity_type,
        "on_feature_path": bool(features),
        "features": ", ".join(sorted(features)) or None,
    }
    if column is not None:
        evidence["column"] = column
    evidence.update(extra)
    return evidence


def _feature_clause(asset: AssetSnapshot, column: str | None = None) -> str:
    features = asset.features_for(column)
    if not features:
        return ""
    if len(features) == 1:
        return f", which feeds {short_urn(features[0])}"
    return f", which feeds {len(features)} live features"


def _kind_label(asset: AssetSnapshot) -> str:
    return {
        "dataset": "Dataset",
        "mlFeature": "Feature",
        "mlFeatureTable": "Feature table",
        "mlModel": "Model",
    }.get(asset.entity_type, "Asset")


__all__ = ["diff_governance", "normalise_tag"]
