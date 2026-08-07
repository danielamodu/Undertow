"""Tests for the governance differ.

CERTAIN findings, like the schema differ — these are flags read off an aspect,
not inferences. The interesting cases are all about *transitions*: an asset that
was already deprecated at the last approved deploy is a known, accepted
condition, and re-reporting it every run is how a gate becomes wallpaper.

Everything is a plain object. No DataHub, no network.
"""

from __future__ import annotations

import pytest

from undertow.differ.governance import diff_governance, normalise_tag
from undertow.models import (
    AssetSnapshot,
    ColumnSnapshot,
    Confidence,
    Finding,
    FindingKind,
    UndertowSnapshot,
)

MODEL = "urn:li:mlModel:(urn:li:dataPlatform:mlflow,fraud_detector_v3,PROD)"
PAYMENTS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,raw.payments,PROD)"
LEGACY = "urn:li:dataset:(urn:li:dataPlatform:snowflake,legacy.merchants,PROD)"
FEATURE = "urn:li:mlFeature:(txn_aggregates,avg_txn_30d)"

TOM = "urn:li:corpuser:data-eng-tom"
ANA = "urn:li:corpuser:data-eng-ana"

PII = "urn:li:tag:PII"
DEPRECATED_TAG = "urn:li:tag:Deprecated"


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def asset(
    *,
    urn: str = PAYMENTS,
    entity_type: str = "dataset",
    tags: tuple[str, ...] = (),
    owners: tuple[str, ...] = (TOM,),
    deprecated: bool = False,
    note: str | None = None,
    columns: tuple[ColumnSnapshot, ...] = (),
    features: tuple[str, ...] = (FEATURE,),
) -> AssetSnapshot:
    return AssetSnapshot(
        urn=urn,
        entity_type=entity_type,
        tags=tags,
        owners=owners,
        deprecated=deprecated,
        deprecation_note=note,
        columns=columns,
        feeds_features=features,
    )


def col(path: str, *tags: str) -> ColumnSnapshot:
    return ColumnSnapshot(path=path, data_type="string", tags=tags)


def snap(*assets: AssetSnapshot) -> UndertowSnapshot:
    return UndertowSnapshot(model_urn=MODEL, assets={a.urn: a for a in assets})


def diff(
    before: AssetSnapshot, after: AssetSnapshot, *, sensitive_tags: tuple[str, ...] | None = None
) -> list[Finding]:
    return diff_governance(snap(before), snap(after), sensitive_tags=sensitive_tags)


# --------------------------------------------------------------------------
# Deprecation
# --------------------------------------------------------------------------


def test_unchanged_governance_produces_no_findings() -> None:
    unchanged = asset(tags=(PII,), owners=(TOM, ANA))

    assert diff(unchanged, unchanged) == []


def test_newly_deprecated_asset_is_reported() -> None:
    findings = diff(asset(), asset(deprecated=True, note="Retiring 2026-09-01"))

    assert len(findings) == 1
    assert findings[0].kind is FindingKind.ASSET_DEPRECATED
    assert findings[0].confidence is Confidence.CERTAIN
    assert findings[0].evidence["note"] == "Retiring 2026-09-01"
    assert "Retiring 2026-09-01" in findings[0].summary


def test_already_deprecated_asset_is_not_re_reported() -> None:
    # Known and accepted at the last cleared deploy. Repeating it every run is
    # how a gate turns into background noise.
    before = asset(deprecated=True)
    after = asset(deprecated=True)

    assert diff(before, after) == []


def test_un_deprecating_an_asset_is_not_a_finding() -> None:
    assert diff(asset(deprecated=True), asset(deprecated=False)) == []


def test_deprecation_names_the_feature_downstream_of_it() -> None:
    finding = diff(asset(), asset(deprecated=True))[0]

    assert finding.affected_feature_urn == FEATURE
    assert "avg_txn_30d" in finding.summary


def test_deprecation_on_a_feature_is_labelled_as_a_feature() -> None:
    before = asset(urn=FEATURE, entity_type="mlFeature", features=())
    after = asset(urn=FEATURE, entity_type="mlFeature", features=(), deprecated=True)

    finding = diff(before, after)[0]

    assert finding.summary.startswith("Feature ")
    assert finding.evidence["entity_type"] == "mlFeature"


# --------------------------------------------------------------------------
# Ownership
# --------------------------------------------------------------------------


def test_losing_the_last_owner_is_reported() -> None:
    findings = diff(asset(owners=(TOM,)), asset(owners=()))

    assert len(findings) == 1
    assert findings[0].kind is FindingKind.OWNERSHIP_LOST
    assert findings[0].evidence["previous_owners"] == TOM
    assert findings[0].evidence["current_owner_count"] == 0


def test_losing_one_of_several_owners_is_not_reported() -> None:
    # Owners rotate constantly. Flagging every reassignment would train people
    # to ignore the finding that actually matters.
    assert diff(asset(owners=(TOM, ANA)), asset(owners=(ANA,))) == []


def test_gaining_an_owner_is_not_reported() -> None:
    assert diff(asset(owners=(TOM,)), asset(owners=(TOM, ANA))) == []


def test_an_asset_that_never_had_an_owner_is_not_newly_orphaned() -> None:
    assert diff(asset(owners=()), asset(owners=())) == []


# --------------------------------------------------------------------------
# Sensitive classification
# --------------------------------------------------------------------------


def test_new_pii_tag_on_an_asset_is_reported() -> None:
    findings = diff(asset(), asset(tags=(PII,)))

    assert len(findings) == 1
    assert findings[0].kind is FindingKind.NEW_SENSITIVE_TAG
    assert findings[0].evidence["tag"] == "PII"
    assert findings[0].evidence["tag_urn"] == PII
    assert findings[0].subject_column is None


def test_new_pii_tag_on_a_column_is_reported_against_that_column() -> None:
    before = asset(columns=(col("email"),))
    after = asset(columns=(col("email", PII),))

    findings = diff(before, after)

    assert len(findings) == 1
    assert findings[0].kind is FindingKind.NEW_SENSITIVE_TAG
    assert findings[0].subject_column == "email"
    assert "`email`" in findings[0].summary


def test_a_tag_already_present_at_baseline_is_not_reported() -> None:
    assert diff(asset(tags=(PII,)), asset(tags=(PII,))) == []


def test_a_removed_sensitive_tag_is_not_reported() -> None:
    # Declassification is not a deploy risk.
    assert diff(asset(tags=(PII,)), asset(tags=())) == []


def test_a_new_but_unremarkable_tag_is_not_reported() -> None:
    assert diff(asset(), asset(tags=("urn:li:tag:Gold",))) == []


def test_a_tag_on_a_brand_new_column_is_left_to_the_schema_differ() -> None:
    # The column addition is already a finding. Reporting its tags separately
    # would double-count one change.
    before = asset(columns=())
    after = asset(columns=(col("email", PII),))

    assert diff(before, after) == []


@pytest.mark.parametrize(
    "tag",
    [
        "urn:li:tag:PII",
        "urn:li:tag:pii",
        "PII",
        "urn:li:glossaryTerm:Classification.PII",
        "urn:li:glossaryTerm:PII",
        "urn:li:tag:Confidential",
        "urn:li:tag:GDPR",
    ],
)
def test_sensitive_classification_is_recognised_in_every_spelling(tag: str) -> None:
    # Tags and glossary terms are treated alike on purpose: DataHub deployments
    # split classification between them more or less arbitrarily.
    assert len(diff(asset(), asset(tags=(tag,)))) == 1


def test_the_sensitive_set_is_configurable() -> None:
    internal = "urn:li:tag:CrownJewels"

    assert diff(asset(), asset(tags=(internal,))) == []
    assert len(diff(asset(), asset(tags=(internal,)), sensitive_tags=("CrownJewels",))) == 1


def test_configuring_the_sensitive_set_replaces_the_default() -> None:
    assert diff(asset(), asset(tags=(PII,)), sensitive_tags=("CrownJewels",)) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("urn:li:tag:PII", "pii"),
        ("urn:li:glossaryTerm:Classification.Sensitive", "sensitive"),
        ("  Confidential  ", "confidential"),
        ("PHI", "phi"),
    ],
)
def test_tag_normalisation(raw: str, expected: str) -> None:
    assert normalise_tag(raw) == expected


# --------------------------------------------------------------------------
# Multiple assets and combined signals
# --------------------------------------------------------------------------


def test_several_governance_changes_on_one_asset_are_separate_findings() -> None:
    before = asset(owners=(TOM,))
    after = asset(owners=(), deprecated=True, tags=(PII,))

    kinds = {f.kind for f in diff(before, after)}

    assert kinds == {
        FindingKind.ASSET_DEPRECATED,
        FindingKind.OWNERSHIP_LOST,
        FindingKind.NEW_SENSITIVE_TAG,
    }


def test_assets_present_in_only_one_snapshot_are_skipped() -> None:
    before = snap(asset(), asset(urn=LEGACY))
    after = snap(asset())

    assert diff_governance(before, after) == []


def test_findings_are_ordered_deterministically_across_assets() -> None:
    before = snap(asset(), asset(urn=LEGACY))
    after = snap(asset(deprecated=True), asset(urn=LEGACY, deprecated=True))

    urns = [f.subject_urn for f in diff_governance(before, after)]

    assert urns == [LEGACY, PAYMENTS]
