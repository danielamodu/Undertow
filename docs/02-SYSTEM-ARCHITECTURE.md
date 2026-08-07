# Undertow — System Architecture & Design

**Status:** design locked for v1

---

## 1. Architectural Position

Undertow is a **stateless analysis agent** invoked at deploy time. It holds no database of its own: DataHub is the source of truth for topology *and* the store for verdict history. The only local state is a **baseline snapshot** — and even that is persisted back into DataHub so the system survives a wiped CI runner.

```
                          ┌──────────────────────────┐
                          │   Trigger                │
                          │  • GitHub Action (PR)    │
                          │  • CLI (local / manual)  │
                          └───────────┬──────────────┘
                                      │ model URN
                                      ▼
   ┌──────────────────────────────────────────────────────────────┐
   │                       UNDERTOW CORE                          │
   │                                                              │
   │   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐  │
   │   │ RESOLVER │──▶│  DIFFER  │──▶│ ATTRIBUTOR│──▶│  POLICY  │  │
   │   │ traverse │   │ schema + │   │  root-    │   │  engine  │  │
   │   │  graph   │   │  stats   │   │cause paths│   │ verdict  │  │
   │   └────┬─────┘   └────┬─────┘   └─────┬────┘   └────┬─────┘  │
   │        │              │               │              │       │
   │        │         ┌────▼───────────────▼──────┐       │       │
   │        │         │   LLM NARRATOR (bounded)   │       │       │
   │        │         │  facts in → prose out      │       │       │
   │        │         │  never decides severity    │       │       │
   │        │         └────────────┬───────────────┘       │       │
   │        │                      │                       │       │
   │        │                      ▼                       ▼       │
   │        │              ┌───────────────────────────────────┐   │
   │        │              │          REPORTER                 │   │
   │        │              │  exit code · PR comment · JSON    │   │
   │        │              └───────────────┬───────────────────┘   │
   └────────┼──────────────────────────────┼───────────────────────┘
            │ read                         │ write
            ▼                              ▼
   ┌────────────────────────────────────────────────────────────┐
   │                        DATAHUB                             │
   │  MCP Server                 │  Python SDK (write)          │
   │  search · get_entities      │  assertionInfo + RunEvent    │
   │  get_lineage · schema_fields│  structuredProperties        │
   │  add_tags · add_struct_props│  globalTags · instMemory     │
   │                                                            │
   │  Graph: mlModel --Consumes--> mlFeature                    │
   │                  --DerivedFrom--> dataset --upstream--> …   │
   └────────────────────────────────────────────────────────────┘
```

### The load-bearing design decision

**Deterministic code decides. The LLM only explains.**

The severity verdict is computed by a rule engine over structured diffs — not by a language model. The LLM receives already-decided facts and turns them into readable prose.

Why this matters:
- No input, however crafted, can talk the gate into a wrong verdict
- Results are reproducible run-to-run — mandatory for a CI gate
- No API key required for the core to function (LLM degrades to templated output)
- It is genuinely the correct engineering choice, not a shortcut

This is the difference between an agent that *uses* an LLM and an LLM that pretends to be a system.

---

## 2. Components

### 2.1 Resolver — build the dependency footprint

**Input:** `urn:li:mlModel:(urn:li:dataPlatform:mlflow,fraud_detector,PROD)`
**Output:** a `DependencyFootprint` — every asset the model transitively depends on, with hop distance and the path taken.

Traversal, breadth-first, `max_hops` configurable (default 5).

**The real ML lineage path — verified against `entity-registry.yml` and `LineageRegistry.java`:**

```
mlModel
  └─ mlFeature      via mlModelProperties.mlFeatures
  │                 relationship "Consumes"     · isLineage: true
  │
  └──── dataset     via mlFeatureProperties.sources
        │           relationship "DerivedFrom"  · isLineage: true
        │
        └─ dataset  transitive upstream via the dataset's own
                    upstreamLineage aspect, N hops
```

**Three verified facts that shape this design:**

1. **`mlFeatureTable` is NOT a lineage node.** Its `Contains` and `KeyedBy` relationships carry no `isLineage` flag, so the lineage graph does not traverse them. Traversal reaches features **directly from the model**, not through their table. *(Feature tables are still fetched for display context — they're just not on the lineage path.)*

2. **There is no direct `mlModel → dataset` edge.** The model→data path is always exactly two hops: `Consumes` then `DerivedFrom`. `mlModelProperties` has no `sources` or `upstreamDatasets` field.

3. **`upstreamLineage` does not apply to any ML entity.** It appears on none of the six ML entity types. ML lineage is expressed *purely* through relationship annotations on property aspects. Only once traversal reaches a `dataset` does normal `upstreamLineage` traversal take over.

`LineageRegistry.java` builds the lineage graph generically from any relationship annotated `isLineage: true` — ML entities are not special-cased, so the standard lineage APIs traverse these edges natively.

**Relationship reference:**

| Relationship | From → To | isLineage | Direction |
|---|---|---|---|
| `Consumes` | `mlModelProperties.mlFeatures` → mlFeature | ✅ | upstream |
| `DerivedFrom` | `mlFeatureProperties.sources` → dataset | ✅ | upstream |
| `TrainedBy` | `mlModelProperties.trainingJobs` → dataJob/dataProcessInstance | ✅ | upstream |
| `MemberOf` | `mlModelProperties.groups` → mlModelGroup | ✅ | downstream |
| `Contains` | `mlFeatureTableProperties.mlFeatures` → mlFeature | ❌ | — |
| `KeyedBy` | `mlFeatureTableProperties.mlPrimaryKeys` → mlPrimaryKey | ❌ | — |

**URN formats — note the irregularity:**

```
mlModel        urn:li:mlModel:(urn:li:dataPlatform:mlflow,fraud_detector,PROD)
mlFeatureTable urn:li:mlFeatureTable:(urn:li:dataPlatform:feast,txn_aggregates)   ← platform, NO fabric
mlFeature      urn:li:mlFeature:(txn_aggregates,avg_txn_30d)                      ← namespace + name only
mlPrimaryKey   urn:li:mlPrimaryKey:(txn_aggregates,user_id)                       ← namespace + name only
```

`mlFeature` and `mlPrimaryKey` URNs contain **no platform and no fabric** — just a bare-string namespace and a name. `MLFeatureUrn.java` enforces exactly two tuple keys. Consequence: feature URNs are not globally scoped by platform or environment, so the fixture must use namespaces that won't collide.

**Primary path — MCP.** The agent calls the DataHub MCP server's read tools:

**Verified tool names** (from the `mcp-server-datahub` README — note `get_entities` is **plural**; some docs pages say `get_entity`, which does not exist):

| Tool | Used for |
|---|---|
| `search` | Resolve a model by name when a URN isn't supplied |
| `get_entities` | Batch-fetch model/feature/dataset detail: schema, ownership, tags, properties |
| `get_lineage` | Walk upstream, with hop control |
| `list_schema_fields` | Enumerate columns for schema diffing on wide tables |
| `get_lineage_paths_between` | Exact path between two assets — feeds the attributor directly |
| `get_dataset_queries` | SQL evidence for how a column flows |

`get_entities` being batch-capable matters: a footprint of ~20 assets is one call, not twenty. `get_lineage_paths_between` is a better fit for attribution than I originally assumed — it returns intermediate transformations, so the attributor can use it instead of reconstructing paths from traversal state.

**Fallback path — Python SDK / GraphQL.** MCP is the showcase surface, but the resolver is written against an interface with two implementations. If MCP is slow or a tool signature shifts, the SDK path keeps the demo alive. *(This is a deliberate hedge — the single biggest live-demo risk is a dependency changing under us.)*

```python
class LineageSource(Protocol):
    def get_entity(self, urn: str) -> Entity: ...
    def get_lineage(self, urn: str, direction: str, hops: int) -> list[Edge]: ...
    def list_schema_fields(self, urn: str) -> list[SchemaField]: ...

class McpLineageSource(LineageSource):   # showcase path
class SdkLineageSource(LineageSource):   # resilience path
```

Traversal is memoised and cycle-guarded — real graphs contain loops, and an unguarded BFS hangs the demo.

---

### 2.2 Differ — compare against the last approved deploy

**Baseline:** an `UndertowSnapshot` captured at the last CLEARED deploy — schemas, profiled statistics, tags, ownership, and deprecation state for every asset in the footprint.

Snapshots live in **two places**: a local `.undertow/snapshots/` file for speed, and a document attached to the `mlModel` in DataHub for durability. The DataHub copy is authoritative — a fresh CI runner with no local cache still works.

Three diff families:

**A · Schema diff — `CERTAIN`**
Deterministic set comparison over `schemaMetadata` fields:

| Change | Severity |
|---|---|
| Column dropped, and it feeds a live feature | 🔴 BLOCK |
| Type changed incompatibly (`string → int`, precision loss) | 🔴 BLOCK |
| Nullability relaxed (`NOT NULL → NULL`) on a feature input | 🟡 WARN |
| Column added | 🟢 INFO |

**B · Statistical diff — `PROBABLE`** — **two-tier, by necessity**

> ### ⚠️ The constraint that shapes this component
>
> Verified in `ge_profiling_config.py`: `include_field_quantiles`, `include_field_histogram`, and `include_field_distinct_value_frequencies` are **all `default=False`**. Profiling itself is `enabled: bool = Field(default=False)`.
>
> Even with the flags on, `ge_data_profiler.py` gates quantiles and histograms behind **numeric type AND `Cardinality` ∈ {FEW, MANY, VERY_MANY}** — so STRING and DATETIME columns *never* get them, and `Cardinality.UNIQUE` columns don't either. When produced, quantiles are a fixed 5 points (`0.05, 0.25, 0.5, 0.75, 0.95`), stored as **strings**.
>
> **Consequence:** a PSI-only differ would work on our fixture and return nothing on a real instance — a check that passes because it never ran.

So the differ is tiered. **Tier 1 runs on default-profiled data; Tier 2 activates when richer stats exist.**

**Tier 1 — always available** *(these are all `default=True`)*

| Signal | From | Rule |
|---|---|---|
| Null-rate jump | `nullCount`, `nullProportion` | `>10pp` → WARN |
| Cardinality collapse/explosion | `uniqueCount`, `uniqueProportion` | `>50%` relative change → WARN |
| Central-tendency shift | `mean`, `median`, `stdev` | z-shift `>3σ` vs baseline stdev → WARN |
| Range violation | `min`, `max` | new min/max outside baseline envelope → WARN |
| Volume anomaly | `rowCount` (on `DatasetProfile`) | `>50%` change → INFO |

This tier alone is a working drift detector on an out-of-the-box DataHub. It's the floor, and the floor is solid.

**Tier 2 — PSI, when the data supports it**

When `quantiles` or `distinctValueFrequencies` are present, compute PSI properly:
- **Numeric** → PSI over the 5 quantile buckets. `>0.25` WARN, `>0.10` INFO
- **Categorical** → PSI over `distinctValueFrequencies`
- **`histogram`** → preferred when present (finer bins than 5 quantiles)

The report states which tier produced each finding, so a user knows whether they're seeing coarse or fine analysis. And the README documents the opt-in that unlocks Tier 2:

```yaml
profiling:
  enabled: true
  include_field_quantiles: true
  include_field_histogram: true
  include_field_distinct_value_frequencies: true
```

**Two config traps worth knowing:** `profile_table_level_only: true` hard-forces every `include_field_*` to False and *raises* if you explicitly enabled one. `turn_off_expensive_profiling_metrics: true` uses `setdefault`, so an explicit `true` still wins.

The fixture enables all three flags so the demo shows Tier 2 — and Tier 1 is demonstrated by pointing at a default-profiled dataset. **Showing both is the honest demo**, and it's a stronger one: it proves the tool degrades gracefully instead of only working in a lab.

**Verified field list** on `DatasetFieldProfile` (from `DatasetFieldProfile.pdl`):

| Field | Type | Used for |
|---|---|---|
| `fieldPath` | `string` **(only required field)** | identity |
| `quantiles` | `optional array[Quantile{quantile, value}]` | **PSI on numeric columns** |
| `distinctValueFrequencies` | `optional array[ValueFrequency{value, frequency}]` | **PSI on categorical columns** |
| `histogram` | `optional Histogram{boundaries, heights}` | alternative binning |
| `nullCount` / `nullProportion` | `optional long` / `float` | null-rate jump |
| `uniqueCount` / `uniqueProportion` | `optional long` / `float` | cardinality shift |
| `min`/`max`/`mean`/`median`/`stdev` | `optional string` | range checks |
| `sampleValues` | `optional array[string]` | evidence in report |

**PSI is computable from stored metadata — but only when explicitly enabled.** `quantiles`, `distinctValueFrequencies`, and `histogram` all exist in the schema, so binned distributions are *possible* without touching raw data. They are simply off by default (see the tiering above).

Two implementation notes the schema forces:
- **`min`/`max`/`mean`/`median`/`stdev` are typed `string`, not numeric** — so non-numeric columns can be described uniformly. Parse defensively; never assume float-castable. `Quantile.value` is a string too.
- **Every field except `fieldPath` is `optional`.** Profiling is not guaranteed present or complete. The differ must treat a missing profile as *"cannot assess"* — never as *"no drift."* Silence is not evidence of stability.

Thresholds are configurable in `undertow.yaml`; defaults per tier are listed above.

PSI is the right metric over KS here: it operates on **bucketed summary statistics**, which is exactly what a metadata catalog stores. It needs no access to raw rows — so Undertow never touches the warehouse, never sees customer data, and stays a pure metadata-plane tool. That's an architectural property, not a limitation.

**C · Governance diff — `CERTAIN`**
Deprecation flags, ownership removed (orphaned dependency), new PII/sensitive tags appearing on a feature input path.

---

### 2.3 Attributor — from finding to root cause

A drift signal on a feature is useless without provenance. For each finding, the attributor walks **back down the recorded traversal path** to produce a `AttributionPath`:

```python
@dataclass
class AttributionPath:
    root_cause_urn: str           # raw.payments.amount_usd
    affected_feature_urn: str     # features.txn_aggregates.avg_txn_30d
    hops: list[LineageHop]        # ordered, with transformation info
    owners: list[Owner]           # resolved from ownership aspect
    change_evidence: Evidence     # what changed, old vs new
```

Two enrichments make the output actionable rather than merely correct:

- **Owner resolution** — `ownership` aspect on the root-cause asset gives a name to notify. Blast radius without a person attached doesn't get fixed.
- **Query evidence** — `get_dataset_queries` retrieves the SQL that produced the transformation, so the report can show *how* the column flows, not just *that* it does.

---

### 2.4 Policy Engine — deterministic verdict

Pure function: `list[Finding] → Verdict`. No LLM, no network, fully unit-testable.

```yaml
# undertow.yaml
version: 1
max_hops: 5

rules:
  - id: upstream-column-dropped
    when: {type: schema, change: column_removed, on_feature_path: true}
    severity: BLOCK
    confidence: CERTAIN

  - id: incompatible-type-change
    when: {type: schema, change: type_changed, compatible: false}
    severity: BLOCK
    confidence: CERTAIN

  - id: upstream-deprecated
    when: {type: governance, change: deprecated}
    severity: BLOCK
    confidence: CERTAIN

  - id: distribution-shift
    when: {type: statistical, metric: psi, threshold: 0.25}
    severity: WARN
    confidence: PROBABLE

  - id: new-pii-upstream
    when: {type: governance, change: tag_added, tag: "PII"}
    severity: WARN
    confidence: CERTAIN

thresholds:
  psi_warn: 0.25
  psi_info: 0.10
  null_rate_jump_pp: 10

exemptions:
  - urn: "urn:li:dataset:(...,legacy.deprecated_table,PROD)"
    reason: "Scheduled for removal, no live features"
    expires: 2026-09-01
```

Verdict = highest severity across findings. **Exemptions carry a mandatory reason and expiry** — un-expiring suppressions are how gates rot into noise.

---

### 2.5 Narrator — bounded LLM

The only LLM call in the system. Strictly constrained:

- **Input:** structured `Verdict` object — findings, paths, owners, severities *already decided*
- **Output:** prose explanation for the PR comment
- **Forbidden:** changing severity, inventing findings, adding graph facts
- **Fallback:** if no API key or the call fails, a Jinja template renders the same content, less elegantly

Post-generation validation rejects any output containing URNs not present in the input — a cheap, effective hallucination guard.

Model: `claude-sonnet-5` (narration is not a reasoning-heavy task; latency in CI matters more).

---

### 2.6 Reporter — write-back

Four outputs:

**1 · Exit code** — `0` CLEAR/WARN, `1` BLOCK. This is what makes CI actually gate.

**2 · PR comment** — markdown, via GitHub Action.

**3 · Write-back to DataHub** — the criterion that most submissions will miss:

**Verified:** `mlModel` carries 30 aspects, including all the write targets below — `structuredProperties`, `globalTags`, `glossaryTerms`, `ownership`, `institutionalMemory`, `status`, `deprecation`, `domains`, `documentation`.

| What | Where | Confidence |
|---|---|---|
| Verdict as a **data quality assertion** | `assertion` entity + `assertionRunEvent` | ✅ verified OSS |
| `undertow:blocked` / `undertow:cleared` | `globalTags` on `mlModel` | ✅ verified OSS |
| `undertow_risk_verdict`, `undertow_last_checked` | `structuredProperties` on `mlModel` | ✅ verified — see A.5 |
| Full reasoning + attribution paths | `institutionalMemory` on `mlModel` | ✅ verified aspect |

### The assertion path — a better write-back than I originally designed

**Verified:** the `assertion` entity is open-source Core, not Cloud-only. It's declared in `entity-registry.yml` as `category: core`, and the Great Expectations plugin (`datahub_gx_plugin`) emits `assertionInfo` + `assertionRunEvent` through a plain `DatahubRestEmitter` with no Cloud package and no license gate. That plugin is the working proof that Core stores and serves assertion results.

**What *is* Cloud-only is the evaluation/scheduling layer** — native assertion monitors, anomaly detection, `runAssertion`, the built-in scheduler, and `pip install acryl-datahub-cloud`. DataHub explicitly "does not directly schedule assertion evaluations"; external tools (dbt, Airflow, GX) schedule, and DataHub stores the results.

**Undertow is exactly that kind of external evaluator.** So each check emits:
- `assertionInfo` with `type=CUSTOM` (the PDL explicitly recommends `CUSTOM` + `CustomAssertionInfo` for third-party self-reported checks) and `source=EXTERNAL`
- `assertionRunEvent` with `status=COMPLETE` and `result.type` ∈ `SUCCESS` / `FAILURE` / `ERROR`

This is materially better than tags-plus-properties: verdicts land in DataHub's **native data-quality surface**, get run history for free, and are visible where a data team already looks. It uses the platform the way the platform intends — which is the top judging criterion.

```python
from datahub.metadata.com.linkedin.pegasus2avro.assertion import (
    AssertionInfo, AssertionResult, AssertionResultType,
    AssertionRunEvent, AssertionRunStatus, AssertionType,
)
```

Note the **pegasus2avro** import path — that's what the GX plugin actually uses. Three MCPs per assertion, all with `entityUrn` = the *assertion* URN (not the dataset/model URN). Build the URN via `builder.make_assertion_urn(builder.datahub_guid({...}))` with a **stable** GUID input, so re-runs append to run history instead of orphaning it.

**Enum facts that constrain the code:** `AssertionRunStatus` has exactly one symbol — `COMPLETE`. `AssertionResultType` is `INIT` / `SUCCESS` / `FAILURE` / `ERROR`. `AssertionType.DATA_SCHEMA` is spelled that way because `SCHEMA` is a PDL reserved word.

### Write-back risk register

| Item | Status |
|---|---|
| `assertion` emission on Core | ✅ Verified via GX plugin source |
| `globalTags` on mlModel | ✅ Verified |
| `structuredProperties` aspect on mlModel | ✅ Verified in `entity-registry.yml` |
| **Assigning** a structured property to an mlModel | ✅ Verified — compose `HasStructuredPropertiesPatch` over the generic `MetadataPatchProposal` base, which derives entity type via `guess_entity_type(urn)` with no allowlist. See A.5 |
| `institutionalMemory` on mlModel | ✅ Verified aspect |
| MCP mutation tools on Core | ✅ Verified — `oss="1.4.0"`, requires GMS ≥ 1.4.0 **and** `TOOLS_IS_MUTATION_ENABLED` truthy. See A.6 |
| `get_dataset_assertions` over MCP | ❌ **Cloud-only** — the only such tool. Undertow can *write* assertions to Core but must *read* them via SDK/GraphQL |
| `save_document` on Core | ✅ `oss="1.4.0"`, but also behind `SAVE_DOCUMENT_TOOL_ENABLED`. Enhancement only — `institutionalMemory` is the default |

**Every write path is now verified.** Build order stays assertions → tags → structured properties, but that ordering is now about incremental confidence during the Day-2 spike, not insurance against a path that might not exist.

**Both write paths are live, and they serve different purposes.** The Python SDK is authoritative for the verdict payload — assertions have no MCP mutation tool, so the SDK is the only way to emit them at all. MCP mutations (`add_tags`, `add_structured_properties`) are verified on Core ≥ 1.4.0 and are worth using for exactly the surfaces they cover, because "the agent wrote back through the MCP server" is a stronger DataHub-usage story than "a script POSTed to GMS." Undertow does both: SDK for assertions and `institutionalMemory`, MCP for tags and structured properties, with an SDK fallback if `TOOLS_IS_MUTATION_ENABLED` is unset or GMS is older.

**4 · JSON artifact** — `undertow-report.json`, machine-readable, uploaded as a CI artifact.

---

## 3. Data Flow — the blocking scenario

```
1. PR opened: retrain fraud_detector_v3
      │
2. GitHub Action → undertow check --model urn:li:mlModel:(...,fraud_detector,PROD)
      │
3. RESOLVER   ──MCP──▶ get_entities([model]) → mlModelProperties.mlFeatures
              ──MCP──▶ get_entities(features) → mlFeatureProperties.sources
              ──MCP──▶ get_lineage(dataset, UPSTREAM, hops=5)
              ◀────── footprint: 12 features, 8 datasets
                      (feature tables fetched for display only — not on the
                       lineage path; see §1.3)
      │
4. DIFFER     load baseline from .undertow/ (or DataHub document)
              compare: schemas, profiles, tags, deprecation
              ◀────── 2 schema deltas, 1 PSI breach
      │
5. ATTRIBUTOR walk paths back to root cause; resolve owners
              ◀────── raw.payments.amount_usd DROPPED → avg_txn_30d (@data-eng-tom)
      │
6. POLICY     rule `upstream-column-dropped` → BLOCK / CERTAIN
      │
7. NARRATOR   facts → prose (template fallback if no key)
      │
8. REPORTER   ├─ exit 1 ......................... CI fails, deploy stopped
              ├─ PR comment .................... engineer sees path + owner
              ├─ DataHub write-back ............ assertion (FAILURE) + tag +
              │                                  structured props + memory link
              └─ undertow-report.json .......... CI artifact
```

**Step 8's DataHub write is the differentiator.** The next engineer to open that model in DataHub sees a failed data-quality assertion, `undertow:blocked`, and the full reasoning — without re-running anything. The graph got smarter. That is the context-platform thesis, executed.

---

## 4. The Fixture — de-risking the demo

The demo is only worth anything on a **real** ML graph. Lineage asserted by hand proves the reader works, not that the product does.

**`fixtures/` ships a genuine end-to-end pipeline:**

```
Public dataset (credit-card fraud / churn — Kaggle-class, redistributable)
   │
   ├─ raw.transactions, raw.payments, raw.merchants        [dataset]
   │      ↓ dbt transformations (real SQL → real upstreamLineage)
   ├─ staging.payments_clean, staging.txn_enriched         [dataset]
   │      ↓
   ├─ avg_txn_30d, txn_velocity, merchant_risk_score       [mlFeature]
   │      each with mlFeatureProperties.sources → staging datasets   ("DerivedFrom")
   │      grouped for display by:
   │      txn_aggregates                                   [mlFeatureTable]
   │      ↓
   └─ fraud_detector_v3                                    [mlModel]
          mlModelProperties.mlFeatures → the 3 features    ("Consumes")
          + trainingMetrics, hyperParams, trainingJobs
```

Note the emission order this forces: features must exist before the model can reference them in `mlModelProperties.mlFeatures`, and datasets before features can reference them in `sources`. The seed script emits **bottom-up**: datasets → features → feature table → model.

Everything is emitted to DataHub with the Python SDK via `make seed`. The lineage is real because the transformations are real.

**Two scripted mutations drive the demo:**

| Command | What it does | Expected verdict |
|---|---|---|
| `make break-schema` | Drops `amount_usd` from `raw.payments`, re-emits | 🔴 BLOCK / CERTAIN |
| `make break-stats` | Shifts `merchant_risk_score` distribution, re-emits profile | 🟡 WARN / PROBABLE |
| `make reset` | Restores baseline | 🟢 CLEAR |

All three run in under two minutes. **Both severity classes and both confidence classes get exercised** — which is what shows the system distinguishes what it knows from what it infers.

---

## 5. Deployment

```
docker compose up          # DataHub Core quickstart + seeded fixture
make seed                  # emit the ML graph
undertow check --model <urn>
```

Three surfaces, one core:

| Surface | Use |
|---|---|
| **CLI** (`undertow check`) | local dev, and the demo |
| **GitHub Action** | the real product wedge |
| **Python library** | embeddable in Airflow/Dagster/Prefect deploy DAGs |

**Config:** `DATAHUB_GMS_URL`, `DATAHUB_GMS_TOKEN`, optional `ANTHROPIC_API_KEY`.
**Stack:** Python 3.11+, `acryl-datahub`, `mcp-server-datahub`, Typer, Pydantic, Rich. Apache 2.0.

---

## 6. Failure Modes & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| **MCP tool signature changes** | Demo dies | `LineageSource` interface + SDK fallback; pin versions |
| **ML entities sparse in real instances** | "Only works on your fixture" | Ship the fixture *and* the emitter helpers so anyone can populate theirs — contributed upstream as the OSS bonus |
| **No profiled stats available** | Statistical diff blank | Degrade gracefully: schema + governance checks still run and still BLOCK. Core value survives |
| **LLM unavailable / rate-limited** | No prose | Template fallback; verdict unaffected (LLM never decides) |
| **Graph cycles** | Infinite traversal | Visited-set cycle guard, hop cap |
| **Huge graph** | Slow CI | Memoisation, hop cap, parallel fetch, footprint cache |
| **False BLOCK** | Destroys trust | Deterministic rules only for BLOCK; statistical findings can never block |

The pattern throughout: **every degradation path preserves the deterministic core.** Lose the LLM, lose the profiles, lose MCP — the gate still gates correctly.

---

## 7. Module Layout

```
undertow/
├── src/undertow/
│   ├── cli.py                 # Typer entrypoint
│   ├── resolver/
│   │   ├── base.py            # LineageSource protocol
│   │   ├── mcp_source.py      # MCP implementation  ← showcase
│   │   ├── sdk_source.py      # SDK implementation  ← fallback
│   │   └── traversal.py       # BFS, cycle guard, memoisation
│   ├── differ/
│   │   ├── schema.py          # deterministic
│   │   ├── statistical.py     # PSI
│   │   └── governance.py      # deprecation, tags, ownership
│   ├── attributor/paths.py    # root-cause paths + owners
│   ├── engine.py              # pure function → Verdict
│   ├── policy.py              # Policy, Rule, Exemption, Thresholds
│   ├── narrator/              # bounded LLM + template fallback
│   ├── reporter/
│   │   ├── console.py
│   │   ├── github.py
│   │   └── datahub_writer.py  # ← the write-back
│   └── models.py              # Pydantic types
├── fixtures/                  # real pipeline + seed/break scripts
├── action/                    # GitHub Action packaging
├── tests/
├── docker-compose.yml
└── LICENSE                    # Apache 2.0
```

*(The policy layer is two flat modules rather than a package: `engine.py` holds the pure `evaluate()` function, `policy.py` holds the configuration types it reads. Rules are a `dict[FindingKind, Severity]` with defaults in `policy.py`, not a separate `rules.py` — there was never enough there to justify a module.)*

**Testing priority** — the policy engine and schema differ get exhaustive unit tests. They produce BLOCK verdicts, and a wrong BLOCK is the one failure that would make a real team rip this out.

---

## Appendix A — Verified API Reference

Everything below was verified against primary sources: `entity-registry.yml`, the PDL schemas in `metadata-models/`, `LineageRegistry.java`, the `metadata-ingestion/examples/library/` files, and the `mcp-server-datahub` README. **Anything unverified is explicitly marked.** Verified against `master`; docs pages self-identified as DataHub 1.6.0.

### A.1 Environment

```bash
python3 -m pip install --upgrade acryl-datahub
datahub docker quickstart                 # or --version v1.6.0 to pin
# UI: http://localhost:9002  ·  datahub / datahub
# GMS: http://localhost:8080
```

Docker needs **2 CPUs, 8GB RAM, 2GB swap, 13GB disk**. Sample data: `datahub datapack load showcase-ecommerce` (~1,050 entities; flagged experimental).

**Auth is OFF by default** — `METADATA_SERVICE_AUTH_ENABLED` is opt-in. A local quickstart needs no token, which removes a whole class of demo failure. Examples still read `os.getenv("DATAHUB_GMS_TOKEN")` with no fallback, so the code works either way.

### A.2 SDK coverage — the constraint that shapes the seed script

`datahub.sdk` has typed entity classes for: `chart, container, dashboard, dataflow, datajob, dataset, document, glossary_node, glossary_term, metric, mlmodel, mlmodelgroup, semantic_model, tag`.

> **`MLFeature`, `MLFeatureTable`, and `MLPrimaryKey` have NO typed SDK classes.** They must be emitted with `DatahubRestEmitter` + `MetadataChangeProposalWrapper`.

So the seed script is deliberately mixed: typed SDK for the model, raw emitter for features. That's not inconsistency — it's the only available path.

Also: the verified method is **`client.entities.update(entity)`**. I could not confirm `upsert` exists — don't assume it.

### A.3 Emitting the fixture

**mlModel** (typed SDK, from `mlmodel_create_full.py`):

```python
import datahub.metadata.schema_classes as models
from datahub.metadata.urns import MlFeatureUrn, MlModelGroupUrn
from datahub.sdk import DataHubClient
from datahub.sdk.mlmodel import MLModel

client = DataHubClient.from_env()

mlmodel = MLModel(
    id="fraud_detector_v3",
    name="Fraud Detector v3",
    platform="mlflow",
    custom_properties={"framework": "sklearn"},
    extra_aspects=[
        models.MLModelPropertiesClass(
            mlFeatures=[
                str(MlFeatureUrn(feature_namespace="txn_aggregates",
                                 name="avg_txn_30d")),
            ],
        )
    ],
    training_metrics={"accuracy": "0.94", "precision": "0.91"},  # STRINGS
    hyper_params={"learning_rate": "0.01", "n_estimators": "200"},
)
client.entities.update(mlmodel)
```

Two traps: `training_metrics` / `hyper_params` take **string** values, not floats. And `mlFeatures` is **not** a first-class `MLModel` kwarg — it goes through `extra_aspects` with a raw `MLModelPropertiesClass`, with URNs `str()`-wrapped.

**mlFeature with upstream lineage** (emitter path, from `mlfeature_create.py`):

```python
import datahub.emitter.mce_builder as builder
import datahub.metadata.schema_classes as models
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter

emitter = DatahubRestEmitter(gms_server="http://localhost:8080")

dataset_urn = builder.make_dataset_urn(
    name="staging.payments_clean", platform="snowflake", env="PROD"
)
feature_urn = builder.make_ml_feature_urn(
    feature_table_name="txn_aggregates", feature_name="avg_txn_30d"
)

emitter.emit_mcp(
    MetadataChangeProposalWrapper(
        entityUrn=feature_urn,
        aspect=models.MLFeaturePropertiesClass(
            description="30-day average transaction value",
            sources=[dataset_urn],      # ← the "DerivedFrom" lineage edge
            dataType="CONTINUOUS",
        ),
    )
)
```

`sources` is what creates warehouse→ML lineage. Helper URN builders: `make_ml_feature_urn`, `make_ml_feature_table_urn`, `make_ml_primary_key_urn`.

Both `DatahubRestEmitter` and `DataHubRestEmitter` spellings resolve. Both `emit()` and `emit_mcp()` exist.

### A.4 datasetProfile — ✅ field names verified against the installed SDK

`datasetProfile` is a **timeseries** aspect, so `timestampMillis` is required.

```python
DatasetProfileClass(
    timestampMillis=int(time.time() * 1000),
    rowCount=..., columnCount=..., sizeInBytes=...,
    fieldProfiles=[
        DatasetFieldProfileClass(
            fieldPath="merchant_risk_score",
            quantiles=[QuantileClass(quantile="0.95", value="950")],
            distinctValueFrequencies=[ValueFrequencyClass(value="1", frequency=1)],
            histogram=HistogramClass(boundaries=["0","500","1000"], heights=[0.5,0.5]),
            nullCount=0, nullProportion=0.0,
            min="1", max="1000", mean="500.5", stdev="288.8",   # STRINGS
        )
    ],
)
```

Executed against `acryl-datahub 1.6.0.17`. `DatasetProfileClass` accepts `timestampMillis`, `rowCount`, `columnCount`, `sizeInBytes`, `fieldProfiles`; `DatasetFieldProfileClass` accepts every field above. Both tiers of the differ are pinned by `tests/test_sdk_assumptions.py`, which fails loudly on an SDK bump that moves them.

> **Still not verified end-to-end:** emitting a profile to a live GMS and reading it back. Class construction proves the shape, not the round trip — do that on Day 1 against quickstart.

### A.5 Structured properties on `mlModel` — ✅ resolved and executed

Open-source Core (CLI 0.13.1+), not Cloud-gated. Types: `string`, `number`, `date`, `urn`, `rich_text`. Cardinality: `SINGLE` / `MULTIPLE`.

Define with `StructuredProperties(...)` + `client.emit(mcp)`, listing `"mlModel"` in `entity_types`. The tutorial only demonstrates dataset/dataFlow/dataJob/schemaField, but `entity-registry.yml` confirms `mlModel`, `mlFeature`, and `mlFeatureTable` all declare the `structuredProperties` aspect.

**Assignment is solved, and more cleanly than the raw-aspect workaround I first assumed.** There is no `specific/mlmodel.py` — the `datahub/specific/` directory contains only `chart.py`, `dashboard.py`, `datajob.py`, `dataproduct.py`, `dataset.py`, `form.py`, `status.py`, `structured_property.py`. But the machinery is entity-agnostic:

- `datahub/specific/aspect_helpers/structured_properties.py` defines `HasStructuredPropertiesPatch(MetadataPatchProposal)` — a **mixin**, with `set_structured_property(key, value, attribution_source=None)`.
- `MetadataPatchProposal.__init__(self, urn, system_metadata=None, audit_header=None)` derives the entity type via `guess_entity_type(urn)`. **No allowlist, no entity check.**

So an Undertow-local patch builder composes from the existing mixin over the generic base:

```python
from datahub.emitter.mcp_patch_builder import MetadataPatchProposal
from datahub.specific.aspect_helpers.structured_properties import (
    HasStructuredPropertiesPatch,
)

class MLModelPatchBuilder(HasStructuredPropertiesPatch, MetadataPatchProposal):
    """No upstream equivalent exists; the base class imposes no entity restriction."""
```

This is ~5 lines, uses supported public machinery, and is a **clean OSS contribution candidate** — the gap is real and the fix is small.

**Executed, not just reasoned about.** Against `acryl-datahub 1.6.0.17`, that class produces exactly one MCP:

```
entityType: mlModel          # derived from the URN by guess_entity_type
aspectName: structuredProperties
changeType: PATCH
entityUrn : urn:li:mlModel:(urn:li:dataPlatform:mlflow,fraud_detector_v3,PROD)
```

`pkgutil.iter_modules(datahub.specific.__path__)` returns `aspect_helpers, chart, dashboard, datajob, dataproduct, dataset, form, status, structured_property` — no `mlmodel`, confirming the gap is still open upstream. `tests/test_sdk_assumptions.py` asserts both facts, so if upstream ships its own builder the suite tells us the contribution needs rescoping instead of us discovering it in review.

### A.6 MCP server — ✅ Core gating resolved

Gating is implemented in `mcp_server_datahub/version_requirements.py` via `@min_version(cloud=..., oss=...)`, where **`None` means "not available on that flavor."** It filters `on_list_tools` — gated tools are *hidden from discovery*, they do not raise at call time.

**Read tools are effectively ungated.** `search`, `enhanced_search`, `get_entities` *(plural — `get_entity` does not exist)*, `get_lineage`, `get_lineage_paths_between`, `list_schema_fields`, `get_dataset_queries` each carry a bare `@read_only` with no `@min_version` at all — their modules don't even import `min_version`. **Undertow's resolver is safe on Core at any GMS version.**

`search_documents`, `grep_documents`, `get_me` need OSS ≥ 1.4.0.

**Mutation tools work on Core** — all carry `@min_version(cloud="0.3.16", oss="1.4.0")`, including `add_tags` and `add_structured_properties`. Requirements: **GMS ≥ 1.4.0** *and* `TOOLS_IS_MUTATION_ENABLED` truthy (`register_mutation_tools` returns early otherwise). So MCP write-back is a genuinely available showcase path, not a hope.

**The only unambiguously Cloud-only tool is `get_dataset_assertions`** — the sole `@min_version` with no `oss=`. Note the asymmetry: Undertow can *emit* assertions to Core via the SDK, but cannot *read them back over MCP*. Read them via the SDK/GraphQL if needed.

`get_lineage` imposes **no entity-type whitelist** — types come from the caller's `compile_filters(filter)`, and it runs `searchAcrossLineage` under a degree filter. ML entities flow through the generic path.

> ⚠️ **Two residual sharp edges, both fail-closed, both worth 10 minutes on Day 2.**
>
> **① Silent version fail-closed.** `_get_server_version_info` defaults to an **all-zero version tuple** when it can't read `server_config`. `filter_tools_by_version` fails *open* on exceptions — but a successful-but-zero read fails *closed*, silently hiding every `@min_version` tool. Symptom: mutation tools missing with no error. Check `list_tools` output rather than assuming the env var didn't take.
>
> **② `#[CLOUD]` field stripping on self-hosted.** `graphql_helpers._is_datahub_cloud` does **not** read `server_config` — it heuristically probes `graph.frontend_base_url` (`ValueError` ⇒ not Cloud). For self-hosted Core, `#[CLOUD]`-marked GraphQL fields are commented out of the raw `.gql` source by line-level text transforms. Field-level thinning, not tool absence, is the real Core risk. `DISABLE_NEWER_GMS_FIELD_DETECTION=true` forces the non-Cloud path for both marker families.
>
> Unread: `gql/entity_details.gql`. If its selection set uses per-type inline fragments, ML entities could traverse successfully but return thin detail. Confirm on Day 2 with a real ML URN.

*(Unresolved, and not load-bearing: the CHANGELOG says `update_description` became Cloud-only in 0.5.1, while the source on `main` still shows `oss="1.4.0"`. Undertow doesn't use it.)*

### A.7 UI lineage rendering for ML entities — ✅ resolved

**`mlModel`: yes.** `datahub-web-react/src/app/entityV2/mlModel/MLModelEntity.tsx` declares the full dataset-equivalent pattern:

```tsx
name: i18next.t('entity.types:tab.lineage'),
component: LineageTab,
icon: PartitionOutlined,
supportsFullsize: true,
```

plus a sidebar `LineageExplore` section, `EntityCapabilityType.LINEAGE` in `getGenericEntityProperties`, and a `getLineageVizConfig` implementation.

**`mlFeature`: yes** — all four markers present, and `isLineageEnabled = () => true`.

**`mlFeatureTable`: partial** — declares `isLineageEnabled = () => true` and renders as a node inside *other* entities' graphs, but has **no Lineage tab on its own page**. Consistent with §1.3: feature tables sit outside the lineage graph.

Only `entityV2/` exists on `master`; the v1 `entity/` ML paths 404. The lineage visualisation handles entity types generically — no whitelist.

**Consequence for the demo: point the video at an `mlModel` page, not a feature table.**

> ⚠️ Frontend declarations are confirmed; the **backend GraphQL resolver actually returning ML relationships against real data** is not independently confirmed. `LineageRegistry.java` builds the graph from any `isLineage: true` relationship, so this should hold — but verify visually on Day 1 before scripting the video beat around it.

### A.8 Remaining open question

| # | Question | Impacts | Fallback |
|---|---|---|---|
| 1 | Does `datasetProfile` survive a live emit → read-back round trip? | Tier-2 statistical differ | Tier-1 checks (null rate, cardinality, mean shift, row count) run on default-profiled data; schema + governance checks still BLOCK |

Narrowed by execution: the aspect classes accept every field A.4 uses, so the remaining risk is transport and storage, not schema. Everything else that was open is closed against source or against the installed SDK. Its fallback costs no feature — the two-tier differ in §2.3 was redesigned specifically so Tier 1 stands alone.

### A.9 What is pinned by tests

`tests/test_sdk_assumptions.py` converts this appendix from prose into executable assertions, skipped automatically when the SDK is absent. It pins:

| Claim | Test |
|---|---|
| No upstream `mlModel` patch builder (A.5) | `test_no_upstream_mlmodel_patch_builder_exists` |
| `MLModelPatchBuilder` emits a valid `mlModel` PATCH (A.5) | `test_mlmodel_patch_builder_produces_a_valid_patch` |
| Two-hop path fields exist — `mlFeatures`, `sources` (§1.3) | `test_two_hop_path_fields_exist` |
| Tier-1 profile fields exist (A.4) | `test_tier_1_profile_fields_exist` |
| Tier-2 profile fields exist but are opt-in (A.4) | `test_tier_2_profile_fields_exist_but_are_opt_in` |
| `rowCount` is on the profile, not the field profile (A.4) | `test_row_count_lives_on_the_dataset_profile` |
| Custom assertion write-back constructs (§2.6) | `test_custom_assertion_write_back_constructs` |
| Assertion result types the reporter maps onto (§2.6) | `test_assertion_result_types_are_what_the_reporter_maps_onto` |

The last one records a design consequence worth stating plainly: `AssertionResultTypeClass` offers only `SUCCESS`, `FAILURE`, `ERROR`, `INIT`. **There is no native WARN.** The reporter therefore maps CLEAR and WARN both onto `SUCCESS`, BLOCK onto `FAILURE`, and carries the real severity in the description — a lossy mapping that has to stay deliberate, since a WARN written back as `FAILURE` would make Undertow look like it blocks on statistics.
