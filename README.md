# Undertow
> Stop deploying models on broken data.

Undertow is a lineage-grounded pre-deploy gate for production ML models, built on DataHub. It traverses the metadata graph from an `mlModel` back through its feature pipelines and staging layers to the raw tables underneath, diffs what it finds against an approved baseline, and names the engineer who owns the change. If a breaking upstream change threatens a model, Undertow blocks the deploy in CI and writes native assertions, tags, and structured risk properties back into the catalog — so the next run, on any machine, starts from what the last one learned.

It is stateless by design. DataHub is the source of truth for topology and the store for verdict history; the only local artifact is a baseline snapshot, and that is mirrored into DataHub too.

---

## The Problem

Production ML models silently degrade when upstream data contracts shift without notice. A column is dropped in a Snowflake staging table, an upstream engineer relaxes nullability, or a feature mean shifts by 3σ — yet the model continues serving predictions without error. Data engineers don't know which models depend on their schemas, and ML engineers don't discover the failure until model accuracy plunges in production.

---

## How It Works

Undertow walks the chain the *Production ML Agents* challenge describes — training data to features to models to deployments — and stops a deploy at the end of it.

```
transactions.raw                                   (@data_eng_tom)
└── staging.transactions_clean                     [DownstreamOf]
    ├── transaction_velocity_7d                    [DerivedFrom]
    │   └── fraud_detector_v3     (@ml_eng_alex)   [Consumes]
    │       └── deployment: fraud_detector_prod
    └── customer_txn_volume                        [DerivedFrom]
        └── churn_predictor_v1    (@ml_eng_priya)  [Consumes]
```

- **Lineage Traversal**: Resolves the full upstream footprint of an `mlModel` across ML relationships (`mlModel --Consumes--> mlFeature --DerivedFrom--> dataset`) *and* multi-hop dataset lineage, so a change in a raw table is still found through the staging layer that sits between it and the feature.
- **Blast Radius**: One dropped column reaches every model downstream of it, not just the one you happened to be deploying. Above, a single change to `transactions.raw` blocks two models owned by two teams who have never spoken.
- **Multi-Aspect Drift Detection**: Compares live graph metadata against an approved baseline for schema changes, governance shifts, and statistical drift.
- **Root-Cause Attribution**: Walks findings back to root-cause assets and resolves technical owners so the right engineer is named on the failure.
- **Native DataHub Write-Back**: Emits native `assertionInfo` + `assertionRunEvent`, risk tags (`undertow:blocked`/`cleared`), and `structuredProperties` back into the catalog.

---

## Quick Start

### Prerequisites
- Python 3.11+
- Docker Desktop
- DataHub running locally

### Setup
```bash
git clone https://github.com/danielamodu/Undertow.git
cd Undertow
pip install -e ".[dev]"
datahub docker quickstart
make seed
make baseline
```

---

## Demo

Drop a column from an upstream table, then ask both models whether they still stand:

```bash
# Drop transaction_amount from transactions.raw
make break

# Check every model downstream of it
make blast-radius
```

### Output

Verbatim from a live run against DataHub v1.7.0:

```
🔴 BLOCK — fraud_detector_v3 (1 blocking, 0 warning)

┌───────────────────────────────── BLOCKING ──────────────────────────────────┐
│ Feature `transaction_velocity_7d` — Column `transaction_amount` was dropped │
│ from transactions.raw                                                       │
│                                                                             │
│ transactions.raw.transaction_amount (@data_eng_tom)                         │
│ └── staging.transactions_clean [DownstreamOf]                               │
│     └── transaction_velocity_7d [DerivedFrom]                               │
│         └── fraud_detector_v3 [Consumes]                                    │
│                                                                             │
│   Confidence: CERTAIN (column dropped)                                      │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘

🔴 BLOCK — churn_predictor_v1 (1 blocking, 0 warning)

┌───────────────────────────────── BLOCKING ──────────────────────────────────┐
│ Feature `customer_txn_volume` — Column `transaction_amount` was dropped     │
│ from transactions.raw                                                       │
│                                                                             │
│ transactions.raw.transaction_amount (@data_eng_tom)                         │
│ └── staging.transactions_clean [DownstreamOf]                               │
│     └── customer_txn_volume [DerivedFrom]                                   │
│         └── churn_predictor_v1 [Consumes]                                   │
│                                                                             │
│   Confidence: CERTAIN (column dropped)                                      │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

The fraud team's change broke the churn team's model. Neither team knew the other was downstream of the same table; the graph did.

Add `--write-back` to record the verdict in DataHub:

```bash
make check-write
```

```
✍ Written to DataHub → fraud_detector_v3
```

To restore the baseline graph at any time:
```bash
make reset
```

### Commands

| Command | What it does |
|---|---|
| `make seed` | Build the fixture graph: datasets, staging layer, features, two models, model group, deployment |
| `make baseline` | Capture the approved state of both models |
| `make break` | Drop `transaction_amount` from `transactions.raw` |
| `make check` | Gate the fraud model |
| `make blast-radius` | Gate every model downstream of the change |
| `make check-write` | Gate and write the verdict back to DataHub |
| `make check-mcp` | Resolve lineage through the DataHub **MCP server** instead of the SDK |
| `make check-investigate` | Add the agent investigation loop (needs `--mcp` and `ANTHROPIC_API_KEY`) |
| `make reset` | Restore the graph |

### Exit codes

Undertow is a CI gate, so its exit status is part of its contract:

| Code | Meaning |
|---|---|
| `0` | CLEAR — proceed. Also WARN, unless `--fail-on-warn` |
| `1` | BLOCK — the gate ran and said stop |
| `2` | ERROR — the gate could **not** produce a verdict |

`1` and `2` are deliberately distinct. A pipeline needs to tell "your upstream broke" from "Undertow broke", and those demand different responses. Undertow fails closed: if it cannot reach DataHub, it exits `2` rather than reporting a green light it never actually earned.

---

## Architecture

Undertow is structured into 6 decoupled pipeline layers:

$$\text{Resolver} \longrightarrow \text{Differ} \longrightarrow \text{Attributor} \longrightarrow \text{Policy Engine} \longrightarrow \text{Narrator} \longrightarrow \text{Reporter}$$

1. **Resolver**: Traverses the graph via DataHub's MCP server (or a fallback Python SDK client) to construct `DependencyFootprint` snapshots.
2. **Differ**: Evaluates set differences across schema, governance, and statistical profiles.
3. **Attributor**: Attaches origin-to-leaf `AttributionPath` chains and resolves technical owners.
4. **Policy Engine**: Evaluates rule severity deterministically against `undertow.yaml` rules and exemptions.
5. **Narrator**: Bounded LLM prose generator (`claude-sonnet-4-6`) with Jinja2 template fallback & URN hallucination validation.
6. **Reporter**: Rich console boxed tree renderer, GitHub Actions PR Markdown reporter, and DataHub REST emitter write-back.

### Key Architectural Guarantees

- **Deterministic Core**: No LLM sits in the blocking path. Rules evaluate deterministically, so the same graph and the same baseline always produce the same verdict.
- **The agent cannot change the verdict.** When `--investigate` is on, the investigation loop runs *after* the policy engine has decided, and it can only add explanation. This is enforced structurally rather than by instruction, and there is a test that asserts the verdict is byte-identical with and without the agent.
- **Fails closed.** A resolver that cannot reach DataHub records the failure instead of returning an empty footprint. An empty footprint would otherwise diff clean against any baseline and surface as a confident CLEAR produced by a broken connection.
- **Statistics limited to what DataHub profiles by default**: null-rate jumps, cardinality shifts, z-score mean shifts, range violations, and row-count changes — all from fields that are `default=True` on a stock instance. An earlier revision computed PSI from quantiles and histograms; it was removed because DataHub gates quantiles behind three separate `default=False` flags *and* a cardinality check, so it worked on our fixture and returned nothing on anyone else's DataHub.
- **A missing statistic means "cannot assess", never "no drift."** `profile_coverage` exists so a CLEAR verdict can state honestly how much of the footprint it was able to inspect.
- **CERTAIN vs. PROBABLE**: Schema and governance changes are `CERTAIN`. Statistical drift is `PROBABLE`, and the policy engine refuses to let a PROBABLE finding BLOCK unless a team explicitly opts in — a distribution moving is evidence something *may* be wrong, and blocking on evidence that weak is how a gate loses its welcome.
- **Native Write-Back**: Standard DataHub metadata change proposals. No custom datastore.

---

## DataHub Integration

Undertow holds no database of its own. DataHub is the source of truth for topology *and* the store for verdict history — including the baseline, which means a wiped CI runner can still tell you what changed since the last approved deploy.

- **Graph Traversal via the MCP Server**: A real MCP client (`--mcp`) spawns `mcp_server_datahub` over stdio, keeps one initialised session alive for the whole run, and calls `get_entities`, `get_lineage`, and `list_schema_fields`. Tool names are checked against the server's own `tools/list` at connect time, so a signature drift fails loudly instead of silently resolving to nothing. Verified against mcp-server-datahub 0.6.0, whose OSS build exposes eight read-only tools.
- **SDK fallback**: `SdkLineageSource` covers environments without the MCP server. Both paths resolve the same graph and produce the same verdict.
- **Write-Back Metadata**:
  - `assertionInfo` (CUSTOM / EXTERNAL) & `assertionRunEvent` (SUCCESS / FAILURE) on root cause datasets.
  - `globalTags`: `undertow:blocked` and `undertow:cleared` on `mlModel`.
  - `structuredProperties`: `undertow_risk_verdict`, `undertow_last_checked`, and `undertow_baseline` snapshots on `mlModel`.
  - `institutionalMemory`: Audit links back to GitHub PRs and execution runs.

  Write-back goes through the REST emitter rather than the MCP server because the OSS build of `mcp-server-datahub` gates its mutation, data-quality, and user tools off — the server logs `Mutation Tools DISABLED` on startup. Reads come from MCP; writes take the path that actually exists.

---

## OSS Contribution

**`MLModelPatchBuilder` for `datahub/specific/`** — proposed in [`contrib/datahub-mlmodel-patch-builder/`](contrib/datahub-mlmodel-patch-builder/mlmodel.py).

DataHub's `entity_client.update()` branches on its argument: an `Entity` becomes a full-aspect `UPSERT`, a `MetadataPatchProposal` becomes a surgical `PATCH`. `datahub/specific/` ships patch builders for chart, dashboard, dataJob, dataProduct, dataset, form, and structuredProperty — but none for an ML entity, so `mlModel` aspects are `UPSERT`-only unless you hand-roll one.

That matters because `UPSERT` is lossy. Verified against a live GMS:

| Write path | Result |
|---|---|
| Two independent `PATCH` writes | Both properties survive |
| One full-aspect `UPSERT` | The property the writer didn't know about is destroyed |

Undertow writes `undertow_risk_verdict`, `undertow_last_checked`, and `undertow_baseline` as separate concerns, so this is a live problem rather than a theoretical one.

The proposed builder composes DataHub's existing entity-agnostic mixins (`HasOwnershipPatch`, `HasTagsPatch`, `HasStructuredPropertiesPatch`, …) exactly as `DataProductPatchBuilder` does — no new machinery, just the missing composition, and 19 mutation methods rather than the 2 our vendored version exposes.

*Note: `datahub.sdk.MLModel` already supports mlModel structured properties via the newer SDK layer. The gap is specifically the absence of a `PATCH`-emitting builder, not the absence of mlModel support.*

---

## Prior Art & Research

Two 2025 papers by Leest et al. frame the gap Undertow occupies:

- **[arXiv:2510.24142](https://arxiv.org/abs/2510.24142)** — *Monitoring and Observability of Machine Learning Systems: Current Practices and Gaps.* Seven focus groups with practitioners; documents how teams validate models, detect faults, and explain degradations, and where that practice falls short.
- **[arXiv:2510.23528](https://arxiv.org/abs/2510.23528)** — *Tracing Distribution Shifts with Causal System Maps.* Proposes making the propagation paths between environment and ML internals explicit so distribution shifts can be attributed rather than merely detected. Explicitly work-in-progress: an approach and research agenda, not an implementation.

Undertow implements that attribution framing against a lineage graph that already exists in production — DataHub — and makes the result a blocking pre-deploy gate.

---

## V2 Roadmap

- **Continuous Watch Mode**: Daemon process monitoring DataHub Kafka events for real-time upstream drift notifications.
- **Slack & Webhook Integrations**: Instant alerts dispatched to dataset owners when an upstream change threatens a downstream model.
- **Historical Verdict Timeline**: Historical risk trends rendered in DataHub's UI timeline.
- **Automated Data PR Comments**: Bot comments posted directly on dbt/SQL pull requests modifying upstream schema definitions.

*Multi-model blast radius was on this list and is now shipped — see `make blast-radius`.*

---

## License

[Apache License 2.0](LICENSE)
