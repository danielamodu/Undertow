# Undertow

[![CI](https://github.com/danielamodu/Undertow/actions/workflows/ci.yml/badge.svg)](https://github.com/danielamodu/Undertow/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](pyproject.toml)

**Undertow blocks a bad ML model deploy by tracing a schema change through DataHub's lineage graph to the model — and the engineer whose change caused it.**

An agent gathers the context. A deterministic policy engine decides. The split isn't a design promise, it's enforced by the type system: the investigation loop consumes and produces `Finding`, and `Finding` has no severity field. There is a test asserting the verdict is byte-identical with and without the agent running.

## See it in 60 seconds — no DataHub, no Docker, no API key

```
git clone https://github.com/danielamodu/Undertow.git && cd Undertow
pip install -e ".[dev]"
undertow demo
```

Runs the whole gate — CLEAR on the approved graph, then a dropped column, then BLOCK on two models owned by two different teams who've never spoken:

```
🔴 BLOCK — fraud_detector_v3 (1 blocking, 0 warning)

┌────────────────────────────────────── BLOCKING ───────────────────────────────────────┐
│ Feature `transaction_velocity_7d` — Column `transaction_amount` was dropped from      │
│ transactions.raw                                                                      │
│                                                                                       │
│ transactions.raw.transaction_amount (@data_eng_tom)                                   │
│ └── staging.transactions_clean [DownstreamOf]                                         │
│     └── transaction_velocity_7d [DerivedFrom]                                         │
│         └── fraud_detector_v3 [Consumes]                                              │
│                                                                                       │
│   Confidence: CERTAIN (column dropped)                                                │
└───────────────────────────────────────────────────────────────────────────────────────┘
exit: 1

🔴 BLOCK — churn_predictor_v1 (1 blocking, 0 warning)
  ... same column, different team, different model. Neither team knew they shared a table.
exit: 1
```

This replays a graph recorded from a live DataHub OSS v1.7.0 instance (`scripts/record_fixture.py`). The differs, attribution, policy engine, reporter and exit codes are the same code a live run uses, on the same data — only the resolver's live connection is swapped for a recording. `--mcp` runs it against a real instance.

Captured output for every scenario (CLEAR, WARN, BLOCK, blast radius, PR comment, write-back proof) is in `examples/`. Tests need no DataHub: `pytest -q`.

## Why you can trust the verdict

This is the part that matters for a production gate: **the agent cannot change the outcome.**

- The investigation loop runs *after* the policy engine has already decided. It can only attach explanation.
- This isn't a prompt instruction — it's structural. `Finding` has no severity field, so there's nothing for the agent to influence even if it tried.
- `tests/test_investigator.py::test_investigation_cannot_change_the_verdict` asserts the verdict is byte-identical whether `--investigate` runs or not.
- Schema and governance changes are graded `CERTAIN` and may block. Statistical drift is graded `PROBABLE` and *cannot* block unless a team explicitly opts in — a distribution moving is evidence something may be wrong, not proof.
- Fails closed: if the resolver can't reach DataHub, it returns exit code `2` (error), never a false `CLEAR`. An empty footprint diffs clean against any baseline, so a broken connection must never be allowed to look like a passing check.

| Exit code | Meaning |
|---|---|
| `0` | CLEAR (or WARN, unless `--fail-on-warn`) |
| `1` | BLOCK — the gate ran and said stop |
| `2` | ERROR — the gate could not produce a verdict |

## The problem

A column gets dropped in a staging table, or a feature's mean shifts by 3σ, and a production model keeps serving predictions without error. Data engineers don't know which models sit downstream of their schemas. ML engineers don't find out until accuracy drops in prod.

DataHub already does impact analysis — open any dataset and see what's downstream, models included. Undertow doesn't reinvent that graph; it adds three things on top of it:

1. **A diff against an approved baseline, not a view of the present.** Impact analysis answers "what's downstream of this?" Undertow answers "what changed since this model was last approved, and does it reach the model?" — which is why `undertow_baseline` gets written back into the catalog.
2. **Schema changes and statistical drift, joined on the same lineage path, graded differently on purpose** — CERTAIN vs. PROBABLE, as above.
3. **A verdict with an exit code, in CI, before the deploy** — not a dashboard you check after being paged.

If you only need "what's downstream of this table," use DataHub's impact analysis. Undertow is for when you want that answer to stop a deploy.

## How it works

```
transactions.raw                                   (@data_eng_tom)
└── staging.transactions_clean                     [DownstreamOf]
    ├── transaction_velocity_7d                    [DerivedFrom]
    │   └── fraud_detector_v3     (@ml_eng_alex)   [Consumes]
    │       └── deployment: fraud_detector_prod
    └── customer_txn_volume                        [DerivedFrom]
        └── churn_predictor_v1    (@ml_eng_priya)  [Consumes]
```

- **Lineage traversal** resolves the full upstream footprint of an `mlModel` across ML relationships and multi-hop dataset lineage, so a change in a raw table is found through the staging layer between it and the feature.
- The fixture's lineage isn't hand-written — `staging.transactions_clean`'s schema and column-level lineage come from running DataHub's own `sqlglot_lineage` parser over its SQL. The `transaction_amount → amount` edge exists because `CAST(transaction_amount AS DECIMAL(10, 2)) AS amount` was parsed.
- **Blast radius**: one dropped column reaches every downstream model, not just the one you're deploying.
- **Root-cause attribution** walks findings back to the source asset and resolves the technical owner.
- **Native write-back**: `assertionInfo` + `assertionRunEvent`, `undertow:blocked`/`undertow:cleared` tags, and structured properties, written into DataHub as standard aspects — no separate datastore.

## Architecture

Six decoupled layers. The LLM sits off to the side of the decision, never in it.

```
  model URN
      │
      ▼
┌───────────┐   ┌──────────┐   ┌────────────┐   ┌───────────────┐
│ RESOLVER  │──▶│  DIFFER  │──▶│ ATTRIBUTOR │──▶│ POLICY ENGINE │
│ walk the  │   │ schema ∙ │   │ root cause │   │   verdict     │
│   graph   │   │  stats   │   │  + owners  │   │  + exit code  │
└─────┬─────┘   └──────────┘   └────────────┘   └───────┬───────┘
      │                                                 │
      │ MCP / SDK                    ┌──────────────────┴────────┐
      │                              ▼                           ▼
      │                     ┌─────────────────┐        ┌──────────────────┐
      │                     │  INVESTIGATOR   │        │    NARRATOR      │
      │                     │  agent loop —   │        │ prose, or a      │
      │                     │  adds context,  │        │ template. Never  │
      │                     │  never severity │        │ decides anything │
      │                     └────────┬────────┘        └────────┬─────────┘
      │                              └─────────────┬────────────┘
      │                                            ▼
      │                                    ┌───────────────┐
      │                                    │   REPORTER    │
      │                                    │ console ∙ PR  │
      │                                    │ ∙ write-back  │
      │                                    └───────┬───────┘
      ▼                                            │
┌──────────────────────────────────────────────────▼──────────┐
│                          DATAHUB                            │
│  reads: MCP server (get_entities, get_lineage, schema)      │
│  writes: REST emitter (assertions, tags, structured props)  │
└──────────────────────────────────────────────────────────────┘
```

- **Resolver** — traverses the graph via DataHub's MCP server (or SDK fallback) into `DependencyFootprint` snapshots.
- **Differ** — set differences across schema, governance, and statistical profiles. Statistics are limited to what DataHub profiles by default (null-rate, cardinality, z-score mean shift, range, row count); a missing statistic is reported as "cannot assess," never "no drift."
- **Attributor** — origin-to-leaf `AttributionPath` chains + technical owners.
- **Policy engine** — deterministic rules against `undertow.yaml`.
- **Narrator** — bounded LLM prose (`claude-sonnet-4-6`) with a Jinja2 template fallback and URN hallucination check.
- **Reporter** — console tree, GitHub PR markdown, DataHub REST write-back.

## Catching it before the merge

`undertow check` runs at deploy time, after the damage is done. `undertow impact` runs on the pull request that changes the SQL, before it merges:

```
undertow impact examples/pr-drops-amount.sql

staging.transactions_clean — removes amount
    reaches churn_predictor_v1 (@ml_eng_priya)
      via customer_txn_volume → churn_predictor_v1
    reaches fraud_detector_v3 (@ml_eng_alex)
      via transaction_velocity_7d → fraud_detector_v3
```

Informational by default — the check doesn't know if a removal is intentional, and a PR check that fails on every deliberate column drop gets disabled within a week. `--fail-on-impact` makes it blocking. See `examples/github-pr-comment-upstream.md` and `.github/workflows/upstream-pr-gate.yml`.

## DataHub Skill

`skills/undertow-deploy-gate/` is a DataHub Skill — it gives an agent judgment about *when* to reach for the gate, not just tools to call. The part worth reading is what it forbids:

> You do not decide whether the deploy proceeds. Undertow does.
> If you find yourself looking for a flag that makes the red box go away, stop and tell the user what the finding actually is.

It will not edit `undertow.yaml` to downgrade a BLOCK, re-baseline to silently clear a finding, or report exit code 2 as a pass. `tests/test_skill.py` asserts every command the skill references actually exists.

## Quick start (against a real DataHub instance)

**Prerequisites:** Python 3.11+, Docker Desktop, DataHub OSS (verified against v1.7.0)

```
git clone https://github.com/danielamodu/Undertow.git
cd Undertow
pip install -e ".[dev]"
datahub docker quickstart
make seed
make baseline
```

`.[dev]` installs everything documented, including the MCP client/server and the Anthropic SDK. Leaner installs: `.[mcp]` or `.[llm]` — the core package needs neither.

### Demo: drop a column, check the blast radius

```
make break            # drops transaction_amount from transactions.raw
make blast-radius     # checks every downstream model
make check-write      # gate + write verdict back to DataHub
make reset            # restore the baseline graph
```

Windows has no `make` — every target also exists as `.\demo.ps1 <target>` (`.\demo.ps1 help` for the list).

| Command | What it does |
|---|---|
| `make seed` | Build the fixture graph |
| `make baseline` | Capture the approved state |
| `make break` | Drop a column — CERTAIN change |
| `make break-stats` | Shift a distribution 4.8σ — PROBABLE change |
| `make break-governance` | Deprecate a table, tag PII |
| `make check-warn` | Gate after drift: WARN, exit 0 |
| `make check` | Gate the fraud model |
| `undertow check --all` | Gate every model in `undertow.yaml` |
| `make blast-radius` | Gate every downstream model |
| `make check-write` | Gate + write verdict to DataHub |
| `make impact` | Check a proposed SQL change pre-merge |
| `make history` | Every recorded verdict, from DataHub |
| `make check-mcp` | Resolve lineage via MCP instead of SDK |
| `make check-investigate` | Add the agent investigation loop (needs `--mcp` + `ANTHROPIC_API_KEY`) |
| `make reset` | Restore the graph |

## Using this on your own models

1. **Find the model, approve today's state.**
   ```
   datahub search "fraud_detector" -f entity_type=mlModel --urns-only
   undertow baseline --model "<that URN>"
   ```
2. **List gated models in `undertow.yaml`** — `undertow check --all` sweeps them and exits with the worst outcome, so a forgotten model is visible in a reviewed file instead of a silent gap.
3. **Add it to the deploy pipeline:**
   ```yaml
   - uses: danielamodu/Undertow@main
     with:
       model: ${{ vars.MODEL_URN }}
       datahub-url: ${{ secrets.DATAHUB_GMS_URL }}
       datahub-token: ${{ secrets.DATAHUB_GMS_TOKEN }}
   ```
4. **Re-baseline deliberately** when a change is intentional — `undertow baseline --model …` is an explicit, named act, not a flag that suppresses a warning.
5. Optionally gate the SQL repo itself with `upstream-pr-gate.yml`, so the engineer removing a column finds out on their own PR.
6. Tune `undertow.yaml` — severity per finding kind, thresholds, exemptions with a required expiry.

**What it needs from you:** column-level lineage in DataHub (or findings degrade to table-level), profiling for statistical checks, and a baseline per model. No baseline → exit `2`, not a guess.

## DataHub integration details

- **Reads** via the Agent Context Kit's MCP server (`--mcp`): `get_entities`, `get_lineage`, `list_schema_fields`, checked against the server's live `tools/list` at connect time so a signature drift fails loudly. SDK fallback (`SdkLineageSource`) resolves the same graph for environments without the MCP server.
- **Writes** via the REST emitter, not MCP — the OSS `mcp-server-datahub` build ships with mutation tools disabled, so reads and writes take different paths on purpose.
- Verified against `mcp-server-datahub` 0.6.0 on DataHub OSS v1.7.0.
- Write-back: `assertionInfo`/`assertionRunEvent` on root-cause datasets, `undertow:blocked`/`undertow:cleared` tags on `mlModel`, structured properties (`undertow_risk_verdict`, `undertow_last_checked`, `undertow_baseline`), and institutional-memory links back to the PR and run.

### Verdict history

Every `--write-back` appends to a native DataHub assertion (a timeseries aspect), so runs accumulate rather than overwrite — visible in DataHub's own assertion timeline, not a store Undertow invented.

```
undertow history --model "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detector_v3,PROD)"

              Verdict history — fraud_detector_v3
┌─────────────────────────┬─────────┬──────────┬─────────┬────────┬──────┐
│ Checked at              │ Verdict │ Blocking │ Warning │ Assets │ Exit │
├─────────────────────────┼─────────┼──────────┼─────────┼────────┼──────┤
│ 2026-08-07 21:08:14 UTC │ BLOCK   │        1 │       0 │      6 │    1 │
│ 2026-08-07 16:46:20 UTC │ BLOCK   │        1 │       0 │      6 │    1 │
│ 2026-08-06 11:18:32 UTC │ BLOCK   │        1 │       0 │      5 │    1 │
└─────────────────────────┴─────────┴──────────┴─────────┴────────┴──────┘
```

## Composing with other agents

Undertow writes verdicts into the catalog, not into a store of its own — so the next agent to read that catalog inherits them without integrating with Undertow at all. DataHub's Analytics Agent reads documentation and context before writing SQL; after an Undertow run, a model carries a tag, a structured property, and a native assertion with run history, and any agent asking about that model gets an answer shaped by all of it.

## OSS contribution

`MLModelPatchBuilder` for `datahub/specific/` — [upstream PR datahub-project/datahub#18979](https://github.com/datahub-project/datahub/pull/18979), closes [#18971](https://github.com/datahub-project/datahub/issues/18971). Vendored here so Undertow runs today: `contrib/datahub-mlmodel-patch-builder/`.

DataHub ships patch builders (surgical PATCH, not lossy full UPSERT) for chart, dashboard, dataJob, dataProduct, dataset, form, and structuredProperty — but not for `mlModel`, so its aspects were UPSERT-only. That's a live problem: `examples/datahub-writeback.json` shows a property written by something else surviving three independent PATCH writes from Undertow; a full UPSERT would have destroyed it. 13-test suite, no DataHub required: `pytest contrib/datahub-mlmodel-patch-builder/ -q`.

## Prior art

Two 2025 papers by Leest et al. frame the gap this occupies:
- [arXiv:2510.24142](https://arxiv.org/abs/2510.24142) — *Monitoring and Observability of Machine Learning Systems* — documents current practices and gaps in fault detection and degradation explanation.
- [arXiv:2510.23528](https://arxiv.org/abs/2510.23528) — *Tracing Distribution Shifts with Causal System Maps* — proposes explicit propagation paths for attributing distribution shift; explicitly a research agenda, not an implementation.

Undertow implements that attribution framing against a lineage graph that already exists in production, as a blocking pre-deploy gate.

## License

Apache License 2.0
