"""Seed DataHub with a realistic ML lineage graph and baseline snapshot.

Derived tables are not described here. `scripts/sql/` holds the SQL that builds
them, and DataHub's own SQL parser — `sqlglot_lineage`, the one the Snowflake
and BigQuery connectors run in production — produces both the output schema and
the column-level lineage from those statements.

That distinction is the point. A fixture whose lineage is hand-asserted proves
only that Undertow can read aspects someone wrote by hand to make the demo work.
A fixture whose lineage is parsed out of `CAST(transaction_amount AS ...) AS
amount` proves the column-level path Undertow attributes a failure along is the
one the SQL actually creates. Only source tables are declared directly, because
in a real catalog those arrive from the source system rather than from a query.
"""

import os
import pathlib
import sys
import time

import datahub.emitter.mce_builder as builder
import datahub.metadata.schema_classes as models
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.sql_parsing.schema_resolver import SchemaResolver
from datahub.sql_parsing.sqlglot_lineage import (
    SqlParsingResult,
    infer_output_schema,
    sqlglot_lineage,
)

from undertow.models import (
    AssetSnapshot,
    ColumnSnapshot,
    FieldProfileSnapshot,
    ProfileSnapshot,
    UndertowSnapshot,
)
from undertow.reporter.datahub_writer import MLModelPatchBuilder

SQL_DIR = pathlib.Path(__file__).parent / "sql"
PLATFORM = "snowflake"
ENV = "PROD"

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
# Owned by a different team, built off the same staging table. This is what makes
# the blast radius real: one dropped column reaches two models nobody has
# introduced to each other.
FEAT_VOLUME_URN = "urn:li:mlFeature:(customer_churn,customer_txn_volume)"

MODEL_URN = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detector_v3,PROD)"
MODEL_CHURN_URN = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,churn_predictor_v1,PROD)"

# The bookends of the challenge's "training data -> features -> models ->
# deployments" chain. Undertow gates a deploy, so the deployment being a real
# entity in the graph rather than a figure of speech is the point.
MODEL_GROUP_URN = "urn:li:mlModelGroup:(urn:li:dataPlatform:sagemaker,fraud_detection,PROD)"
DEPLOYMENT_URN = (
    "urn:li:mlModelDeployment:(urn:li:dataPlatform:sagemaker,fraud_detector_prod,PROD)"
)

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


# Source tables. These are declared rather than parsed because that is how they
# arrive in a real catalog: an ingestion run reads them out of the warehouse.
# Everything downstream of here is derived from SQL instead.
TXN_FIELDS = [
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
]


CUST_FIELDS = [
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
]


def make_schema(name: str, fields: list) -> models.SchemaMetadataClass:
    return models.SchemaMetadataClass(
        schemaName=name,
        platform=builder.make_data_platform_urn(PLATFORM),
        version=0,
        hash="",
        platformSchema=models.OtherSchemaClass(rawSchema=""),
        fields=fields,
    )


def seed_datasets(emitter: DatahubRestEmitter) -> None:
    print("Emitting source datasets...")

    # 1. transactions.raw (5 columns)
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(
            entityUrn=DS_TXN_URN, aspect=make_schema("transactions.raw", TXN_FIELDS)
        )
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
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(
            entityUrn=DS_CUST_URN, aspect=make_schema("customers.raw", CUST_FIELDS)
        )
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


def source_schemas() -> dict[str, models.SchemaMetadataClass]:
    """The schemas the parser can bind columns against, keyed by URN.

    Without schemas the parser still finds table-level lineage, but `SELECT *`
    and unqualified columns cannot be attributed to a source table — and
    column-level attribution is the entire product. Reusing the same field lists
    that get emitted keeps the parse grounded in the catalog rather than in a
    second, drifting description of it.
    """
    return {
        DS_TXN_URN: make_schema("transactions.raw", TXN_FIELDS),
        DS_CUST_URN: make_schema("customers.raw", CUST_FIELDS),
    }


def make_resolver(schemas: dict[str, models.SchemaMetadataClass]) -> SchemaResolver:
    """A fresh resolver over `schemas`.

    Fresh, not mutated in place. `sqlglot_lineage` memoises on the resolver
    object, so adding a schema to one already used for a parse yields a cache
    hit and the previous, less-informed answer — silently, with no error and a
    confidence score that never improves.
    """
    resolver = SchemaResolver(platform=PLATFORM, env=ENV)
    for urn, schema in schemas.items():
        resolver.add_schema_metadata(urn, schema)
    return resolver


def parse_transform(
    sql_file: str, schemas: dict[str, models.SchemaMetadataClass]
) -> tuple[str, SqlParsingResult]:
    """Run DataHub's SQL parser over one transform. Fails loudly.

    Parsed twice on purpose. The first pass knows the source schemas but not the
    table the statement creates, and the parser discounts its own confidence
    accordingly — 0.35 here. Feeding the inferred output schema back in and
    re-parsing gives it both ends of the mapping, and takes the same four column
    edges to 0.9.

    A production connector gets this for free: the output table was ingested
    before its queries were parsed. A fixture that builds the table from the
    query has to close the loop itself, and the alternative is publishing
    column-level lineage the parser openly says it is unsure about.

    `schemas` is mutated with the inferred output schema, so a later transform
    reading this table parses against it too.
    """
    sql = (SQL_DIR / sql_file).read_text(encoding="utf-8")

    def parse() -> SqlParsingResult:
        result = sqlglot_lineage(sql, schema_resolver=make_resolver(schemas))
        if result.debug_info.error:
            raise RuntimeError(f"{sql_file}: SQL parse failed — {result.debug_info.error}")
        if not result.out_tables:
            raise RuntimeError(f"{sql_file}: parsed no output table")
        return result

    first = parse()
    out_urn = first.out_tables[0]
    out_fields = infer_output_schema(first)
    if out_fields:
        schemas[out_urn] = make_schema(out_urn.split(",")[-2], out_fields)

    result = parse()
    if not result.column_lineage:
        raise RuntimeError(
            f"{sql_file}: parsed no column lineage. Table-level edges alone cannot "
            "name a column, and naming the column is the point."
        )
    return sql, result


def emit_transform(
    emitter: DatahubRestEmitter, sql: str, result: SqlParsingResult, *, actor: str
) -> str:
    """Emit the schema, lineage, and query entity implied by one SQL statement.

    Everything here comes out of the parse. Nothing about the output table's
    columns, or which upstream column feeds which downstream one, is written
    down a second time.
    """
    out_urn = result.out_tables[0]
    now_ms = int(time.time() * 1000)
    audit = models.AuditStampClass(time=now_ms, actor=builder.make_user_urn(actor))

    # The query entity, so the statement is inspectable in the catalog rather
    # than only in this repository. Fine-grained edges point back at it.
    query_urn = f"urn:li:query:undertow-{result.query_fingerprint}"
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(
            entityUrn=query_urn,
            aspect=models.QueryPropertiesClass(
                statement=models.QueryStatementClass(
                    value=sql, language=models.QueryLanguageClass.SQL
                ),
                source=models.QuerySourceClass.SYSTEM,
                created=audit,
                lastModified=audit,
            ),
        )
    )
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(
            entityUrn=query_urn,
            aspect=models.QuerySubjectsClass(
                subjects=[
                    models.QuerySubjectClass(entity=urn)
                    for urn in [out_urn, *result.in_tables]
                ]
            ),
        )
    )

    # Output schema, inferred from the SELECT list and the source column types.
    fields = infer_output_schema(result)
    if not fields:
        raise RuntimeError(f"could not infer an output schema for {out_urn}")
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(
            entityUrn=out_urn,
            aspect=make_schema(out_urn.split(",")[-2], fields),
        )
    )

    # Table-level edges give the traversal something to walk; the fine-grained
    # edges let attribution name a column instead of a table.
    fine_grained = [
        models.FineGrainedLineageClass(
            upstreamType=models.FineGrainedLineageUpstreamTypeClass.FIELD_SET,
            downstreamType=models.FineGrainedLineageDownstreamTypeClass.FIELD,
            upstreams=[
                builder.make_schema_field_urn(up.table, up.column) for up in cl.upstreams
            ],
            downstreams=[builder.make_schema_field_urn(out_urn, cl.downstream.column)],
            # Taken from the parse, not asserted: a column copied straight
            # through is a different risk from one the SQL transforms.
            transformOperation=(
                "IDENTITY" if cl.logic and cl.logic.is_direct_copy else "TRANSFORMED"
            ),
            confidenceScore=result.debug_info.confidence,
            query=query_urn,
        )
        for cl in (result.column_lineage or [])
        if cl.upstreams
    ]

    emitter.emit_mcp(
        MetadataChangeProposalWrapper(
            entityUrn=out_urn,
            aspect=models.UpstreamLineageClass(
                upstreams=[
                    models.UpstreamClass(
                        dataset=up_urn,
                        type=models.DatasetLineageTypeClass.TRANSFORMED,
                        auditStamp=audit,
                        query=query_urn,
                    )
                    for up_urn in result.in_tables
                ],
                fineGrainedLineages=fine_grained,
            ),
        )
    )

    print(
        f"  parsed {len(fine_grained)} column edges into {out_urn.split(',')[-2]} "
        f"from {len(result.in_tables)} upstream table(s)"
    )
    return out_urn


def seed_transforms(emitter: DatahubRestEmitter) -> dict[str, list]:
    """Build every derived table by parsing the SQL that defines it.

    Returns the inferred schema per derived table so the baseline can be built
    from the same parse. Writing those columns out a second time by hand is how
    a seeded baseline ends up disagreeing with the graph it was seeded from.
    """
    print("Parsing scripts/sql/ and emitting derived tables...")
    schemas = source_schemas()

    sql, result = parse_transform("staging_transactions_clean.sql", schemas)
    out_urn = emit_transform(emitter, sql, result, actor="data_eng_tom")
    if out_urn != DS_STAGING_URN:
        raise RuntimeError(
            f"SQL produced {out_urn}, but the fixture expects {DS_STAGING_URN}. "
            "The demo's URNs and the SQL have drifted apart."
        )

    emitter.emit_mcp(
        MetadataChangeProposalWrapper(
            entityUrn=DS_STAGING_URN,
            aspect=models.DatasetProfileClass(
                timestampMillis=int(time.time() * 1000),
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
            ),
        )
    )

    return {DS_STAGING_URN: infer_output_schema(result) or []}


def to_column_snapshots(fields: list) -> tuple:
    """SchemaFieldClass -> ColumnSnapshot, matching what the resolver captures."""
    return tuple(
        ColumnSnapshot(
            path=f.fieldPath,
            data_type=type(f.type.type).__name__ if f.type else "unknown",
            native_type=f.nativeDataType,
            nullable=bool(f.nullable),
        )
        for f in fields
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

    # 3. customer_txn_volume -> staging.transactions_clean
    #
    # A different team's feature off the same staging table. The churn team has
    # never spoken to the fraud team and neither knows they share an upstream.
    # That is the situation the gate exists for.
    feat_volume_props = models.MLFeaturePropertiesClass(
        description="30-day customer transaction volume",
        dataType="CONTINUOUS",
        sources=[DS_STAGING_URN],
    )
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(entityUrn=FEAT_VOLUME_URN, aspect=feat_volume_props)
    )


def seed_model_group_and_deployment(emitter: DatahubRestEmitter) -> None:
    """The two ends of the chain the Production ML Agents challenge describes.

    "The path from training data to features to models to deployments." Without
    these the fixture stops at the model, and Undertow gating "a deployment" is
    a figure of speech rather than something you can point at in the catalog.
    """
    print("Emitting ML model group and deployment...")

    group_props = models.MLModelGroupPropertiesClass(
        name="fraud_detection",
        description="Fraud detection model family. v3 is the live version.",
    )
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(entityUrn=MODEL_GROUP_URN, aspect=group_props)
    )

    deployment_props = models.MLModelDeploymentPropertiesClass(
        description="Production SageMaker endpoint serving fraud_detector_v3.",
        version=models.VersionTagClass(versionTag="v3"),
        status=models.DeploymentStatusClass.IN_SERVICE,
    )
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(entityUrn=DEPLOYMENT_URN, aspect=deployment_props)
    )


def seed_model(emitter: DatahubRestEmitter) -> None:
    print("Emitting ML models...")

    model_props = models.MLModelPropertiesClass(
        description="SageMaker Fraud Detector Model v3",
        mlFeatures=[FEAT_VELOCITY_URN, FEAT_RISK_URN],
        # Closes the chain: this model belongs to a family and is serving behind
        # a named deployment, both resolvable in the graph.
        groups=[MODEL_GROUP_URN],
        deployments=[DEPLOYMENT_URN],
        trainingMetrics=[
            models.MLMetricClass(name="accuracy", value="0.95"),
            models.MLMetricClass(name="precision", value="0.92"),
        ],
    )
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(entityUrn=MODEL_URN, aspect=model_props)
    )

    emitter.emit_mcp(
        MetadataChangeProposalWrapper(
            entityUrn=MODEL_URN,
            aspect=models.OwnershipClass(
                owners=[
                    models.OwnerClass(
                        owner=builder.make_user_urn("ml_eng_alex"),
                        type=models.OwnershipTypeClass.TECHNICAL_OWNER,
                    )
                ]
            ),
        )
    )

    # A second model, a different team, the same upstream table.
    churn_props = models.MLModelPropertiesClass(
        description="Customer churn predictor v1",
        mlFeatures=[FEAT_VOLUME_URN],
        trainingMetrics=[models.MLMetricClass(name="auc", value="0.88")],
    )
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(entityUrn=MODEL_CHURN_URN, aspect=churn_props)
    )

    emitter.emit_mcp(
        MetadataChangeProposalWrapper(
            entityUrn=MODEL_CHURN_URN,
            aspect=models.OwnershipClass(
                owners=[
                    models.OwnerClass(
                        owner=builder.make_user_urn("ml_eng_priya"),
                        type=models.OwnershipTypeClass.TECHNICAL_OWNER,
                    )
                ]
            ),
        )
    )


def seed_baseline(emitter: DatahubRestEmitter, derived_fields: dict[str, list]) -> None:
    print("Emitting baseline snapshot to structured properties & local storage...")

    # Ensure property definition exists in DataHub
    ensure_structured_property_def(emitter)

    ds_txn_asset = AssetSnapshot(
        urn=DS_TXN_URN,
        entity_type="dataset",
        columns=to_column_snapshots(TXN_FIELDS),
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
        # From the SQL parse, not restated here — see seed_transforms.
        columns=to_column_snapshots(derived_fields[DS_STAGING_URN]),
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
        columns=to_column_snapshots(CUST_FIELDS),
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
        derived_fields = seed_transforms(emitter)
        seed_dataset_ownership(emitter)
        seed_features(emitter)
        seed_model_group_and_deployment(emitter)
        seed_model(emitter)
        seed_baseline(emitter, derived_fields)
        print("\nSuccessfully seeded DataHub graph.\n")
        print("  transactions.raw")
        print("    └─ staging.transactions_clean          [lineage parsed from SQL]")
        print("       ├─ transaction_velocity_7d  -> fraud_detector_v3   (@ml_eng_alex)")
        print("       └─ customer_txn_volume      -> churn_predictor_v1  (@ml_eng_priya)")
        print()
        print("  fraud_detector_v3 -> group: fraud_detection | deployment: fraud_detector_prod")
        print()
        print("  One column drop in transactions.raw reaches both models.")
    except Exception as exc:
        print(f"Error seeding DataHub: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
