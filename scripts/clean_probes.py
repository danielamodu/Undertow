"""Remove the probe_* structured properties left over from manual experiments.

`probe_alpha` and `probe_beta` were written by hand while verifying that PATCH
writes survive where a full-aspect UPSERT would clobber them. They proved the
point, and then sat in the catalog looking like debris — which is what they are.

The PATCH argument does not need them: it is carried by the 13 tests in
contrib/datahub-mlmodel-patch-builder/ and by the upstream PR, both of which a
reader can run.

Safe to run more than once; missing properties are ignored.
"""

import os
import sys

from datahub.ingestion.graph.client import DataHubGraph, DataHubGraphConfig

GMS_URL = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
TOKEN = os.environ.get("DATAHUB_GMS_TOKEN")

PROBES = [
    "urn:li:structuredProperty:probe_alpha",
    "urn:li:structuredProperty:probe_beta",
]


def main() -> None:
    graph = DataHubGraph(DataHubGraphConfig(server=GMS_URL, token=TOKEN, timeout_sec=30))

    for urn in PROBES:
        try:
            graph.hard_delete_entity(urn)
            print(f"deleted {urn}")
        except Exception as exc:
            # Already gone, or the build has no hard delete. Either way the
            # property is not something the demo depends on.
            print(f"skipped {urn}: {type(exc).__name__}: {exc}", file=sys.stderr)

    print()
    print("Re-run `python scripts/reset_demo.py` to rewrite the model's properties.")


if __name__ == "__main__":
    main()
