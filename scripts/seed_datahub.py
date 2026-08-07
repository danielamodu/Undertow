"""Seed DataHub with a realistic ML lineage graph and baseline snapshot."""

import json
import os
import sys
import time

import datahub.emitter.mce_builder as builder
import datahub.metadata.schema_classes as models
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter

from undertow.models import (
    AssetSnapshot,
    ColumnSnapshot,
    FieldProfileSnapshot,
    ProfileSnapshot,
    UndertowSnapshot,
)
from undertow.reporter.datahub_writer import MLModelPatchBuilder

GMS_URL = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
TOKEN = os.environ.get("DATAHUB_GMS_TOKEN")

# URN Constants
DS_TXN_URN = "urn:li:dataset:(urn:li:dataPlatform:snowflake,transactions.raw,PROD)"
DS_CUST_URN = "urn:li:dataset:(urn:li:dataPlatform:snowflake,customers.raw,PROD)"
# The staging layer is what makes the demo's attribution path more than one hop.
# Without a dataset -> dataset edge there is no chain to walk, and a gate that
# only ever looks at a feature's immediate source is not doing lineage at all.
DS_STAGING_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:snowflake,staging.transactions_clean,PROD)"
)

FEAT_VELOCITY_URN = "urn:li:mlFeature:(fraud_detection,transaction_velocity_7d)"
FEAT_RISK_URN = "urn:li:mlFeature:(fraud_detection,customer_risk_score)"

MODEL_URN = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detector_v3,PROD)"
PROP_BASELINE_URN = "urn:li:structuredProperty:undertow_baseline"


def get_emitter() -> DatahubRestEmitter:
    return DatahubRestEmitter(gms_server=GMS_URL, token=TOKEN)


def ensure_structured_property_def(emitter: DatahubRestEmitter) -> None:
    """Register the undertow_baseline structured property definition in DataHub."""
    prop_def = models.StructuredPropertyDefinitionClass(
        qualifiedName="undertow.baseline",
        displayName="Undertow Baseline Snapshot",
        valueType="urn:li:dataType:datahub.string",
        entityTypes=["urn:li:entityType:datahub.mlModel"],
        cardinality="SINGLE",
    )
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(entityUrn=PROP_BASELINE_URN, aspect=prop_def)
    )


def seed_datasets(emitter: DatahubRestEmitter) -> None:
    print("Emitting upstream datasets...")

    # 1. transactions.raw (5 columns)
    schema_txn = models.SchemaMetadataClass(
        schemaName="transactions.raw",
        platform=builder.make_data_platform_urn("snowflake"),
        version=0,
        hash="",
        platformSchema=models.OtherSchemaClass(rawSchema=""),
        fields=[
            models.SchemaFieldClass(
                fieldPath="transaction_id",
                type=models.SchemaFieldDataTypeClass(type=models.StringTypeClass()),
                nativeDataType="VARCHAR(64)",
                nullable=False,
                description="Unique transaction ID",
            ),
            models.SchemaFieldClass(
                fieldPath="customer_id",
                type=models.SchemaFieldDataTypeClass(type=models.StringTypeClass()),
                nativeDataType="VARCHAR(64)",
                nullable=False,
                description="Customer ID",
            ),
            models.SchemaFieldClass(
                fieldPath="transaction_amount",
                type=models.SchemaFieldDataTypeClass(type=models.NumberTypeClass()),
                nativeDataType="DECIMAL(10,2)",
                nullable=False,
                description="Transaction amount in USD",
            ),
            models.SchemaFieldClass(
                fieldPath="merchant_id",
                type=models.SchemaFieldDataTypeClass(type=models.StringTypeClass()),
                nativeDataType="VARCHAR(64)",
                nullable=True,
                description="Merchant ID",
            ),
            models.SchemaFieldClass(
                fieldPath="timestamp",
                type=models.SchemaFieldDataTypeClass(type=models.TimeTypeClass()),
                nativeDataType="TIMESTAMP_NTZ",
                nullable=False,
                description="Transaction timestamp",
            ),
        ],
    )
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(entityUrn=DS_TXN_URN, aspect=schema_txn)
    )

    profile_txn = models.DatasetProfileClass(
        timestampMillis=int(time.time() * 1000),
        rowCount=10000,
        columnCount=5,
        fieldProfiles=[
            models.DatasetFieldProfileClass(
                fieldPath="transaction_amount",
                nullCount=0,
                nullProportion=0.0,
                min="1.00",
                max="5000.00",
                mean="125.50",
                stdev="45.20",
            )
        ],
    )
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(entityUrn=DS_TXN_URN, aspect=profile_txn)
    )

    # 2. customers.raw (5 columns)
    schema_cust = models.SchemaMetadataClass(
        schemaName="customers.raw",
        platform=builder.make_data_platform_urn("snowflake"),
        version=0,
        hash="",
        platformSchema=models.OtherSchemaClass(rawSchema=""),
        fields=[
            models.SchemaFieldClass(
                fieldPath="customer_id",
                type=models.SchemaFieldDataTypeClass(type=models.StringTypeClass()),
                nativeDataType="VARCHAR(64)",
                nullable=False,
                description="Customer ID",
            ),
            models.SchemaFieldClass(
                fieldPath="signup_date",
                type=models.SchemaFieldDataTypeClass(type=models.DateTypeClass()),
                nativeDataType="DATE",
                nullable=False,
                description="Customer signup date",
            ),
            models.SchemaFieldClass(
                fieldPath="credit_score",
                type=models.SchemaFieldDataTypeClass(type=models.NumberTypeClass()),
                nativeDataType="INT",
                nullable=True,
                description="Credit score",
            ),
            models.SchemaFieldClass(
                fieldPath="country_code",
                type=models.SchemaFieldDataTypeClass(type=models.StringTypeClass()),
                nativeDataType="VARCHAR(2)",
                nullable=True,
                description="ISO Country code",
            ),
            models.SchemaFieldClass(
                fieldPath="risk_level",
                type=models.SchemaFieldDataTypeClass(type=models.StringTypeClass()),
                nativeDataType="VARCHAR(16)",
                nullable=True,
                description="Evaluated risk level",
            ),
        ],
    )
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(entityUrn=DS_CUST_URN, aspect=schema_cust)
    )

    profile_cust = models.DatasetProfileClass(
        timestampMillis=int(time.time() * 1000),
        rowCount=5000,
        columnCount=5,
        fieldProfiles=[
            models.DatasetFieldProfileClass(
                fieldPath="credit_score",
                nullCount=50,
                nullProportion=0.01,
                min="300",
                max="850",
                mean="710.0",
                stdev="55.0",
            )
        ],
    )
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(entityUrn=DS_CUST_URN, aspect=profile_cust)
    )


def seed_staging(emitter: DatahubRestEmitter) -> None:
    """Emit the staging table and the raw -> staging lineage edge.

    Both levels are emitted: table-level `upstreams` so the traversal has an edge
    to walk, and `fineGrainedLineages` mapping
    `transactions.raw.transaction_amount -> staging.transactions_clean.amount`
    so the attribution can name the column rather than the table. The column-level
    map is the difference between "something upstream changed" and "this column,
    this path, this owner".
    """
    print("Emitting staging layer and column-level lineage...")

    schema_staging = models.SchemaMetadataClass(
        schemaName="staging.transactions_clean",
        platform=builder.make_data_platform_urn("snowflake"),
        version=0,
        hash="",
        platformSchema=models.OtherSchemaClass(rawSchema=""),
        fields=[
            models.SchemaFieldClass(
                fieldPath="transaction_id",
                type=models.SchemaFieldDataTypeClass(type=models.StringTypeClass()),
                nativeDataType="VARCHAR(64)",
                nullable=False,
                description="Unique transaction ID",
            ),
            models.SchemaFieldClass(
                fieldPath="customer_id",
                type=models.SchemaFieldDataTypeClass(type=models.StringTypeClass()),
                nativeDataType="VARCHAR(64)",
                nullable=False,
                description="Customer ID",
            ),
            models.SchemaFieldClass(
                fieldPath="amount",
                type=models.SchemaFieldDataTypeClass(type=models.NumberTypeClass()),
                nativeDataType="DECIMAL(10,2)",
                nullable=False,
                description="Cleaned transaction amount, derived from transactions.raw.transaction_amount",
            ),
            models.SchemaFieldClass(
                fieldPath="event_date",
                type=models.SchemaFieldDataTypeClass(type=models.DateTypeClass()),
                nativeDataType="DATE",
                nullable=False,
                description="Transaction date",
            ),
        ],
    )
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(entityUrn=DS_STAGING_URN, aspect=schema_staging)
    )

    now_ms = int(time.time() * 1000)
    audit = models.AuditStampClass(time=now_ms, actor=builder.make_user_urn("data_eng_tom"))

    upstream_lineage = models.UpstreamLineageClass(
        upstreams=[
            models.UpstreamClass(
                dataset=DS_TXN_URN,
                type=models.DatasetLineageTypeClass.TRANSFORMED,
                auditStamp=audit,
            )
        ],
        fineGrainedLineages=[
            models.FineGrainedLineageClass(
                upstreamType=models.FineGrainedLineageUpstreamTypeClass.FIELD_SET,
                downstreamType=models.FineGrainedLineageDownstreamTypeClass.FIELD,
                upstreams=[builder.make_schema_field_urn(DS_TXN_URN, "transaction_amount")],
                downstreams=[builder.make_schema_field_urn(DS_STAGING_URN, "amount")],
                transformOperation="CAST",
                confidenceScore=1.0,
            ),
            models.FineGrainedLineageClass(
                upstreamType=models.FineGrainedLineageUpstreamTypeClass.FIELD_SET,
                downstreamType=models.FineGrainedLineageDownstreamTypeClass.FIELD,
                upstreams=[builder.make_schema_field_urn(DS_TXN_URN, "customer_id")],
                downstreams=[builder.make_schema_field_urn(DS_STAGING_URN, "customer_id")],
                transformOperation="IDENTITY",
                confidenceScore=1.0,
            ),
        ],
    )
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(entityUrn=DS_STAGING_URN, aspect=upstream_lineage)
    )

    profile_staging = models.DatasetProfileClass(
        timestampMillis=now_ms,
        rowCount=10000,
        columnCount=4,
        fieldProfiles=[
            models.DatasetFieldProfileClass(
                fieldPath="amount",
                nullCount=0,
                nullProportion=0.0,
                min="1.00",
                max="5000.00",
                mean="125.50",
                stdev="45.20",
            )
        ],
    )
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(entityUrn=DS_STAGING_URN, aspect=profile_staging)
    )


def seed_dataset_ownership(emitter: DatahubRestEmitter) -> None:
    """Put a name on every upstream dataset.

    Attribution without an owner is half a finding — the engineer reading the
    report needs to know who to talk to, not just which table moved.
    """
    print("Emitting dataset ownership...")
    owner = models.OwnershipClass(
        owners=[
            models.OwnerClass(
                owner=builder.make_user_urn("data_eng_tom"),
                type=models.OwnershipTypeClass.TECHNICAL_OWNER,
            )
        ]
    )
    for urn in (DS_TXN_URN, DS_CUST_URN, DS_STAGING_URN):
        emitter.emit_mcp(MetadataChangeProposalWrapper(entityUrn=urn, aspect=owner))


def seed_features(emitter: DatahubRestEmitter) -> None:
    print("Emitting ML features...")

    # transaction_velocity_7d derives from the *staging* table, not the raw one.
    # That is what gives the demo a three-hop path:
    #   transactions.raw -> staging.transactions_clean -> feature -> model
    feat_vel_props = models.MLFeaturePropertiesClass(
        description="7-day rolling transaction velocity",
        dataType="CONTINUOUS",
        sources=[DS_STAGING_URN],
    )
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(entityUrn=FEAT_VELOCITY_URN, aspect=feat_vel_props)
    )

    # 2. customer_risk_score -> customers.raw
    feat_risk_props = models.MLFeaturePropertiesClass(
        description="Aggregated customer risk score",
        dataType="CONTINUOUS",
        sources=[DS_CUST_URN],
    )
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(entityUrn=FEAT_RISK_URN, aspect=feat_risk_props)
    )


def seed_model(emitter: DatahubRestEmitter) -> None:
    print("Emitting ML model...")

    model_props = models.MLModelPropertiesClass(
        description="SageMaker Fraud Detector Model v3",
        mlFeatures=[FEAT_VELOCITY_URN, FEAT_RISK_URN],
        trainingMetrics=[
            models.MLMetricClass(name="accuracy", value="0.95"),
            models.MLMetricClass(name="precision", value="0.92"),
        ],
    )
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(entityUrn=MODEL_URN, aspect=model_props)
    )

    ownership = models.OwnershipClass(
        owners=[
            models.OwnerClass(
                owner=builder.make_user_urn("data_eng_tom"),
                type=models.OwnershipTypeClass.TECHNICAL_OWNER,
            )
        ]
    )
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(entityUrn=MODEL_URN, aspect=ownership)
    )


def seed_baseline(emitter: DatahubRestEmitter) -> None:
    print("Emitting baseline snapshot to structured properties & local storage...")

    # Ensure property definition exists in DataHub
    ensure_structured_property_def(emitter)

    ds_txn_asset = AssetSnapshot(
        urn=DS_TXN_URN,
        entity_type="dataset",
        columns=(
            ColumnSnapshot(path="transaction_id", data_type="StringTypeClass", native_type="VARCHAR(64)", nullable=False),
            ColumnSnapshot(path="customer_id", data_type="StringTypeClass", native_type="VARCHAR(64)", nullable=False),
            ColumnSnapshot(path="transaction_amount", data_type="NumberTypeClass", native_type="DECIMAL(10,2)", nullable=False),
            ColumnSnapshot(path="merchant_id", data_type="StringTypeClass", native_type="VARCHAR(64)", nullable=True),
            ColumnSnapshot(path="timestamp", data_type="TimeTypeClass", native_type="TIMESTAMP_NTZ", nullable=False),
        ),
        profile=ProfileSnapshot(
            row_count=10000,
            column_count=5,
            fields=(
                FieldProfileSnapshot(
                    path="transaction_amount",
                    null_count=0,
                    null_proportion=0.0,
                    min="1.00",
                    max="5000.00",
                    mean="125.50",
                    stdev="45.20",
                ),
            ),
        ),
    )

    ds_staging_asset = AssetSnapshot(
        urn=DS_STAGING_URN,
        entity_type="dataset",
        columns=(
            ColumnSnapshot(path="transaction_id", data_type="StringTypeClass", native_type="VARCHAR(64)", nullable=False),
            ColumnSnapshot(path="customer_id", data_type="StringTypeClass", native_type="VARCHAR(64)", nullable=False),
            ColumnSnapshot(path="amount", data_type="NumberTypeClass", native_type="DECIMAL(10,2)", nullable=False),
            ColumnSnapshot(path="event_date", data_type="DateTypeClass", native_type="DATE", nullable=False),
        ),
        profile=ProfileSnapshot(
            row_count=10000,
            column_count=4,
            fields=(
                FieldProfileSnapshot(
                    path="amount",
                    null_count=0,
                    null_proportion=0.0,
                    min="1.00",
                    max="5000.00",
                    mean="125.50",
                    stdev="45.20",
                ),
            ),
        ),
        owners=("urn:li:corpuser:data_eng_tom",),
        feeds_features=(FEAT_VELOCITY_URN,),
    )

    ds_cust_asset = AssetSnapshot(
        urn=DS_CUST_URN,
        entity_type="dataset",
        columns=(
            ColumnSnapshot(path="customer_id", data_type="StringTypeClass", native_type="VARCHAR(64)", nullable=False),
            ColumnSnapshot(path="signup_date", data_type="DateTypeClass", native_type="DATE", nullable=False),
            ColumnSnapshot(path="credit_score", data_type="NumberTypeClass", native_type="INT", nullable=True),
            ColumnSnapshot(path="country_code", data_type="StringTypeClass", native_type="VARCHAR(2)", nullable=True),
            ColumnSnapshot(path="risk_level", data_type="StringTypeClass", native_type="VARCHAR(16)", nullable=True),
        ),
        profile=ProfileSnapshot(
            row_count=5000,
            column_count=5,
            fields=(
                FieldProfileSnapshot(
                    path="credit_score",
                    null_count=50,
                    null_proportion=0.01,
                    min="300",
                    max="850",
                    mean="710.0",
                    stdev="55.0",
                ),
            ),
        ),
        feeds_features=(FEAT_RISK_URN,),
    )

    feat_vel_asset = AssetSnapshot(urn=FEAT_VELOCITY_URN, entity_type="mlFeature")
    feat_risk_asset = AssetSnapshot(urn=FEAT_RISK_URN, entity_type="mlFeature")
    model_asset = AssetSnapshot(urn=MODEL_URN, entity_type="mlModel", owners=("data_eng_tom",))

    snapshot = UndertowSnapshot(
        model_urn=MODEL_URN,
        assets={
            DS_TXN_URN: ds_txn_asset,
            DS_STAGING_URN: ds_staging_asset,
            DS_CUST_URN: ds_cust_asset,
            FEAT_VELOCITY_URN: feat_vel_asset,
            FEAT_RISK_URN: feat_risk_asset,
            MODEL_URN: model_asset,
        },
        baseline_ref="v1.0.0-seed",
    )

    snapshot_json = snapshot.model_dump_json()

    # 1. Emit as structuredProperty "undertow_baseline" on MODEL_URN
    try:
        patch_builder = MLModelPatchBuilder(MODEL_URN)
        patch_builder.set_structured_property("undertow_baseline", snapshot_json)
        for patch_mcp in patch_builder.build():
            emitter.emit_mcp(patch_mcp)
    except Exception as exc:
        print(f"Warning: structuredProperty emit failed: {exc}", file=sys.stderr)

    # 2. Emit InstitutionalMemory link with full snapshot JSON
    inst_memory = models.InstitutionalMemoryClass(
        elements=[
            models.InstitutionalMemoryMetadataClass(
                url="undertow://baseline/v1",
                description=f"Baseline snapshot for fraud_detector_v3: {snapshot_json}",
                createStamp=models.AuditStampClass(
                    time=int(time.time() * 1000),
                    actor=builder.make_user_urn("undertow"),
                ),
            )
        ]
    )
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(entityUrn=MODEL_URN, aspect=inst_memory)
    )

    # 3. Save locally to .undertow/snapshots/
    os.makedirs(".undertow/snapshots", exist_ok=True)
    model_id = MODEL_URN.split(",")[-2] if "," in MODEL_URN else "fraud_detector_v3"
    with open(f".undertow/snapshots/{model_id}.json", "w", encoding="utf-8") as f:
        f.write(snapshot_json)


def main() -> None:
    print(f"Connecting to DataHub GMS at {GMS_URL}...")
    try:
        emitter = get_emitter()
        seed_datasets(emitter)
        seed_staging(emitter)
        seed_dataset_ownership(emitter)
        seed_features(emitter)
        seed_model(emitter)
        seed_baseline(emitter)
        print("Successfully seeded DataHub graph!")
        print(
            "  transactions.raw -> staging.transactions_clean -> "
            "transaction_velocity_7d -> fraud_detector_v3"
        )
    except Exception as exc:
        print(f"Error seeding DataHub: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
