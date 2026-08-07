"""Restore the demo graph to its seeded state.

Re-seeding is not enough on its own. `seed_datahub.py` emits the aspects the
fixture is built from — schemas, lineage, profiles, ownership — so anything it
does not emit survives a reseed untouched. The demo scripts and Undertow itself
add three kinds of aspect that fall in that gap:

  * `deprecation`, from the governance scenario
  * `globalTags`, from that scenario and from every `--write-back` run, which
    tags the model `undertow:blocked` or `undertow:cleared`
  * `datasetProfile`, from `drift_stats.py`

A leftover deprecation is the one that actually bites. It becomes part of the
next baseline, and because the governance differ reports the *transition* rather
than the state, a permanently-deprecated upstream stops being reported at all.
The gate then looks like it is passing a scenario it is simply no longer seeing.

So reset clears those explicitly, then reseeds.
"""

import os
import sys

import datahub.emitter.mce_builder as builder
import datahub.metadata.schema_classes as models
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from seed_datahub import (  # noqa: E402
    DS_CUST_URN,
    DS_STAGING_URN,
    DS_TXN_URN,
    GMS_URL,
    MODEL_CHURN_URN,
    MODEL_URN,
    TOKEN,
)
from seed_datahub import main as seed_main  # noqa: E402

DATASETS = (DS_TXN_URN, DS_CUST_URN, DS_STAGING_URN)
MODELS = (MODEL_URN, MODEL_CHURN_URN)


def clear_experiment_aspects(emitter: DatahubRestEmitter) -> None:
    """Undo what the demo scripts and write-back leave behind."""
    # `actor` must be a real URN even when clearing — GMS rejects an empty one
    # with a 422 rather than treating it as absent.
    for urn in DATASETS:
        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=models.DeprecationClass(
                    deprecated=False,
                    note="",
                    actor=builder.make_user_urn("undertow"),
                ),
            )
        )

    # Empty rather than absent: DataHub has no "delete aspect" over this path,
    # and an empty tag set diffs identically to never having had one.
    for urn in (*DATASETS, *MODELS):
        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn, aspect=models.GlobalTagsClass(tags=[])
            )
        )

    print("Cleared deprecation and tags from the fixture assets.")


def main() -> None:
    print("Resetting DataHub ML graph to baseline...")
    emitter = DatahubRestEmitter(gms_server=GMS_URL, token=TOKEN)
    clear_experiment_aspects(emitter)
    # Reseeding last so the profile emitted here overwrites any drifted one.
    seed_main()


if __name__ == "__main__":
    main()
