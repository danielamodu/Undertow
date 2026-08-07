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


# --------------------------------------------------------------------------
# Which surface an aspect lives on
# --------------------------------------------------------------------------
#
# The assumption that cost the most. `datasetProfile` is a timeseries aspect,
# so it never appears in `get_entity_semityped`, so the resolver's
# `if "datasetProfile" in aspects` branch could not fire against a live GMS.
# The statistical differ was fully implemented, fully unit-tested, and connected
# to nothing.
#
# `ASPECT_TYPE` makes that introspectable, which turns the mistake into an
# assertion instead of a thing to remember.


# Aspects the traversal looks up by name in `node.aspects`, which is populated
# from an entity snapshot. Every one of these must be versioned.
SNAPSHOT_ASPECTS = [
    "schemaMetadata",
    "globalTags",
    "ownership",
    "deprecation",
    "structuredProperties",
    "institutionalMemory",
]

# Aspects Undertow reads through the timeseries API instead.
TIMESERIES_ASPECTS = ["datasetProfile", "assertionRunEvent"]


def aspect_class(name: str) -> type:
    for candidate in vars(sc).values():
        if isinstance(candidate, type) and getattr(candidate, "ASPECT_NAME", None) == name:
            return candidate
    raise AssertionError(f"no aspect class named {name}")


@pytest.mark.parametrize("name", SNAPSHOT_ASPECTS)
def test_aspects_read_from_the_snapshot_are_versioned(name: str) -> None:
    """A timeseries aspect appearing here would silently read as absent."""
    assert aspect_class(name).ASPECT_TYPE == "default", (
        f"{name} is not a versioned aspect, so it will not appear in "
        "get_entity_semityped and the traversal branch that reads it is dead."
    )


@pytest.mark.parametrize("name", TIMESERIES_ASPECTS)
def test_aspects_read_from_the_timeseries_api_are_timeseries(name: str) -> None:
    """If one of these became versioned, the extra round trip is wasted."""
    assert aspect_class(name).ASPECT_TYPE == "timeseries"


def test_get_timeseries_values_still_requires_a_filter() -> None:
    """`filter` is positional and required — omitting it is a TypeError.

    Worth pinning because the natural call omits it, and the failure is at
    runtime inside a path that catches broadly.
    """
    from datahub.ingestion.graph.client import DataHubGraph

    params = inspect.signature(DataHubGraph.get_timeseries_values).parameters

    assert "filter" in params
    assert params["filter"].default is inspect.Parameter.empty
    assert {"entity_urn", "aspect_type", "limit"} <= set(params)


def test_scroll_lineage_still_returns_relationships() -> None:
    """The SDK source reads `.relationships` off the result.

    It does so through `getattr(..., None) or []`, so a rename would produce an
    empty edge list rather than an error — and an empty edge list is read
    downstream as "nothing upstream changed", which is a CLEAR verdict.
    """
    import dataclasses

    from datahub.ingestion.graph.client import DataHubGraph
    from datahub.ingestion.graph.openapi import LineageRelationshipScrollResult

    assert hasattr(DataHubGraph, "scroll_lineage")
    params = inspect.signature(DataHubGraph.scroll_lineage).parameters
    assert {"urns", "direction", "count"} <= set(params)

    fields = {f.name for f in dataclasses.fields(LineageRelationshipScrollResult)}
    assert "relationships" in fields

    # And the fields the source reads off each relationship.
    relationship = LineageRelationshipScrollResult.__annotations__["relationships"]
    del relationship  # the element type is a forward ref; the names below are the contract
    from datahub.ingestion.graph.openapi import LineageRelationship

    edge_fields = {f.name for f in dataclasses.fields(LineageRelationship)}
    assert {"upstream_urn", "downstream_urn", "relationship_type"} <= edge_fields
