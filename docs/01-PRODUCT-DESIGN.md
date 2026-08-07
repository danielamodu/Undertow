# Undertow — Product Design

> **The lineage-grounded pre-deploy gate for production ML.**
> Drift tools tell you *that* a model broke. Undertow tells you *what changed upstream, who changed it, and whether to ship* — before the deploy lands.

---

## 1. The Problem

Production ML models fail silently, and they fail from **upstream**.

A data engineer renames a column in `raw.payments`. Three hops downstream, a feature called `avg_txn_30d` starts computing over nulls. The fraud model keeps serving predictions — confidently, and wrong — for eleven days until someone notices the chargeback numbers.

Nobody did anything wrong. The data engineer had no idea a model consumed that table. The ML engineer had no idea the table changed. **The knowledge existed in the organisation; it just wasn't in the room at the moment of the decision.**

### Why existing tools don't solve this

| Tool class | What it does | Why it fails here |
|---|---|---|
| Drift monitors (Evidently, WhyLabs, Arize, NannyML) | Detect distribution shift in model inputs | **Model-local — the graph stops at the model boundary.** They tell you `avg_txn_30d` drifted. They cannot tell you it drifted because of PR #402 on `raw.payments`, because they have never heard of `raw.payments`. Arize's Performance Tracing is the strongest RCA here and it operates entirely inside the model's own feature data. |
| Data quality tools (Great Expectations, Soda, Monte Carlo, Anomalo) | Assert conditions on tables; detect anomalies | **Model-blind.** They know the table changed. Monte Carlo will even root-cause it to an upstream commit — but post-incident, and its ML leg is thin. Nothing here knows a model depends on the table, or blocks a model deploy. |
| CI data-diff gates (Datafold, Recce, Grai) | Column lineage + value diff, posted on every PR | **Right mechanism, wrong subject.** These gate the *data* PR. Nobody gates the *model deploy*. Datafold surfaces downstream ML models as generic "data app assets" and has no feature-store connectors, so feature engineering outside SQL is invisible. |
| Warehouse-native lineage (Databricks Unity Catalog) | Table→model lineage, column-level, MLflow-integrated | **Primitives ship; the gate is DIY.** Their docs explicitly suggest querying lineage system tables to "build automated impact analysis for schema changes." That's an invitation, not a product. Bounded to Databricks. |
| Feature stores (Feast, Tecton, Hopsworks) | Feature group → view → training set → model provenance | **Every graph starts at the feature-store boundary** — no table or column nodes, so an upstream warehouse change is invisible. Feast says so outright and delegates lineage to DataHub. Hopsworks stops at the storage connector and needs FG→FG edges hand-declared. Tecton's `prevent_destroy` is the only real gate in the category, and it's a static per-object flag, not computed impact. |
| **DataHub itself** | Holds the full graph — ML entities, column-level lineage, ownership, impact analysis, subscriptions | **This is the closest thing that exists, and it is deliberately investigative.** DataHub already answers "which models are downstream of this table." What it does not do is *join that to a measured drift signal*, or *block a deploy*. It informs; it does not decide. |

**The gap is narrower and sharper than "nobody does lineage."** Two mature capabilities exist and have never been joined:

- **Lineage graphs** answer *"which models are downstream of this table?"* — DataHub and Unity Catalog both ship this.
- **Drift monitors** answer *"did this feature's distribution change?"* — Evidently and Arize both ship this.

Nobody takes a drift signal and attributes it to a specific upstream change by walking the graph. And nobody enforces the result as a **blocking gate on the model deploy** — every shipped CI gate gates the *data* PR instead.

Today that join is a manual, cross-team investigation: an ML engineer notices drift, Slacks a data engineer, someone eventually finds the commit. This is documented, not assumed — a 2025 study of monitoring practice (arXiv 2510.24142) records practitioners "trac[ing] backwards through their systems to identify what changed upstream" by hand, and arXiv 2510.23528 proposes exactly this attribution framing as future work, with no implementation and an explicit concession that it *assumes* a lineage graph rather than providing one.

DataHub provides the graph. Undertow is the layer that walks it, joins it to a measured signal, and turns the result into a verdict.

---

## 2. The Insight

> **Drift detection is a statistics problem. Drift *attribution* is a graph problem.**

Every drift tool on the market solves the statistics and stops. They stop because attribution requires knowing your entire stack's topology — and the tools that know the topology aren't watching the statistics.

**Be precise about what is new here.** Undertow composes shipped DataHub primitives — ML entities, column-level lineage, ownership, assertions. That composition is the *point*, not a gap in the research. Three things are genuinely unoccupied:

| | Claim | Why it holds |
|---|---|---|
| **1** | **The join.** A measured drift signal × the lineage graph = causal attribution. | Drift tools have no graph. Graph tools measure nothing. Proposed in the literature (arXiv 2510.23528), implemented by nobody. |
| **2** | **Gating the model, not the table.** | Datafold, Recce, and Grai gate data PRs. No shipped tool gates an *ML model deploy* on upstream lineage risk. |
| **3** | **Pre-deploy, not post-incident.** | Monte Carlo root-causes after the incident. Datafold checks before a table merge. Before a *model* ships is the empty cell. |

And the reframe from *predicting degradation* to *attributing change* is what makes it buildable:

- **More feasible** — traversal and diffing are deterministic; predicting accuracy loss is open research.
- **More defensible** — the moat is the graph, and the graph gets richer with every verdict written back.
- **More honest** — every claim is falsifiable by inspecting a path. Nothing rests on a model we can't explain.

---

## 3. Users & Jobs to Be Done

**Primary — the ML engineer shipping a model.**
> *"When I deploy a retrain, I want to know if anything upstream shifted since the last good version, so I don't push a silently degraded model to prod."*

**Secondary — the data engineer changing a table.**
> *"When I alter a schema, I want to know if an ML model depends on it, so I don't break someone's model without knowing."*

**Tertiary — the platform/governance lead.**
> *"I want a durable, auditable record of why each model version was cleared to ship."*

That third job is what turns this from a script into a system. **The verdict is written back to DataHub**, so the next engineer — or the next agent — inherits the reasoning instead of re-deriving it.

---

## 4. What It Does

Undertow runs as a **CI gate** before a model deploy. Four steps:

**1 · Resolve** — From a model URN, walk the DataHub graph:
`mlModel --Consumes--> mlFeature --DerivedFrom--> dataset → …upstream datasets`
Result: the model's complete **data dependency footprint**, N hops deep.

*(Two hops to reach data, not three. Feature tables are fetched for display but sit outside the lineage graph — their `Contains` relationship isn't flagged `isLineage`. Verified in `LineageRegistry.java`; see Architecture §1.3.)*

**2 · Diff** — For every asset in that footprint, compare current state against the snapshot from the last approved deploy:
- **Schema deltas** — columns dropped/renamed/retyped, nullability changes *(deterministic)*
- **Distribution deltas** — PSI / KS on profiled statistics *(probabilistic)*
- **Governance deltas** — deprecations, ownership changes, new PII tags

**3 · Attribute & Rule** — For each finding, produce the **lineage path** from root cause to affected feature, then apply a deterministic risk policy:

| Severity | Trigger | Action |
|---|---|---|
| 🔴 **BLOCK** | Breaking schema change or deprecated asset on a live feature path | Fail the CI check |
| 🟡 **WARN** | Distribution drift past threshold, ownership loss, new PII on a feature input | Pass with annotation |
| 🟢 **CLEAR** | No material change | Pass |

**4 · Write back** — Post the verdict to the PR **and** to DataHub. The verdict is emitted as a **native data-quality assertion** (`assertionInfo` + `assertionRunEvent`, type `CUSTOM`, source `EXTERNAL`) against the model, plus an `undertow:blocked` / `undertow:cleared` tag and a link to the full reasoning.

Assertions are the right surface here, not a bolt-on: DataHub explicitly doesn't schedule assertion evaluations itself — external tools (dbt, Airflow, Great Expectations) evaluate, and DataHub stores the results. Undertow is exactly that kind of external evaluator. Verdicts land in the data-quality surface a team already watches, and get run history for free.

### The output an engineer actually sees

```
🔴 BLOCK — model:fraud_detector_v3 (2 blocking, 1 warning)

┌─ BLOCKING ────────────────────────────────────────────────
│ Feature `avg_txn_30d` — upstream column removed
│
│   raw.payments.amount_usd        ← DROPPED in PR #402 (@data-eng-tom)
│     └→ staging.payments_clean.amount
│        └→ features.txn_aggregates.avg_txn_30d
│           └→ mlModel:fraud_detector_v3  [feature weight: 0.23]
│
│   Confidence: CERTAIN (schema diff)
└───────────────────────────────────────────────────────────

┌─ WARNING ─────────────────────────────────────────────────
│ Feature `merchant_risk_score` — distribution shift
│   PSI 0.31 (threshold 0.20) · origin: raw.merchants (3 hops)
│   Confidence: PROBABLE (statistical)
└───────────────────────────────────────────────────────────

✍ Written to DataHub → mlModel:fraud_detector_v3
```

Three things make this credible: **the path** (not just "something drifted"), **the person** (accountability), and **the confidence label** (the system knows what it knows).

---

## 5. Design Principles

**1 · Prove, don't predict.**
We never claim "accuracy will drop 4%." We claim "this feature's upstream column was deleted — here is the path." Every finding is falsifiable by inspecting the graph. That is the difference between a tool engineers trust and one that collapses the first time somebody checks its arithmetic.

**2 · Certainty is a first-class output.**
`CERTAIN` (schema diff) blocks. `PROBABLE` (statistics) warns. Conflating them is how monitoring tools train people to ignore alerts.

**3 · Meet the workflow where it is.**
A CI check and a PR comment. No new dashboard, no new login, no adoption ceremony.

**4 · Close the loop.**
Reading the graph is table stakes. Writing conclusions back is what makes the stack cumulatively smarter — and it's the behaviour DataHub's context-platform thesis is built on.

---

## 6. Scope

### In scope (v1)
- CI gate: GitHub Action + standalone CLI
- Lineage traversal over DataHub ML entities via MCP + Python SDK
- Schema diff (deterministic) and statistical drift (PSI/KS) on profiled data
- Attribution paths with owner resolution
- Deterministic risk policy, configurable via `undertow.yaml`
- Write-back: native **assertion** (primary), tag, structured properties, `institutionalMemory` link — all four verified on open-source Core
- Reference fixture: a seeded `mlModel` with genuine end-to-end lineage in DataHub — a three-hop chain (`transactions.raw` → `staging.transactions_clean` → `transaction_velocity_7d` → `fraud_detector_v3`) including column-level `fineGrainedLineage`. The model's training metrics are seeded metadata, not the output of a training run; Undertow gates on lineage, so nothing in the product depends on the weights existing.

### Explicitly NOT in scope
- ❌ **Predicting accuracy degradation** — unsolved; claiming it destroys trust
- ❌ Auto-fixing or auto-retraining — human decides
- ❌ Replacing drift monitoring — we attribute, we don't observe continuously
- ❌ A UI — DataHub *is* the UI
- ❌ Model performance monitoring — different product

*Non-goals are load-bearing. Each one is a place where scope creep would produce an unshippable demo.*

---

## 7. Why This Wins

| Judging criterion | How Undertow scores |
|---|---|
| **Use of DataHub** | Uses the ML half of the graph (`mlModel`/`mlFeature`) that most projects ignore. Reads via MCP, **writes verdicts back as native data-quality assertions**. The product is impossible without the context platform — not decorated by it. |
| **Technical execution** | Deterministic core, real fixture, real CI, runs end-to-end from `docker compose up`. Every API path verified against source before a line was written. |
| **Originality** | The **join** — drift statistic × lineage graph → attribution — is unimplemented anywhere, and gating a *model deploy* (rather than a data PR) on upstream risk is an empty cell in the market. |
| **Real-world usefulness** | Silent model degradation from upstream change is a top-3 MLOps failure mode with real money attached. |
| **Submission quality** | One-command demo, seeded fixture, <3min video with a clean narrative arc. |
| **OSS contribution bonus** | `MLModelPatchBuilder` for `datahub/specific/`. DataHub's `entity_client.update()` accepts a `MetadataPatchProposal` and routes it to a surgical `PATCH`, and `datahub/specific/` supplies builders for seven entity types — but none for an ML entity, so `mlModel` aspects can only be written by full-aspect `UPSERT`. Verified against a live GMS: two independent `PATCH` writes to `structuredProperties` both survive; an `UPSERT` silently drops the property it didn't know about. The builder composes DataHub's existing entity-agnostic mixins — no new machinery, just the missing composition. |

### Where this sits relative to DataHub itself

**DataHub Cloud already ships blast-radius analysis** — "shows exactly which models, reports, and teams are affected the moment upstream data shifts." Undertow does not claim to have invented downstream traversal, and would be a worse product if it pretended the graph were not already there.

It composes DataHub's shipped primitives and adds the two things they deliberately leave out: joining the graph to a measured signal, and making the result blocking. DataHub informs; Undertow decides.

The overlap is the point. A tool built along the platform's grain inherits every future improvement to that platform; one built beside it inherits nothing and has to maintain its own copy of the graph.

---

## 8. Success Criteria

**It has to work in someone else's hands, on their machine.**
- `datahub docker quickstart` → seeded DataHub with ML lineage in <10 min
- One command triggers a BLOCK; one triggers a CLEAR
- The verdict is visible in DataHub's own UI, not only in our output
- No step depends on a live external service

**Product claims must hold.**
- Every finding traceable to a graph path
- Zero false BLOCKs on the fixture (deterministic checks must be right)
- Confidence labels never conflated

---

## 9. Where this goes next

The wedge is the CI gate. The expansion is obvious and sequential:

1. **Gate** → pre-deploy verdicts *(v1)*
2. **Watch** → continuous evaluation as upstream changes land, not just at deploy
3. **Attribute** → when a model *does* degrade in prod, walk the graph backwards to the change that caused it
4. **Govern** → org-wide model risk posture, built from accumulated verdicts

Each stage writes more to the graph, which makes each subsequent stage better. That compounding is the business.
