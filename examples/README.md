# Sample Outputs

Every file here is **captured from a real run** against DataHub OSS v1.7.0
(`datahub docker quickstart`), not hand-written. They exist so the project can be
evaluated without standing up the stack.

Reproduce with:

```bash
datahub docker quickstart
make seed && make baseline
make check           # -> verdict-clear.txt
make break
make blast-radius    # -> verdict-blast-radius.txt
make check-write     # -> verdict-block.txt, github-pr-comment.md, and the aspects below
```

## The fixture

`make seed` builds a three-hop chain with column-level lineage, ending in **two
models owned by two different teams**:

```
transactions.raw                                   (@data_eng_tom)
└── staging.transactions_clean                     [DownstreamOf]
    ├── transaction_velocity_7d                    [DerivedFrom]
    │   └── fraud_detector_v3     (@ml_eng_alex)   [Consumes]
    │       └── deployment: fraud_detector_prod
    └── customer_txn_volume                        [DerivedFrom]
        └── churn_predictor_v1    (@ml_eng_priya)  [Consumes]

customers.raw ──DerivedFrom──> customer_risk_score ──Consumes──> fraud_detector_v3
```

`make break` drops `transaction_amount` from `transactions.raw` — three hops
above either model, which is the whole point: nothing model-local can see it.

## Files

| File | What it is | Exit |
|---|---|---|
| `verdict-clear.txt` | `check` on an unchanged graph. | **0** |
| `verdict-block.txt` | `check` after the column drop, with the full attribution path. | **1** |
| `verdict-blast-radius.txt` | The same drop, checked from *both* models. Two teams, one column. | **1** and **1** |
| `github-pr-comment.md` | What lands in the PR, generated via `GITHUB_STEP_SUMMARY`. | — |
| `datahub-writeback.json` | The aspects Undertow wrote, **read back out of GMS** to prove they landed. | — |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | proceed — CLEAR, or WARN without `--fail-on-warn` |
| 1 | blocked — the gate ran and said stop |
| 2 | error — the gate could not produce a verdict |

Two properties are deliberate here.

**1 and 2 are different.** A pipeline needs to distinguish "your upstream broke"
from "Undertow broke" — those demand opposite responses, and collapsing them into
one code makes a broken gate look like a working one.

**Undertow fails closed.** An unreachable GMS, a bad token, or a footprint that
resolves to nothing exits 2 — never 0. An empty result and a clean result are
indistinguishable to a machine, and only one of them is safe to deploy.

WARN exits 0 by default because a warning annotates a deploy rather than stopping
it. Teams who want the stricter reading pass `--fail-on-warn`, and that is an
explicit choice rather than a default they discover in an outage.

## What `verdict-blast-radius.txt` shows

One dropped column, checked from each model that depends on it. `fraud_detector_v3`
belongs to the team that owns the change. `churn_predictor_v1` does not — it reaches
`transactions.raw` through the same staging table, and its owner has no reason to be
watching a table three hops upstream of a feature they did not build.

Both exit 1. Neither team had to know the other existed; the graph did.

## What `datahub-writeback.json` proves

Read by querying GMS after the run, rather than by trusting the CLI's own output:

- **`globalTags`** — `urn:li:tag:undertow:blocked` on the model.
- **`structuredProperties`** — `undertow_risk_verdict` (`BLOCK`),
  `undertow_last_checked`, and `undertow_baseline` (the full snapshot, truncated
  here for readability). The baseline is what lets a fresh CI runner with no local
  cache diff against the last approved deploy.
- **`institutionalMemory`** — an audit link back to the run.
- **`assertionInfo`** — a native `CUSTOM` assertion, source `EXTERNAL`. DataHub
  does not schedule assertion evaluations itself; external evaluators run them and
  report results. Undertow is exactly that kind of evaluator, so verdicts land in
  the data-quality surface a team already watches.
- **`assertionRunEvent`** — `FAILURE`, carrying severity and counts. This is a
  *timeseries* aspect, so it does not appear on the entity snapshot; it was read
  from `getTimeseriesAspectValues`. Successive runs append to the assertion's
  history rather than overwriting it.

### The `probe_alpha` property is not a mistake

`structuredProperties` contains `probe_alpha`, which Undertow did not write. It was
left on the model by a separate experiment, and it is still there **after** Undertow
wrote three properties of its own.

That is the evidence behind the [OSS
contribution](../contrib/datahub-mlmodel-patch-builder/): Undertow writes structured
properties as `PATCH`es, so a property it has never heard of survives. A full-aspect
`UPSERT` — the only option DataHub ships for `mlModel`, since `datahub/specific/` has
no ML patch builder — would have destroyed it.

Mutation tools are gated off in DataHub OSS builds, so write-back goes through the
REST emitter rather than the MCP server. The MCP server is used for reads.
