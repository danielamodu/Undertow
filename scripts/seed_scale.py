"""Seed a large, deliberately hostile graph to measure what the gate costs.

The demo fixture is six assets. That proves the logic and says nothing about
what happens on a catalog with thousands, which is the first thing anyone
running a real DataHub will ask.

This builds a separate `scale.*` namespace — it never touches the demo fixture,
so both can live in the same instance — shaped to be worse than a real catalog
rather than kinder:

  * a wide model: 40 features, each from its own staging table
  * depth: every staging table sits on a raw table, so the chain is
    raw -> staging -> feature -> model for all 40 branches
  * fan-in: several features share upstream tables, so the traversal meets the
    same node by more than one path
  * a cycle between two staging tables, which a naive walk never returns from
  * unowned assets, so attribution has to report "unassigned" rather than fail
  * unprofiled assets, so coverage has to say what it could not assess
  * a deep chain, deeper than max_hops, to prove the bound actually binds

Run:

    python scripts/seed_scale.py --features 40
    time undertow check --model "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,scale_model_v1,PROD)"
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import datahub.emitter.mce_builder as builder
import datahub.metadata.schema_classes as models
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter

GMS_URL = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
TOKEN = os.environ.get("DATAHUB_GMS_TOKEN")

PLATFORM = builder.make_data_platform_urn("snowflake")
MODEL_URN = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,scale_model_v1,PROD)"
DEEP_CHAIN = 12  # longer than the default max_hops of 5


def dataset(name: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:snowflake,scale.{name},PROD)"


def feature(index: int) -> str:
    return f"urn:li:mlFeature:(scale_features,feature_{index:03d})"


def schema_for(name: str, columns: list[str]) -> models.SchemaMetadataClass:
    return models.SchemaMetadataClass(
        schemaName=f"scale.{name}",
        platform=PLATFORM,
        version=0,
        hash="",
        platformSchema=models.OtherSchemaClass(rawSchema=""),
        fields=[
            models.SchemaFieldClass(
                fieldPath=c,
                type=models.SchemaFieldDataTypeClass(type=models.NumberTypeClass()),
                nativeDataType="DECIMAL(10,2)",
                nullable=False,
            )
            for c in columns
        ],
    )


def upstream(target: str, sources: list[str], actor: str) -> MetadataChangeProposalWrapper:
    stamp = models.AuditStampClass(
        time=int(time.time() * 1000), actor=builder.make_user_urn(actor)
    )
    return MetadataChangeProposalWrapper(
        entityUrn=target,
        aspect=models.UpstreamLineageClass(
            upstreams=[
                models.UpstreamClass(
                    dataset=s,
                    type=models.DatasetLineageTypeClass.TRANSFORMED,
                    auditStamp=stamp,
                )
                for s in sources
            ]
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--features", type=int, default=40)
    args = parser.parse_args()

    emitter = DatahubRestEmitter(gms_server=GMS_URL, token=TOKEN)
    emitted = 0

    def emit(mcp: MetadataChangeProposalWrapper) -> None:
        nonlocal emitted
        emitter.emit_mcp(mcp)
        emitted += 1

    print(f"Seeding a {args.features}-feature scale graph into {GMS_URL} ...")

    feature_urns = []
    for i in range(args.features):
        raw = dataset(f"raw_{i:03d}")
        staging = dataset(f"staging_{i:03d}")

        # Some raw tables carry no owner and no profile on purpose.
        emit(
            MetadataChangeProposalWrapper(
                entityUrn=raw, aspect=schema_for(f"raw_{i:03d}", ["amount", "id", "ts"])
            )
        )
        if i % 3 != 0:
            emit(
                MetadataChangeProposalWrapper(
                    entityUrn=raw,
                    aspect=models.OwnershipClass(
                        owners=[
                            models.OwnerClass(
                                owner=builder.make_user_urn(f"eng_{i % 7}"),
                                type=models.OwnershipTypeClass.TECHNICAL_OWNER,
                            )
                        ]
                    ),
                )
            )
        if i % 4 != 0:
            emit(
                MetadataChangeProposalWrapper(
                    entityUrn=raw,
                    aspect=models.DatasetProfileClass(
                        timestampMillis=int(time.time() * 1000),
                        rowCount=10_000 + i,
                        columnCount=3,
                        fieldProfiles=[
                            models.DatasetFieldProfileClass(
                                fieldPath="amount",
                                nullCount=0,
                                nullProportion=0.0,
                                min="1.00",
                                max="500.00",
                                mean="100.00",
                                stdev="20.00",
                            )
                        ],
                    ),
                )
            )

        emit(
            MetadataChangeProposalWrapper(
                entityUrn=staging, aspect=schema_for(f"staging_{i:03d}", ["amount", "id"])
            )
        )
        # Fan-in: every third staging table also reads a shared hot table, so the
        # traversal reaches the same node down many different paths.
        sources = [raw] + ([dataset("hot_shared")] if i % 3 == 0 else [])
        emit(upstream(staging, sources, f"eng_{i % 7}"))

        emit(
            MetadataChangeProposalWrapper(
                entityUrn=feature(i),
                aspect=models.MLFeaturePropertiesClass(
                    description=f"scale feature {i}",
                    dataType="CONTINUOUS",
                    sources=[staging],
                ),
            )
        )
        feature_urns.append(feature(i))

    # The shared hot table every third branch reads.
    emit(
        MetadataChangeProposalWrapper(
            entityUrn=dataset("hot_shared"), aspect=schema_for("hot_shared", ["amount"])
        )
    )

    # A cycle: two staging tables each declared upstream of the other. A walk
    # without a visited-set never comes back from this.
    a, b = dataset("cycle_a"), dataset("cycle_b")
    for name, urn in (("cycle_a", a), ("cycle_b", b)):
        emit(MetadataChangeProposalWrapper(entityUrn=urn, aspect=schema_for(name, ["amount"])))
    emit(upstream(a, [b], "eng_0"))
    emit(upstream(b, [a], "eng_0"))
    emit(upstream(dataset("staging_000"), [dataset("raw_000"), a], "eng_0"))

    # A chain deeper than max_hops, to prove the bound binds.
    previous = dataset("deep_00")
    emit(MetadataChangeProposalWrapper(entityUrn=previous, aspect=schema_for("deep_00", ["amount"])))
    for depth in range(1, DEEP_CHAIN):
        current = dataset(f"deep_{depth:02d}")
        emit(
            MetadataChangeProposalWrapper(
                entityUrn=current, aspect=schema_for(f"deep_{depth:02d}", ["amount"])
            )
        )
        emit(upstream(previous, [current], "eng_0"))
        previous = current
    emit(upstream(dataset("staging_001"), [dataset("raw_001"), dataset("deep_00")], "eng_0"))

    emit(
        MetadataChangeProposalWrapper(
            entityUrn=MODEL_URN,
            aspect=models.MLModelPropertiesClass(
                description=f"Scale test model consuming {args.features} features.",
                mlFeatures=feature_urns,
            ),
        )
    )
    emit(
        MetadataChangeProposalWrapper(
            entityUrn=MODEL_URN,
            aspect=models.OwnershipClass(
                owners=[
                    models.OwnerClass(
                        owner=builder.make_user_urn("ml_eng_scale"),
                        type=models.OwnershipTypeClass.TECHNICAL_OWNER,
                    )
                ]
            ),
        )
    )

    print(f"Emitted {emitted} aspects.")
    print(f"Model: {MODEL_URN}")
    print()
    print("Now run:")
    print(f'  undertow baseline --model "{MODEL_URN}"')
    print(f'  undertow check --model "{MODEL_URN}"')


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error seeding scale graph: {exc}", file=sys.stderr)
        sys.exit(1)
