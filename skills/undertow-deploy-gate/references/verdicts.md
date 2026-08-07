# Verdicts, findings, and confidence

Reference for `undertow-deploy-gate`. The severities below are the ones shipped
in this repository's `undertow.yaml`. A team may override any of them, so
`undertow policy show` is always authoritative — run it before telling anyone
what their gate will do.

## Exit codes

| Code | Verdict | Meaning |
| --- | --- | --- |
| `0` | CLEAR | Nothing material changed upstream. Also WARN, unless `--fail-on-warn`. |
| `1` | BLOCK | The gate ran and said stop. |
| `2` | — | No verdict could be produced. Not a pass. |

Undertow fails closed: an unreachable GMS, a bad token, or a footprint that
resolves to nothing exits `2` rather than reporting a green light it did not
earn. An empty result and a clean result are indistinguishable to a machine.

## Finding kinds

### Schema — read off `schemaMetadata`, therefore `CERTAIN`

| Kind | Shipped severity | Meaning |
| --- | --- | --- |
| `COLUMN_DROPPED` | BLOCK | A column the model's lineage depends on no longer exists. |
| `COLUMN_TYPE_CHANGED` | BLOCK | A type change that cannot hold every value the old type could. |
| `COLUMN_NULLABILITY_RELAXED` | WARN | A column that could not be null now can be. |
| `COLUMN_ADDED` | CLEAR | Recorded, not penalised. |

A type change is only a finding when it is *lossy*. Widening `INT` to `BIGINT`
is compatible; narrowing is not. A column whose type was previously unknown to
the profiler gaining a real type is the schema improving, not breaking.

### Governance — read off `deprecation`, `ownership`, `globalTags`, `CERTAIN`

| Kind | Shipped severity | Meaning |
| --- | --- | --- |
| `ASSET_DEPRECATED` | BLOCK | An upstream asset was marked deprecated. |
| `OWNERSHIP_LOST` | WARN | An upstream asset no longer has an owner — nobody to escalate to. |
| `NEW_SENSITIVE_TAG` | WARN | A sensitivity tag appeared upstream; the model may now carry restricted data. |

### Statistical — from DataHub's default profiling, therefore `PROBABLE`

| Kind | Shipped severity | Meaning |
| --- | --- | --- |
| `NULL_RATE_JUMP` | WARN | Null proportion moved materially. |
| `CARDINALITY_CHANGE` | WARN | Distinct-value count moved materially. |
| `MEAN_SHIFT` | WARN | Mean moved beyond the configured z-score. |
| `RANGE_VIOLATION` | WARN | Observed min/max left the baseline range. |
| `ROW_COUNT_CHANGE` | WARN | Row count moved materially. |

## CERTAIN vs PROBABLE

`CERTAIN` findings are facts read straight out of the graph: a column is present
or it is not. `PROBABLE` findings are statistical inferences.

**A `PROBABLE` finding cannot BLOCK under the default policy.** The policy engine
refuses to let one stop a deploy unless a team sets `allow_probable_block: true`
deliberately. A distribution moving is evidence something *may* be wrong, and
blocking on evidence that weak is how a gate loses its welcome and gets deleted.

When reporting, keep the distinction visible. "The mean of `amount` moved 3.2σ"
is honest. "The data is corrupted" is not.

## Statistical coverage

Undertow only computes statistics DataHub profiles by default. If a field has no
profile, that is reported as *cannot assess* — never as *no drift*.

`profile_coverage` on a verdict states how much of the footprint was actually
inspectable. A CLEAR verdict with low coverage is a weaker statement than a
CLEAR verdict with full coverage, and worth saying out loud:

> CLEAR — but only 2 of 6 upstream assets carry profiles, so statistical drift
> was assessable on a third of the footprint.

## Exemptions

`undertow.yaml` may exempt specific assets or finding kinds. Every exemption
must carry an expiry date — the policy fails validation without one, on the
grounds that a permanent exemption is just a rule nobody wrote down.

An exempted finding still appears in the report, marked with the exemption and
its expiry. It is suppressed, not hidden.
