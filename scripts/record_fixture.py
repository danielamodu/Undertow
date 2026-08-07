"""Record the live fixture graph to a file, so the demo runs without DataHub.

Standing up DataHub takes ten minutes and several gigabytes. That is a fair ask
of someone adopting Undertow and an unreasonable one of someone deciding whether
to look at it for five minutes.

So the graph gets recorded: every entity, edge, schema and profile the resolver
would fetch, captured from a real instance and replayed offline by
`RecordedLineageSource`. The recording is a transcript of real answers from a
real DataHub, not a hand-built fixture — which is the only reason replaying it
proves anything.

Two states are captured, because a verdict is a comparison:

  * `before` — the seeded graph, which becomes the baseline
  * `after`  — the same graph with `transaction_amount` dropped

Run against a seeded instance:

    python scripts/reset_demo.py
    python scripts/record_fixture.py
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from typing import Any

from undertow.resolver import SdkLineageSource

GMS_URL = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
TOKEN = os.environ.get("DATAHUB_GMS_TOKEN")
OUT = pathlib.Path(__file__).parent.parent / "examples" / "recorded-graph.json"

FRAUD = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detector_v3,PROD)"
CHURN = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,churn_predictor_v1,PROD)"

# Everything the traversal touches walking upstream from either model.
SEEDS = [
    FRAUD,
    CHURN,
    "urn:li:mlFeature:(fraud_detection,transaction_velocity_7d)",
    "urn:li:mlFeature:(fraud_detection,customer_risk_score)",
    "urn:li:mlFeature:(customer_churn,customer_txn_volume)",
    "urn:li:dataset:(urn:li:dataPlatform:snowflake,staging.transactions_clean,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:snowflake,transactions.raw,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:snowflake,customers.raw,PROD)",
]


def aspects_to_plain(node: Any) -> dict[str, Any]:
    """Aspect classes to plain JSON, via the SDK's own serialiser where it has one."""
    out: dict[str, Any] = {}
    for name, aspect in (node.aspects or {}).items():
        if hasattr(aspect, "to_obj"):
            try:
                out[name] = aspect.to_obj()
                continue
            except Exception:
                pass
        if isinstance(aspect, dict | list | str | int | float | bool) or aspect is None:
            out[name] = aspect
    return out


def capture(source: SdkLineageSource) -> dict[str, Any]:
    entities: dict[str, Any] = {}
    lineage: dict[str, Any] = {}
    schemas: dict[str, Any] = {}
    profiles: dict[str, Any] = {}

    nodes = source.get_entities(SEEDS)
    for urn in SEEDS:
        node = nodes.get(urn)
        entities[urn] = {
            "entity_type": node.entity_type if node else urn.split(":")[2],
            "aspects": aspects_to_plain(node) if node else {},
        }

        for direction in ("UPSTREAM", "DOWNSTREAM"):
            try:
                edges = source.get_lineage(urn, direction=direction)
            except Exception:
                edges = []
            lineage.setdefault(urn, {})[direction] = [
                {
                    "source_urn": e.source_urn,
                    "target_urn": e.target_urn,
                    "relationship": e.relationship,
                    "via": e.via,
                }
                for e in edges
            ]

        schemas[urn] = [
            {
                "field_path": f.field_path,
                "data_type": f.data_type,
                "native_type": f.native_type,
                "nullable": f.nullable,
                "tags": list(f.tags),
            }
            for f in source.list_schema_fields(urn)
        ]

        profile = source.get_latest_profile(urn)
        if profile:
            profiles[urn] = profile

    return {
        "entities": entities,
        "lineage": lineage,
        "schemas": schemas,
        "profiles": profiles,
    }


def main() -> None:
    root = pathlib.Path(__file__).parent.parent
    source = SdkLineageSource(gms_url=GMS_URL, token=TOKEN)
    if source.connection_error:
        raise SystemExit(f"cannot reach DataHub: {source.connection_error}")

    print("Recording the seeded graph...")
    before = capture(source)
    if not before["schemas"].get(
        "urn:li:dataset:(urn:li:dataPlatform:snowflake,transactions.raw,PROD)"
    ):
        raise SystemExit("transactions.raw has no schema — reseed before recording.")

    print("Dropping transaction_amount...")
    subprocess.run(
        [sys.executable, str(root / "scripts" / "break_schema.py")],
        check=True,
        capture_output=True,
    )

    print("Recording the broken graph...")
    after = capture(SdkLineageSource(gms_url=GMS_URL, token=TOKEN))

    payload = {
        "_comment": (
            "Recorded from a live DataHub OSS v1.7.0 by scripts/record_fixture.py. "
            "Replayed offline by `undertow demo` so the project can be evaluated "
            "without standing up the stack. Not hand-written."
        ),
        "gms_version": "1.7.0",
        "models": {"fraud": FRAUD, "churn": CHURN},
        "before": before,
        "after": after,
    }

    with open(OUT, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")

    size_kb = OUT.stat().st_size / 1024
    print(f"Wrote {OUT.relative_to(root).as_posix()} ({size_kb:.0f} KB)")
    print("Restoring the graph...")
    subprocess.run(
        [sys.executable, str(root / "scripts" / "reset_demo.py")],
        check=True,
        capture_output=True,
    )
    print("Done.")


if __name__ == "__main__":
    main()
