"""Simulate statistical drift: shift the profile of staging.transactions_clean.amount.

The counterpart to `break_schema.py`, and the reason both exist. A dropped column
is a fact read off the graph — `CERTAIN`, and it blocks. A distribution moving is
an inference — `PROBABLE`, and it warns without stopping the deploy.

Running both is how you show the gate can tell the difference, which is the
distinction the whole policy engine is built around.

Baseline for `amount` is mean 125.50, stdev 45.20, null proportion 0.0. The
values below are chosen to cross two default thresholds and no others:

  * mean 342.10  ->  (342.10 - 125.50) / 45.20 = 4.79 sigma, over mean_shift_sigma (3.0)
  * null proportion 0.18  ->  18 percentage points, over null_rate_jump_pp (10.0)

Row count is left alone so `ROW_COUNT_CHANGE` stays quiet and the report shows
targeted findings rather than everything firing at once.
"""

import os
import sys
import time

import datahub.metadata.schema_classes as models
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter

GMS_URL = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
TOKEN = os.environ.get("DATAHUB_GMS_TOKEN")

DS_STAGING_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:snowflake,staging.transactions_clean,PROD)"
)


def main() -> None:
    print(f"Simulating statistical drift on {DS_STAGING_URN}...")
    emitter = DatahubRestEmitter(gms_server=GMS_URL, token=TOKEN)

    drifted = models.DatasetProfileClass(
        timestampMillis=int(time.time() * 1000),
        rowCount=10000,
        columnCount=4,
        fieldProfiles=[
            models.DatasetFieldProfileClass(
                fieldPath="amount",
                nullCount=1800,
                nullProportion=0.18,
                min="1.00",
                max="18400.00",
                mean="342.10",
                stdev="512.75",
            )
        ],
    )

    try:
        emitter.emit_mcp(
            MetadataChangeProposalWrapper(entityUrn=DS_STAGING_URN, aspect=drifted)
        )
    except Exception as exc:
        print(f"Error emitting drifted profile: {exc}", file=sys.stderr)
        sys.exit(1)

    print("Shifted `amount`: mean 125.50 -> 342.10 (4.79 sigma), nulls 0% -> 18%.")
    print("Expect WARN, exit 0 — statistical findings are PROBABLE and do not block.")
    print("Use `undertow check --model <urn> --fail-on-warn` to make them blocking.")


if __name__ == "__main__":
    main()
