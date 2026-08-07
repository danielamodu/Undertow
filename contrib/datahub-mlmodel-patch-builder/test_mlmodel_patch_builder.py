"""Tests for the proposed `datahub/specific/mlmodel.py`.

Written against the shape DataHub's own patch-builder tests use, so the file can
move upstream alongside `mlmodel.py` with only the import path changed.

The point of the builder is that it emits `PATCH`, not `UPSERT`. The first two
tests pin that, because it is the entire reason the gap matters: an `UPSERT` on
`structuredProperties` rewrites the whole aspect and silently discards every
property the writer did not know about.

Run from the repository root:

    pytest contrib/datahub-mlmodel-patch-builder/ -q
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from mlmodel import MLModelPatchBuilder  # noqa: E402

MODEL_URN = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detector_v3,PROD)"


def patches_of(proposal) -> list[dict]:
    """Decode the JSON-Patch operations carried by a built proposal.

    The payload is an envelope — `arrayPrimaryKeys` alongside the operation list
    under `patch` — not a bare RFC 6902 array.
    """
    value = proposal.aspect.value
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return json.loads(value)["patch"]


def test_builder_emits_patch_not_upsert() -> None:
    """The whole reason this class needs to exist."""
    builder = MLModelPatchBuilder(MODEL_URN)
    builder.set_structured_property("undertow_risk_verdict", "BLOCK")

    proposals = list(builder.build())

    assert proposals, "builder produced no proposals"
    for proposal in proposals:
        assert proposal.changeType == "PATCH"
        assert proposal.entityUrn == MODEL_URN


def test_entity_type_is_inferred_from_the_urn() -> None:
    """`MetadataPatchProposal` is entity-agnostic; mlModel needs no special casing.

    This is the claim that makes the upstream change small — the base class
    already resolves the entity type via `guess_entity_type`, so the builder is
    composition of existing mixins rather than new machinery.
    """
    proposals = list(
        MLModelPatchBuilder(MODEL_URN)
        .set_structured_property("undertow_risk_verdict", "BLOCK")
        .build()
    )

    assert proposals[0].entityType == "mlModel"


def test_independent_property_writes_do_not_overwrite_each_other() -> None:
    """Two properties, two patches, no read-modify-write in between.

    An UPSERT path would require the caller to know both values at once. The
    patch path does not, which is what lets independent writers coexist.
    """
    builder = MLModelPatchBuilder(MODEL_URN)
    builder.set_structured_property("undertow_risk_verdict", "BLOCK")
    builder.set_structured_property("undertow_last_checked", "2026-08-07T16:46:20Z")

    operations = [op for proposal in builder.build() for op in patches_of(proposal)]

    paths = [op["path"] for op in operations]
    assert len(operations) == 2
    assert len(set(paths)) == 2, f"operations collided on one path: {paths}"

    # Each property is addressed at its own path. Nothing targets the aspect
    # root, which is what a full-aspect overwrite would have to do.
    for op in operations:
        assert op["op"] == "add"
        assert op["path"].startswith("/properties/urn:li:structuredProperty:")
        assert op["path"] not in {"", "/", "/properties"}


def test_structured_properties_target_the_right_aspect() -> None:
    proposals = list(
        MLModelPatchBuilder(MODEL_URN)
        .set_structured_property("undertow_baseline", "{}")
        .build()
    )

    assert proposals[0].aspectName == "structuredProperties"


@pytest.mark.parametrize(
    "method, args",
    [
        ("add_tag", ("urn:li:tag:undertow:blocked",)),
        ("add_owner", ("urn:li:corpuser:ml_eng_alex",)),
    ],
)
def test_inherited_mixins_are_wired_up(method: str, args: tuple) -> None:
    """The mixins are the point — mlModel gets tags and ownership for free."""
    builder = MLModelPatchBuilder(MODEL_URN)

    assert hasattr(builder, method), f"{method} missing from the composed builder"
