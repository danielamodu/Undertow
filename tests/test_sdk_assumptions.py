"""Preflight: assert the DataHub SDK still exposes what Undertow is built on.

Every claim here was executed against `acryl-datahub 1.6.0.17` before being
designed around. The point of keeping it as a test is that a future SDK bump
tells us *which* assumption broke, in one line, instead of surfacing as a
confusing failure deep in a resolver three days later.

Skipped when the SDK isn't installed, so the pure-core suite still runs on a
machine with nothing but pydantic.
"""

from __future__ import annotations

import inspect
import time

import pytest

datahub = pytest.importorskip("datahub", reason="acryl-datahub not installed")

import datahub.metadata.schema_classes as sc  # noqa: E402
from datahub.emitter.mcp import MetadataChangeProposalWrapper  # noqa: E402
from datahub.emitter.mcp_patch_builder import MetadataPatchProposal  # noqa: E402
from datahub.specific.aspect_helpers.structured_properties import (  # noqa: E402
    HasStructuredPropertiesPatch,
)

MODEL_URN = "urn:li:mlModel:(urn:li:dataPlatform:mlflow,fraud_detector_v3,PROD)"


def init_params(cls: type) -> set[str]:
    # `cls.__init__` on the class object, not an instance — mypy flags the
    # instance form as unsound, and the class form is what we mean anyway.
    return {p for p in inspect.signature(cls).parameters if p != "self"}


# --------------------------------------------------------------------------
# A.5 — structured properties on mlModel
# --------------------------------------------------------------------------


class MLModelPatchBuilder(HasStructuredPropertiesPatch, MetadataPatchProposal):
    """The OSS contribution, in miniature.

    No upstream equivalent exists — `datahub.specific` ships builders for
    dataset, chart, dashboard, datajob, dataproduct and form, but not mlModel.
    The base class imposes no entity restriction, so composing the two works.
    """


def test_no_upstream_mlmodel_patch_builder_exists() -> None:
    # If this ever fails, upstream shipped it and our Day 6 contribution is moot.
    import pkgutil

    import datahub.specific as specific

    modules = {m.name for m in pkgutil.iter_modules(specific.__path__)}
    assert "mlmodel" not in modules, (
        f"Upstream now ships an mlModel patch builder ({modules}); "
        "the planned OSS contribution needs rescoping."
    )


def test_mlmodel_patch_builder_produces_a_valid_patch() -> None:
    builder = MLModelPatchBuilder(MODEL_URN)
    builder.set_structured_property(
        "urn:li:structuredProperty:undertow.lastVerdict", "BLOCK"
    )
    mcps = list(builder.build())

    assert len(mcps) == 1
    (mcp,) = mcps
    # entityType is derived from the URN — this is what proves the generic base
    # class handles mlModel without a bespoke builder.
    assert mcp.entityType == "mlModel"
    assert mcp.aspectName == "structuredProperties"
    assert mcp.changeType == "PATCH"
    assert mcp.entityUrn == MODEL_URN


# --------------------------------------------------------------------------
# The two-hop lineage path
# --------------------------------------------------------------------------


def test_two_hop_path_fields_exist() -> None:
    """mlModel --Consumes--> mlFeature --DerivedFrom--> dataset.

    The three-hop path through mlFeatureTable that most people reach for does
    not work: `Contains` isn't flagged `isLineage` in the entity registry.
    These two fields are the edges that do exist.
    """
    assert "mlFeatures" in init_params(sc.MLModelPropertiesClass)
    assert "sources" in init_params(sc.MLFeaturePropertiesClass)


# --------------------------------------------------------------------------
# A.4 / A.8 — the two-tier statistical differ
# --------------------------------------------------------------------------

TIER_1_FIELDS = {"nullCount", "nullProportion", "uniqueCount", "mean", "stdev", "min", "max"}
TIER_2_FIELDS = {"quantiles", "histogram", "distinctValueFrequencies"}


def test_tier_1_profile_fields_exist() -> None:
    # Tier 1 is uncuttable — it's half the novelty claim — so it may only depend
    # on fields DataHub profiles by default.
    available = init_params(sc.DatasetFieldProfileClass)
    assert TIER_1_FIELDS.issubset(available), f"missing: {TIER_1_FIELDS - available}"


def test_tier_2_profile_fields_exist_but_are_opt_in() -> None:
    # These exist on the class; they're just `default=False` in the profiler
    # config, which is why PSI may never fire on a stock instance.
    available = init_params(sc.DatasetFieldProfileClass)
    assert TIER_2_FIELDS.issubset(available), f"missing: {TIER_2_FIELDS - available}"


def test_row_count_lives_on_the_dataset_profile() -> None:
    assert "rowCount" in init_params(sc.DatasetProfileClass)


# --------------------------------------------------------------------------
# Day 5 — assertion write-back
# --------------------------------------------------------------------------


def test_custom_assertion_write_back_constructs() -> None:
    """Core-supported, not a Cloud-only path — the GX plugin uses the same aspects."""
    assertion_urn = "urn:li:assertion:undertow-fraud_detector_v3"

    info = sc.AssertionInfoClass(
        type=sc.AssertionTypeClass.DATASET,
        customAssertion=sc.CustomAssertionInfoClass(
            type="UNDERTOW_DEPLOY_GATE", entity=MODEL_URN
        ),
        source=sc.AssertionSourceClass(type=sc.AssertionSourceTypeClass.EXTERNAL),
        description="Undertow pre-deploy lineage gate",
    )
    mcp = MetadataChangeProposalWrapper(entityUrn=assertion_urn, aspect=info)
    assert (mcp.entityType, mcp.aspectName) == ("assertion", "assertionInfo")

    run = sc.AssertionRunEventClass(
        timestampMillis=int(time.time() * 1000),
        runId="undertow-run-1",
        asserteeUrn=MODEL_URN,
        assertionUrn=assertion_urn,
        status=sc.AssertionRunStatusClass.COMPLETE,
        # A BLOCK verdict maps to FAILURE; there is no "warning" result type.
        result=sc.AssertionResultClass(type=sc.AssertionResultTypeClass.FAILURE),
    )
    mcp = MetadataChangeProposalWrapper(entityUrn=assertion_urn, aspect=run)
    assert (mcp.entityType, mcp.aspectName) == ("assertion", "assertionRunEvent")


def test_assertion_result_types_are_what_the_reporter_maps_onto() -> None:
    # WARN has no native equivalent, so the reporter maps CLEAR/WARN -> SUCCESS
    # and BLOCK -> FAILURE, with the real severity in the description.
    for name in ("SUCCESS", "FAILURE", "ERROR", "INIT"):
        assert hasattr(sc.AssertionResultTypeClass, name)
