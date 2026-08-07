# Undertow — 7-Day Build Plan

**Today:** Mon 2026-08-03 · **Deadline:** Mon 2026-08-10, 22:00 GMT+1
**Working days:** 7 · **Real working days:** 6 (Day 7 is buffer, and you will need it)

---

## Sequencing Principle

**Kill the biggest risk first.**

The riskiest assumption in this entire project is *"we can get a real ML lineage graph into DataHub."* Everything else is ordinary software. So that gets Day 1 — not Day 4, when discovering it's hard would be fatal.

The order below is deliberately **not** the order the code reads in. It's ordered by risk:

```
Day 1  ⚠️  RISKIEST — real ML graph in DataHub            ← go/no-go gate
Day 2  ⚠️  RISKY   — traversal via MCP + write-back proof
Day 3      CORE    — differ + policy engine (deterministic)
Day 4      CORE    — attribution + reporting
Day 5      POLISH  — CI action, narrator, fixture scripts
Day 6      SHIP    — video, README, submission
Day 7      BUFFER  — the thing that goes wrong
```

**Two hard checkpoints.** If Day 1 fails, pivot to the Docs-Debt Agent — it needs no ML entities and reuses the traversal, differ, and write-back layers. If Day 2 fails, drop MCP to the fallback SDK path and continue. Both pivots are cheap *because* they're planned now.

> **Day 2's risk dropped materially since this plan was drafted.** MCP read tools (`get_entities`, `get_lineage`, `search`, `list_schema_fields`, `get_lineage_paths_between`, `get_dataset_queries`) carry **no version gate at all** — bare `@read_only`, no `@min_version` — so the resolver works on Core at any GMS version. Mutation tools are confirmed available on Core ≥ 1.4.0. The `SdkLineageSource` fallback stays in the plan as insurance, but it is now unlikely to be needed.

---

## Day 1 — Monday · Prove the graph
**Goal: a real ML lineage graph, visible in DataHub, end to end.**
**This is the go/no-go day.**

- [ ] `datahub docker quickstart`; UI at `localhost:9002` (datahub/datahub), GMS at `localhost:8080`. **Docker needs 8GB RAM allocated** — check this before anything else. Auth is off by default, so no token needed
- [ ] ~~Repo scaffold~~ **done** — `pyproject.toml`, `src/undertow/`, Apache 2.0 `LICENSE` committed, 54 tests green (`pytest`). The pure core (models, policy, engine, CLI) needs no DataHub instance
- [ ] Pick the dataset — public, redistributable, fraud/churn shaped. Small enough to be fast, real enough to be believable
- [ ] Build the actual pipeline: raw tables → SQL transforms → features → trained model. **Real transformations, because real transformations produce real lineage**

**⚡ One unknown left, now narrowed to a round trip.** Four of the original five closed against source (Architecture §A.5–A.8), and the fifth is half-closed: `acryl-datahub 1.6.0.17` is installed and every field A.4 uses is confirmed present on `DatasetProfileClass` / `DatasetFieldProfileClass`, pinned by `tests/test_sdk_assumptions.py`. **The schema is no longer a guess; only transport is unproven.**

- [ ] **`datasetProfile` round trip.** Emit one profile against quickstart, confirm it renders on the dataset's Stats tab. Construction is verified — this is checking emit → store → read, not field names. *If the round trip fails: Tier-1 stats (null rate, cardinality, mean shift, row count) come from default profiling and need no fix; only Tier-2 PSI is affected*

**Two quick confirmations while the graph is fresh** — both are verified in source, you're checking reality matches:

- [ ] **ML lineage in the UI.** `mlModel` and `mlFeature` both declare `LineageTab` + `getLineageVizConfig` in `entityV2/` (§A.7). Open `fraud_detector_v3` and confirm the tab is populated — the frontend is confirmed, the **backend resolver returning ML relationships against real data** is not. This is a scripted video beat, so see it with your own eyes today
- [ ] **Don't look for a Lineage tab on `mlFeatureTable`** — it doesn't have one by design. It renders as a node in *other* graphs only

Then build the seed:

- [ ] Emit **bottom-up** — the reference order is forced by the data model: datasets → features (`sources`) → feature table → model (`mlFeatures`)
- [ ] Use the verified code in Architecture §A.3. Two traps that will cost you an hour each: `training_metrics`/`hyper_params` take **strings, not floats**; and `mlFeatures` is **not** an `MLModel` kwarg — it goes through `extra_aspects`
- [ ] Remember `mlFeature`/`mlFeatureTable`/`mlPrimaryKey` have **no typed SDK class** — emitter + MCPW only. The seed script is legitimately mixed-style
- [ ] `make seed` reproduces the whole graph from scratch

**✅ Exit criterion:** `fraud_detector_v3` exists in DataHub, and you can traverse `mlModel --Consumes--> mlFeature --DerivedFrom--> dataset` programmatically. If traversal fails, stop and pivot.

> **Note the exit criterion is programmatic, not visual.** The UI declares ML lineage support (§A.7) but the backend resolver returning it against real data isn't independently confirmed — and either way, a working API traversal is the real dependency. The UI is a demo beat, not a blocker.

> **Why this is Day 1:** every other day's work assumes this graph exists. Discovering on Day 4 that ML lineage emission is fighting you is unrecoverable; discovering it today costs you one day and you still ship.

---

## Day 2 — Tuesday · Prove read *and* write
**Goal: traverse the graph programmatically, and write something back.**

- [ ] MCP server connected; enumerate the real tool signatures against your instance — **verify, don't trust the docs**. Confirmed names: `search`, `get_entities` *(plural)*, `get_lineage`, `list_schema_fields`, `get_lineage_paths_between`, `get_dataset_queries`
- [ ] `LineageSource` protocol + `McpLineageSource`
- [ ] BFS traversal along the **verified two-hop path**: `mlModel --Consumes--> mlFeature --DerivedFrom--> dataset`, then normal `upstreamLineage` once you reach datasets. Cycle guard + hop cap + memoisation
- [ ] **Do not route traversal through `mlFeatureTable`** — its `Contains`/`KeyedBy` relationships are not `isLineage`, so it sits outside the lineage graph. Fetch it for display context only
- [ ] `DependencyFootprint` Pydantic model
- [ ] `SdkLineageSource` fallback — same interface, GraphQL/SDK underneath *(1–2 hrs of insurance against your highest-variance dependency)*
- [ ] **Write-back spike — all four paths verified on Core, so this is confirmation, not discovery.** In order: ① assertion (`assertionInfo` + `assertionRunEvent`, `type=CUSTOM`, `source=EXTERNAL`) → ② `globalTags` → ③ structured property via the composed `MLModelPatchBuilder` (Architecture §A.5) → ④ `institutionalMemory`. Confirm each in the UI before moving on
- [ ] Write the ~5-line `MLModelPatchBuilder(HasStructuredPropertiesPatch, MetadataPatchProposal)`. **This is the Day 6 OSS contribution.** Already written and executed in `tests/test_sdk_assumptions.py` — it emits a valid `mlModel`/`structuredProperties`/`PATCH` MCP, and the same file asserts upstream still lacks one. Promote it from test to `src/` when the write-back path lands
- [ ] **MCP mutations: set `TOOLS_IS_MUTATION_ENABLED=true` and check `list_tools`.** Verified requirements are GMS ≥ 1.4.0 plus that env var. Gated tools are *hidden from discovery rather than erroring*, so a missing tool means the gate didn't open — check the tool list, don't infer from a call failure
- [ ] **If mutation tools are absent despite the env var:** the version read failed closed. `_get_server_version_info` defaults to an all-zero version tuple when it can't read `server_config`, which silently filters out every `@min_version` tool. Check GMS connectivity, not your config
- [ ] **Expect `get_dataset_assertions` to be missing — it's genuinely Cloud-only,** the one such tool. Undertow *writes* assertions via the SDK and reads them back the same way; nothing depends on that MCP tool
- [ ] Sanity-check ML entity detail coming back from `get_lineage`. Traversal is generic (no type whitelist), but `gql/entity_details.gql` is unread — if it uses per-type inline fragments, ML nodes could traverse fine yet return thin detail. Also note `#[CLOUD]`-marked GraphQL fields are stripped on self-hosted Core via a `frontend_base_url` heuristic, so field-level thinning is the real Core risk, not tool absence

**✅ Exit criterion:** `undertow resolve --model <urn>` prints the full footprint. An assertion you emitted is visible in DataHub.

> **Why write-back today:** it's the #1 judging criterion. All four paths are now verified in source, but *verified in source* and *working against your instance* are different claims — and the gap between them is exactly what Day 2 exists to close. Proving it today rather than assuming it on Day 5 is what keeps the submission's strongest claim intact.

---

## Day 3 — Wednesday · The deterministic core
**Goal: findings and verdicts, fully testable, no LLM.**

- [ ] Snapshot capture/load: `UndertowSnapshot` — schemas, profiles, tags, ownership
- [ ] **Schema differ** — column dropped / type changed / nullability. `CERTAIN` confidence
- [ ] **Governance differ** — deprecation, ownership loss, new PII tags
- [ ] **Statistical differ — two tiers** (Architecture §2.3), because DataHub's profiler defaults matter here. **Tier 1** works on default-profiled data: null-rate jump >10pp, cardinality change >50%, mean z-shift >3σ, min/max range violation, rowCount change >50%. **Tier 2** adds PSI, but only when `quantiles` / `distinctValueFrequencies` / `histogram` are present — all three are `default=False` in `ge_profiling_config.py`, and even when enabled they're gated behind numeric type *and* `Cardinality ∈ {FEW, MANY, VERY_MANY}`, so STRING and DATETIME columns never get them. Build Tier 1 first; it carries the feature alone. `PROBABLE` confidence for both
- [ ] **Policy engine** — pure `list[Finding] → Verdict`; `undertow.yaml` loading; exemptions with expiry
- [ ] **Unit tests on the policy engine and schema differ.** Non-negotiable: these produce BLOCK verdicts, and a false BLOCK is the one bug that makes a real team delete your tool

**✅ Exit criterion:** feed it two snapshots, get a correct `Verdict` with correct severity and confidence labels. Tests green.

---

## Day 4 — Thursday · Attribution and output
**Goal: the output that makes a judge sit up.**

- [ ] `AttributionPath` — walk findings back to root cause through recorded hops
- [ ] Owner resolution from the `ownership` aspect *(a path without a person isn't actionable)*
- [ ] Query evidence via `get_dataset_queries` where available
- [ ] Console reporter — the boxed tree output from the product design doc, via Rich
- [ ] Exit codes: `1` on BLOCK, `0` otherwise
- [ ] `undertow-report.json`
- [ ] Full write-back: **assertion** (`assertionInfo` + `assertionRunEvent`) as the primary surface, `undertow:blocked`/`cleared` tag, `institutionalMemory` link to the reasoning, structured properties if the Day-2 spike cleared them
- [ ] Stable assertion GUID via `builder.make_assertion_urn(builder.datahub_guid({...}))` — **keep the GUID input stable across runs** or you orphan run history instead of appending to it

**✅ Exit criterion:** `undertow check` on the broken fixture prints the attribution tree with owners, exits 1, and the verdict is visible in DataHub.

> This is the day the product becomes *demoable*. The tree output is what lands in the video thumbnail.

---

## Day 5 — Friday · Wedge, polish, fixture scripts
**Goal: it's a product, not a script.**

- [ ] **`make break-schema` / `make break-stats` / `make reset`** — the three demo commands. Build these properly; the entire demo runs on them
- [ ] GitHub Action packaging + PR comment posting
- [ ] Narrator: bounded LLM call (`claude-sonnet-5`), template fallback, URN-validation hallucination guard
- [ ] End-to-end run on a clean machine — catch every hidden local dependency
- [ ] README: what it does, why it matters, one-command setup, architecture diagram
- [ ] `examples/` with sample outputs *(explicitly rewarded in the rules)*

**✅ Exit criterion:** fresh clone → `docker compose up` → `make seed` → `make break-schema` → BLOCK. No manual steps.

---

## Day 6 — Saturday · Ship it
**Goal: submitted, with a day to spare.**

- [ ] **Demo video, <3 min, public on YouTube.** Script:
  - `0:00–0:20` The problem: upstream change silently degrades a prod model
  - `0:20–0:40` The graph in DataHub — this is real lineage. **Credit the platform here, explicitly:** "DataHub already knows which models sit downstream. What it doesn't do is join that to a drift signal and block on it."
  - `0:40–1:10` `make break-schema` → 🔴 BLOCK with attribution path and owner
  - `1:10–1:40` The verdict written back as a native assertion — *the graph got smarter*
  - `1:40–2:10` `make break-stats` → 🟡 WARN — it knows certain from probable
  - `2:10–2:40` The GitHub Action gating a real PR
  - `2:40–3:00` Why it matters + what's next
- [ ] Repo public, **Apache 2.0 visible in the About section** (verify — it's a stated requirement)
- [ ] Devpost submission: description, repo URL, video, setup instructions
- [ ] **Opt into the feedback survey** — $50 × 10 awards, ~15 minutes of work
- [ ] **OSS contribution for the bonus criterion — now a concrete, pre-identified PR.** `datahub/specific/` has patch builders for chart, dashboard, datajob, dataproduct, dataset, form, and structured_property — but **no `mlmodel.py`**. The base `MetadataPatchProposal` is already entity-agnostic (`guess_entity_type(urn)`), so `MLModelPatchBuilder(HasStructuredPropertiesPatch, MetadataPatchProposal)` is a genuine gap with a small, obvious fix. You'll have written and used it on Day 2 — upstream it with the test.

**✅ Exit criterion:** submitted. Not "nearly ready."

---

## Day 7 — Sunday · Buffer
Reserved for whatever broke. If nothing broke:

- [ ] Second reviewer runs the setup cold, on their machine, from the README only
- [ ] Airflow/Dagster usage example — shows the library surface
- [ ] Tighten the video if it's flabby
- [ ] Re-read the judging criteria against the submission, line by line

> **Do not plan features into Day 7.** It exists because something always breaks on Day 5, and a submission that lands on Day 6 beats a better one that lands on Day 11.

---

## Cut List — in order

If you fall behind, cut from the bottom up. Never cut upward.

| Priority | Item | Cuttable? |
|---|---|---|
| 1 | Real ML graph in DataHub | ❌ Never — it's the whole thesis |
| 2 | Traversal + footprint | ❌ Never |
| 3 | Schema differ + policy engine | ❌ Never — the deterministic BLOCK is the product |
| 4 | Attribution paths + owners | ❌ Never — this is the originality |
| 5 | Write-back to DataHub | ❌ Never — #1 judging criterion |
| 6 | Video + README | ❌ Never — submission requirement |
| 7 | Statistical differ — **Tier 1** | ❌ Never — the drift signal is half of the novelty claim (the *join*) |
| 8 | Statistical differ — **Tier 2** (PSI) | ⚠️ Only if profiling config resists. Tier 1 already produces WARN verdicts |
| 9 | GitHub Action | ⚠️ CLI-only demo is acceptable |
| 10 | LLM narrator | ✅ Template output is fine |
| 11 | Query evidence | ✅ Nice-to-have |
| 12 | Airflow example | ✅ Cut freely |

Items 1–7 are the submission. Everything below 7 is upside.

**Note what moved.** The statistical differ used to sit at priority 7 as "cuttable, degrade to null-rate only." It can't be cut now: the defensible originality claim is *the join of a drift signal to the lineage graph*, and cutting drift entirely leaves a schema-diff impact analyser — which is much closer to what DataHub already ships. Splitting it into tiers is what keeps it uncuttable **and** low-risk: Tier 1 runs on default-profiled data and needs nothing verified.

**This list is insurance, not a plan.** With the unknowns closed, nothing here is expected to be exercised. Read it on Friday if Friday goes badly, and not before — deciding in advance to cut something you have time to build is how a submission ends up thinner than it needed to be.

---

## Standing Risks

| Risk | Trigger | Response |
|---|---|---|
| ML emission harder than expected | Day 1 not done by EOD | Pivot to Docs-Debt Agent — reuses traversal + differ + write-back |
| MCP fights you | Day 2 blocked >3 hrs | Switch to `SdkLineageSource`, mention MCP as an alternate path. *Lower risk than drafted — read tools are ungated on Core* |
| Fixture reads as fake | Reviewer says "staged" | Add more transformation depth; real SQL, not hand-written lineage |
| Scope creep | You start adding features on Day 5 | Consult the cut list. Ship Day 6 |
| Video overruns | >3 min | Hard requirement — cut the intro, not the write-back segment |
| **Judge finds DataHub's own impact analysis** | Any time | **Highest-likelihood credibility risk, and fully preventable.** DataHub Cloud markets blast-radius analysis that surfaces affected models on upstream change. Credit it first, unprompted, in the README and at 0:20 in the video. Undertow's claim is the *join* to a drift signal plus a blocking gate on the model — not "nobody does lineage." Getting caught overclaiming costs more than the originality points are worth |

---

## The One Thing

If you remember nothing else from this plan:

> **Day 1 decides whether this project happens.**

A real ML lineage graph in DataHub is the foundation for every claim the product makes. Get it standing today, and the remaining six days are ordinary engineering against a known target. Defer it, and you'll be discovering the hard part on Thursday with nowhere to go.
