# `MLModelPatchBuilder` — proposed addition to DataHub

**Submitted upstream: [datahub-project/datahub#18979](https://github.com/datahub-project/datahub/pull/18979)**, closing
[#18971](https://github.com/datahub-project/datahub/issues/18971).

This directory is the vendored copy, so Undertow works against today's released
SDK rather than waiting on the PR to merge.

| File | Destination upstream |
|---|---|
| [`mlmodel.py`](mlmodel.py) | `metadata-ingestion/src/datahub/specific/mlmodel.py` |
| [`test_mlmodel_patch_builder.py`](test_mlmodel_patch_builder.py) | `metadata-ingestion/tests/unit/patch/test_mlmodel_patch_builder.py` |

Both files are byte-identical to the submitted versions, except that the test
imports the module sitting next to it rather than `datahub.specific.mlmodel`,
which only resolves once the PR lands.

## The gap

`datahub.sdk.entity_client.update()` branches on the type of its argument: an `Entity`
becomes a full-aspect `UPSERT`, a `MetadataPatchProposal` becomes a surgical `PATCH`.

`datahub/specific/` ships patch builders for `chart`, `dashboard`, `dataJob`,
`dataProduct`, `dataset`, `form`, and `structuredProperty` — but none for any ML entity.
So `mlModel` aspects are `UPSERT`-only unless a downstream project hand-rolls a builder,
which is exactly what Undertow had to do.

## Why it matters

`UPSERT` on `structuredProperties` rewrites the whole aspect. Any property the writer
did not know about is destroyed. Verified against a live GMS v1.7.0:

| Write path | Result |
|---|---|
| Two independent `PATCH` writes | Both properties survive |
| One full-aspect `UPSERT` | The property the writer didn't know about is destroyed |

This is not theoretical for anything that writes ML metadata from more than one place.
Undertow writes `undertow_risk_verdict`, `undertow_last_checked`, and
`undertow_baseline` as separate concerns, on different schedules.

Live evidence is in [`../../examples/datahub-writeback.json`](../../examples/datahub-writeback.json):
a `probe_alpha` property written by an unrelated process is still present on the model
after Undertow wrote three properties of its own.

## The change

Composition of DataHub's existing entity-agnostic mixins, in the same shape as
`DataProductPatchBuilder`. No new machinery:

```python
class MLModelPatchBuilder(
    HasOwnershipPatch,
    HasCustomPropertiesPatch,
    HasStructuredPropertiesPatch,
    HasTagsPatch,
    HasTermsPatch,
    HasDomainsPatch,
    HasInstitutionalMemoryPatch,
    MetadataPatchProposal,
):
    ...
```

`MetadataPatchProposal` already resolves the entity type through
`guess_entity_type(urn)`, so `mlModel` needs no special casing — the test suite pins
that.

## Running the tests

```bash
pytest contrib/datahub-mlmodel-patch-builder/ -q
```

13 tests, no DataHub required. They assert the proposals carry `changeType=PATCH`,
resolve `entityType=mlModel` from the URN alone, address each structured property at its
own JSON-Patch path rather than the aspect root, patch the canonical `hyperParams` field
rather than the deprecated `hyperParameters` map, add and remove `mlFeatures` entries
individually instead of rewriting the array, and route tags and ownership through the
inherited mixins.

## Scope note

`datahub.sdk.MLModel` already supports mlModel structured properties through the newer
SDK layer. The gap is specifically the absence of a `PATCH`-emitting builder, not the
absence of mlModel support.
