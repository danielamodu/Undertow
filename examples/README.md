# Sample Outputs

Every file here is **captured from a real run** against DataHub OSS v1.7.0
(`datahub docker quickstart`), not hand-written. They exist so the project can be
evaluated without standing up the stack.

Reproduce with:

```bash
datahub docker quickstart
make seed && make baseline
make check          # -> verdict-clear.txt
make break
make check-write    # -> verdict-block.txt + the DataHub aspects below
```

## The fixture

`make seed` builds a three-hop chain with column-level lineage:

```
transactions.raw ──DownstreamOf──> staging.transactions_clean ──DerivedFrom──> transaction_velocity_7d ──Consumes──> fraud_detector_v3
       │                                    │
       └─ transaction_amount ───────────────┴─ amount        (fineGrainedLineage, CAST)

customers.raw ──DerivedFrom──> customer_risk_score ──Consumes──> fraud_detector_v3
```

`make break` drops `transaction_amount` from `transactions.raw` — three hops
above the model, which is the whole point: nothing model-local can see it.

## Files

| File | What it is |
|---|---|
| `verdict-clear.txt` | `check` on an unchanged graph. Exit **0**. |
| `verdict-block.txt` | `check` after the column drop. Exit **2**, with the full attribution path. |
| `github-pr-comment.md` | What lands in the PR, generated via `GITHUB_STEP_SUMMARY`. |
| `datahub-writeback.json` | The aspects Undertow wrote, **read back out of GMS** to prove they landed. |

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

## What `datahub-writeback.json` proves

Four surfaces, all verified by querying GMS after the run rather than by trusting
the CLI's own output:

- **`globalTags`** — `urn:li:tag:undertow:blocked` on the model.
- **`structuredProperties`** — `undertow_risk_verdict` (`BLOCK`),
  `undertow_last_checked`, and `undertow_baseline` (the full snapshot, truncated
  here for readability). The baseline is what lets a fresh CI runner with no local
  cache diff against the last approved deploy.
- **`assertionInfo`** — a native `CUSTOM` assertion, source `EXTERNAL`. DataHub
  does not schedule assertion evaluations itself; external evaluators run them and
  report results. Undertow is exactly that kind of evaluator, so verdicts land in
  the data-quality surface a team already watches.

Mutation tools are gated off in DataHub OSS builds, so write-back goes through the
REST emitter rather than the MCP server. The MCP server is used for reads.
