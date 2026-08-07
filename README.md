# Undertow

**A lineage-grounded pre-deploy gate for production ML models, built on DataHub.**

The surface looks fine. The model keeps serving predictions. Underneath, something changed upstream days ago and nobody connected the two.

Drift monitors tell you *that* a feature moved. Undertow tells you *what changed upstream, who changed it, and whether to ship* — before the deploy lands.

```
🔴 BLOCK — model:fraud_detector_v3 (2 blocking, 1 warning)

┌─ BLOCKING ────────────────────────────────────────────────
│ Feature `avg_txn_30d` — upstream column removed
│
│   raw.payments.amount_usd        ← DROPPED in PR #402 (@data-eng-tom)
│     └→ staging.payments_clean.amount
│        └→ features.txn_aggregates.avg_txn_30d
│           └→ mlModel:fraud_detector_v3
│
│   Confidence: CERTAIN (schema diff)
└───────────────────────────────────────────────────────────
```

---

## What's actually new here

**Start with what isn't.** DataHub already ships lineage impact analysis — it can tell you which models sit downstream of a changed table, with column-level granularity, ownership, and subscriptions. Databricks Unity Catalog ships table→model lineage too. Undertow composes those primitives rather than reinventing them, and that composition is the point.

Two mature capabilities exist and have never been joined:

| | Answers | Ships in |
|---|---|---|
| Lineage graphs | *"Which models are downstream of this table?"* | DataHub, Unity Catalog |
| Drift monitors | *"Did this feature's distribution change?"* | Evidently, Arize, WhyLabs |

**Nobody joins them.** Taking a measured drift signal and attributing it to a specific upstream change by walking the graph is proposed in the literature ([arXiv 2510.23528](https://arxiv.org/abs/2510.23528)) and implemented by no shipping tool. Today it's a manual cross-team investigation — [arXiv 2510.24142](https://arxiv.org/abs/2510.24142) documents practitioners tracing backwards through their systems by hand.

Undertow's three claims, in descending strength:

1. **The join.** Drift statistic × lineage graph = causal attribution.
2. **It gates the model, not the table.** Datafold, Recce, and Grai all gate *data* PRs. Nothing gates an *ML model deploy* on upstream lineage risk.
3. **Pre-deploy, not post-incident.** Monte Carlo root-causes after the fact. Datafold checks before a table merge. Before a *model* ships is the empty cell.

Feature stores don't close this: Feast, Tecton, and Hopsworks all build provenance graphs that **start at the feature-store boundary**, with no table or column nodes upstream — so a warehouse column change is invisible to all three. Feast says so outright and delegates lineage to DataHub.

**DataHub informs. Undertow decides.**

---

## How it works

Four steps, run as a CI gate before a model deploy:

**1 · Resolve** — walk the graph from a model URN:

```
mlModel --Consumes--> mlFeature --DerivedFrom--> dataset --upstream--> …
```

Two hops to reach data, not three. Feature tables are fetched for display but sit *outside* the lineage graph — their `Contains` relationship isn't flagged `isLineage` in DataHub's entity registry.

**2 · Diff** — compare every asset in that footprint against the last approved deploy: schema deltas, distribution deltas, governance deltas.

**3 · Attribute & rule** — produce the lineage path from root cause to affected feature, then apply a deterministic risk policy.

**4 · Write back** — post the verdict to the PR *and* to DataHub, as a native data-quality assertion. The next engineer inherits the reasoning instead of re-deriving it.

### The load-bearing design decision

**Deterministic code decides. The LLM only explains.**

Severity is computed by a rule engine over structured diffs — never by a language model. The LLM receives already-decided facts and turns them into prose. A judge cannot talk the gate into a wrong verdict with a clever input, and the whole core is unit-testable.

The same discipline governs confidence:

| Confidence | Source | Can block? |
|---|---|---|
| `CERTAIN` | Read off the graph — a column is present or it isn't | Yes |
| `PROBABLE` | Statistical inference | **No**, unless explicitly opted in |

Conflating those two is how monitoring tools train people to ignore alerts. Here it's enforced in code: a `PROBABLE` finding cannot produce a `BLOCK` even if a policy file asks for one.

---

## Status

Design and deterministic core are complete and tested. Integration with a live DataHub instance is in progress.

- ✅ Domain model, policy engine, `undertow.yaml` — pure, no I/O, unit-tested
- ✅ CLI skeleton — `undertow policy validate` / `policy show` work today
- 🚧 DataHub resolver (MCP + SDK fallback), differs, reporter, CI action

`undertow resolve` and `undertow check` are declared but exit 2 (*Undertow failed*), never 0. A stub that exits 0 would turn CI green on a gate that never ran, which is worse than no gate at all.

Every DataHub API path used here was verified against source before being designed around — including one correction that would otherwise have cost a day: the `mlModel → mlFeatureTable → mlFeature` traversal most people reach for **doesn't work**, because feature tables aren't on the lineage graph.

See [`docs/`](docs/) for the product design, system architecture, and build plan.

---

## Development

```bash
pip install -e ".[dev]"
```

```bash
pytest
```

Inspect the shipped risk policy without a DataHub instance:

```bash
undertow policy show
```

The policy engine has no external dependencies and needs no DataHub instance to test.

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
