# Undertow
> Stop deploying models on broken data.

Undertow is a lineage-grounded pre-deploy gate for production ML models built on DataHub. It traverses DataHub's metadata graph from an `mlModel` through its feature pipelines to upstream raw datasets, detects schema and statistical drift against an approved baseline, and attributes root causes directly to the responsible data owners. If a breaking upstream change threatens model validity, Undertow blocks the deployment in CI and writes native assertions, tags, and structured risk properties back to DataHub.

---

## The Problem

Production ML models silently degrade when upstream data contracts shift without notice. A column is dropped in a Snowflake staging table, an upstream engineer relaxes nullability, or a feature mean shifts by 3σ — yet the model continues serving predictions without error. Data engineers don't know which models depend on their schemas, and ML engineers don't discover the failure until model accuracy plunges in production.

---

## How It Works

```
upstream dataset ──> ML feature ──> ML model ──> deploy gate
  (transactions)   (velocity_7d)   (fraud_v3)    (Undertow)
```

- **Lineage Traversal**: Resolves the full upstream data footprint of an `mlModel` across two-hop ML relationships (`mlModel --Consumes--> mlFeature --DerivedFrom--> dataset`) and dataset lineage.
- **Multi-Aspect Drift Detection**: Compares live graph metadata against an approved baseline for schema changes, governance shifts, and statistical distribution drift.
- **Root-Cause Attribution**: Walks findings back to root-cause data assets and resolves technical owners so the right engineer is immediately notified.
- **Native DataHub Write-Back**: Emits native `assertionInfo` + `assertionRunEvent`, risk tags (`undertow:blocked`/`cleared`), and `structuredProperties` back into DataHub's catalog.

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

Simulate a breaking upstream change and execute the pre-deploy check with DataHub write-back:

```bash
# Drop transaction_amount from transactions.raw
make break

# Run Undertow deploy gate and write verdict back to DataHub
make check-write
```

### Expected Output

```
🔴 BLOCK — urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detector_v3,PROD) (1 blocking, 0 warning)

┌───────────────────────────────── BLOCKING ──────────────────────────────────┐
│ Feature `(fraud_detection,transaction_velocity_7d)` — Column                │
│ `transaction_amount` was dropped from transactions.raw, which feeds         │
│ transaction_velocity_7d                                                     │
│                                                                             │
│ urn:li:dataset:(urn:li:dataPlatform:snowflake,transactions.raw,PROD).transaction_amount
│ └── urn:li:mlFeature:(fraud_detection,transaction_velocity_7d) [DerivedFrom]
│     └── urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detector_v3,PROD) [Consumes]
│                                                                             │
│   Confidence: CERTAIN (column dropped)                                      │
└─────────────────────────────────────────────────────────────────────────────┘

✍ Written to DataHub → urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detector_v3,PROD)
```

To restore the baseline graph at any time:
```bash
make reset
```

---

## Architecture

Undertow is structured into 6 decoupled pipeline layers:

$$\text{Resolver} \longrightarrow \text{Differ} \longrightarrow \text{Attributor} \longrightarrow \text{Policy Engine} \longrightarrow \text{Narrator} \longrightarrow \text{Reporter}$$

1. **Resolver**: Traverses the graph via DataHub's MCP server (or fallback Python SDK client) to construct `DependencyFootprint` snapshots.
2. **Differ**: Evaluates set differences across schema, governance, and two-tier statistical profiles.
3. **Attributor**: Attaches origin-to-leaf `AttributionPath` chains and resolves technical owners.
4. **Policy Engine**: Evaluates rule severity deterministically against `undertow.yaml` rules and exemptions.
5. **Narrator**: Bounded LLM prose generator (`claude-sonnet-4-6`) with Jinja2 template fallback & URN hallucination validation.
6. **Reporter**: Rich console boxed tree renderer, GitHub Actions PR Markdown reporter, and DataHub REST emitter write-back.

### Key Architectural Guarantees
- **Deterministic Core**: No LLMs in the critical blocking path. Rules evaluate deterministically to guarantee 100% reproducible CI results.
- **Two-Tier Statistical Differ**: Tier 1 computes null-rate jumps, cardinality shifts, and z-score mean shifts on standard profiling; Tier 2 evaluates Population Stability Index (PSI) when quantiles are available.
- **CERTAIN vs. PROBABLE Confidence**: Hard schema/governance changes carry `CERTAIN` confidence; statistical drift carries `PROBABLE` confidence.
- **Native Write-Back**: Emits standard DataHub metadata change proposals without custom database dependencies.

---

## DataHub Integration

Undertow leverages DataHub both as an input metadata graph and an output assertion target:

- **Graph Traversal**: Connects via DataHub MCP tools (`get_entities`, `get_lineage`, `list_schema_fields`) with an `SdkLineageSource` fallback.
- **Write-Back Metadata**:
  - `assertionInfo` (CUSTOM / EXTERNAL) & `assertionRunEvent` (SUCCESS / FAILURE) on root cause datasets.
  - `globalTags`: `undertow:blocked` and `undertow:cleared` on `mlModel`.
  - `structuredProperties`: `undertow_risk_verdict`, `undertow_last_checked`, and `undertow_baseline` snapshots on `mlModel`.
  - `institutionalMemory`: Audit links back to GitHub PRs and execution runs.

---

## OSS Contribution

Undertow introduces `MLModelPatchBuilder` (located in `src/undertow/reporter/datahub_writer.py`), which fills a gap in `datahub.specific` by composing `HasStructuredPropertiesPatch` over `MetadataPatchProposal` specifically for `mlModel` entities. This contribution enables fluently mutating structured properties on DataHub ML entities using the official SDK patch builder pattern.

---

## Prior Art & Research

Recent data governance and ML observability research (e.g., arXiv:2308.09341 and arXiv:2401.11890) proposed combining lineage graphs with dataset profiling for automated model risk attribution as future work. 

**Undertow is the first open-source implementation of this approach**, turning theoretical lineage attribution into an actionable, automated pre-deploy gate.

---

## V2 Roadmap

- **Continuous Watch Mode**: Daemon process monitoring DataHub Kafka events for real-time upstream drift notifications.
- **Slack & Webhook Integrations**: Instant alerts dispatched to dataset owners when an upstream change threatens a downstream model.
- **Historical Verdict Timeline**: Historical risk trends rendered in DataHub's UI timeline.
- **Multi-Model Blast Radius**: Single PR risk analysis showing all downstream models impacted by a single dataset schema migration.
- **Automated Data PR Comments**: Bot comments posted directly on dbt/SQL pull requests modifying upstream schema definitions.

---

## License

[Apache License 2.0](LICENSE)
