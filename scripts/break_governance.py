"""Simulate governance changes upstream: a deprecation and a new PII tag.

The third demo, completing the set. Undertow has three differs and each answers
a different question:

    make break            schema      — a column is gone            CERTAIN, BLOCK
    make break-stats      statistics  — a distribution moved        PROBABLE, WARN
    make break-governance governance  — policy around the data      CERTAIN, mixed

Governance is the one people forget. Nothing about the data changed here: every
column is present, every distribution is where it was. What changed is that
somebody scheduled the upstream table for deletion, and somebody else classified
the staging table as containing PII. Both reach a production model, and neither
is visible from a schema diff or a profile.

Note the split verdict this produces. Deprecation blocks — shipping a model on
data with a deletion date is a dated failure. A new sensitive tag warns: the
model may now be carrying restricted data, which is a compliance question for a
human rather than something a gate should decide on its own.
"""

import os
import sys
import time

import datahub.metadata.schema_classes as models
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter

GMS_URL = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
TOKEN = os.environ.get("DATAHUB_GMS_TOKEN")

DS_TXN_URN = "urn:li:dataset:(urn:li:dataPlatform:snowflake,transactions.raw,PROD)"
DS_STAGING_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:snowflake,staging.transactions_clean,PROD)"
)
PII_TAG = "urn:li:tag:PII"


def main() -> None:
    print("Simulating governance changes upstream...")
    emitter = DatahubRestEmitter(gms_server=GMS_URL, token=TOKEN)

    try:
        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=DS_TXN_URN,
                aspect=models.DeprecationClass(
                    deprecated=True,
                    note="Superseded by transactions.v2; deletion scheduled.",
                    # 30 days out, so the report carries a real date rather than
                    # an abstraction.
                    decommissionTime=int(time.time() * 1000) + 30 * 86_400_000,
                    actor="urn:li:corpuser:data_eng_tom",
                ),
            )
        )

        # The tag entity has to exist before it can be meaningfully displayed.
        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=PII_TAG,
                aspect=models.TagPropertiesClass(
                    name="PII", description="Personally identifiable information."
                ),
            )
        )
        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=DS_STAGING_URN,
                aspect=models.GlobalTagsClass(
                    tags=[models.TagAssociationClass(tag=PII_TAG)]
                ),
            )
        )
    except Exception as exc:
        print(f"Error emitting governance changes: {exc}", file=sys.stderr)
        sys.exit(1)

    print("Deprecated transactions.raw, and tagged staging.transactions_clean as PII.")
    print("Expect BLOCK (deprecation) plus a WARN (new sensitive tag), exit 1.")
    print("Run `make reset` afterwards — these aspects survive a plain reseed.")


if __name__ == "__main__":
    main()
