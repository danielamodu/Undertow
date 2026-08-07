"""Simulate a breaking schema change by dropping transaction_amount from transactions.raw."""

import os
import sys

import datahub.emitter.mce_builder as builder
import datahub.metadata.schema_classes as models
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter

GMS_URL = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
TOKEN = os.environ.get("DATAHUB_GMS_TOKEN")

DS_TXN_URN = "urn:li:dataset:(urn:li:dataPlatform:snowflake,transactions.raw,PROD)"


def main() -> None:
    print(f"Simulating breaking schema change on {DS_TXN_URN}...")
    emitter = DatahubRestEmitter(gms_server=GMS_URL, token=TOKEN)

    # Re-emit schema WITHOUT transaction_amount column (dropped column)
    broken_schema = models.SchemaMetadataClass(
        schemaName="transactions.raw",
        platform=builder.make_data_platform_urn("snowflake"),
        version=1,
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
            # transaction_amount is DROPPED!
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

    try:
        emitter.emit_mcp(
            MetadataChangeProposalWrapper(entityUrn=DS_TXN_URN, aspect=broken_schema)
        )
        print("Successfully dropped column `transaction_amount` from `transactions.raw`!")
    except Exception as exc:
        print(f"Error emitting broken schema: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
