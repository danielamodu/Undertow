-- A sample pull request against the staging model.
--
-- Not part of the fixture: `make seed` never reads this file. It exists so
-- `undertow impact` has something to check, the way a reviewer would run it
-- against the SQL a PR actually changed:
--
--     undertow impact examples/pr-drops-amount.sql
--
-- The change looks harmless in isolation — one column removed from a SELECT.
-- `amount` feeds two features owned by two teams, and neither of them is
-- reviewing this diff.

CREATE OR REPLACE TABLE staging.transactions_clean AS
SELECT
    transaction_id,
    customer_id,
    CAST(timestamp AS DATE) AS event_date
FROM transactions.raw
WHERE transaction_amount IS NOT NULL
