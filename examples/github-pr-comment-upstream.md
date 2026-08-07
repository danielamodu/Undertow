## 🔴 Undertow: this PR removes columns that production models depend on

### `staging.transactions_clean` — drops `amount`

Defined by `examples\pr-drops-amount.sql`.

| Model | Owner | Reached via |
| --- | --- | --- |
| `churn_predictor_v1` | @ml_eng_priya | customer_txn_volume → churn_predictor_v1 |
| `fraud_detector_v3` | @ml_eng_alex | transaction_velocity_7d → fraud_detector_v3 |

These models are gated on the columns above. Merging this will block their next deploy until their baselines are re-approved.

---
*[Undertow](https://github.com/danielamodu/Undertow) — checked before merge, not after deploy*
