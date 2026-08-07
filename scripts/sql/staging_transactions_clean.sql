-- Cleaned, typed transaction facts. One row per settled transaction.
--
-- This file is the source of truth for staging.transactions_clean. The seed
-- script does not describe this table's columns or its lineage anywhere else:
-- both are produced by running DataHub's SQL parser over the statement below,
-- the same parser the Snowflake and BigQuery connectors use in production.
--
-- Changing the SELECT list changes the catalog.

CREATE OR REPLACE TABLE staging.transactions_clean AS
SELECT
    transaction_id,
    customer_id,
    CAST(transaction_amount AS DECIMAL(10, 2)) AS amount,
    CAST(timestamp AS DATE)                    AS event_date
FROM transactions.raw
WHERE transaction_amount IS NOT NULL
