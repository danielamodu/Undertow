# Undertow

[![CI](https://github.com/danielamodu/Undertow/actions/workflows/ci.yml/badge.svg)](https://github.com/danielamodu/Undertow/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](pyproject.toml)

**Undertow blocks a bad ML model deploy by tracing a schema change through DataHub's lineage graph to the model — and the engineer whose change caused it.**

A column gets dropped from a table, or a feature's distribution shifts, and a production model keeps serving predictions without error. Data engineers don't know which models sit downstream of their schemas. ML engineers don't find out until accuracy drops in prod. Undertow sits in CI, between a model and a deploy: it walks the model's upstream footprint in DataHub, diffs it against the last approved state, and blocks the deploy when something upstream broke it.

An agent gathers context on top of that verdict. A deterministic policy engine decides it. The split isn't a design promise, it's enforced by the type system: the investigation loop consumes and produces `Finding`, and `Finding` has no severity field. There's a test asserting the verdict is byte-identical with and without the agent running.

## Install

```
pip install "undertow @ git+https://github.com/danielamodu/Undertow"
```

Or clone it and install from source — `pip install -e ".[dev]"` pulls in everything documented below, including the MCP client/server and both LLM SDKs. Leaner installs exist too: `.[mcp]` for lineage over the DataHub MCP server, `.[llm]` for `--investigate`. The core package needs neither.

Requires Python 3.11+ and a DataHub instance with lineage ingested for the models you want to gate. See [DataHub integration details](#datahub-integration-details) for what Undertow expects to find there.

Undertow loads a `.env` file on its own — `cp .env.example .env`, fill in `DATAHUB_GMS_URL` and whichever LLM provider key you're using, and every command in this README picks it up without exporting anything into the shell first. Already-exported environment variables still win over the file.

## Try it with no DataHub connection

```
git clone https://github.com/danielamodu/Undertow.git && cd Undertow
pip install -e ".[dev]"
undertow demo
```

This replays a graph recorded from a live DataHub OSS v1.7.0 instance (`scripts/record_fixture.py`) through the real differ, attribution, policy engine, and reporter — only the resolver's live connection is swapped for a recording. It's the fastest way to see the gate's actual output before pointing it at your own catalog:

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
```

Captured output for every scenario (CLEAR, WARN, BLOCK, blast radius, PR comment, write-back proof) is in `examples/`. `--mcp` runs the same commands against a real instance. Tests need no DataHub: `pytest -q`.

## What it adds over DataHub's own impact analysis

DataHub already does impact analysis — open any dataset and see what's downstream, models included. Undertow doesn't reinvent that graph; it adds three things on top of it:

1. **A diff against an approved baseline, not a view of the present.** Impact analysis answers "what's downstream of this?" Undertow answers "what changed since this model was last approved, and does it reach the model?" — which is why `undertow_baseline` gets written back into the catalog.
2. **Schema changes and statistical drift, joined on the same lineage path, graded differently on purpose.** A dropped column is `CERTAIN` and may block. A shifted distribution is `PROBABLE` and warns instead — a team has to opt in before drift alone can stop a deploy, because a gate that blocks on a hunch gets disabled inside a month.
3. **A verdict with an exit code, in CI, before the deploy** — not a dashboard you check after being paged.

If you only need "what's downstream of this table," use DataHub's impact analysis. Undertow is for when you want that answer to stop a deploy.

## Using this on your own models

1. **Find the model, approve today's state.**
   ```
   datahub search "your_model_name" -f entity_type=mlModel --urns-only
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
5. Optionally gate the SQL repo itself with `upstream-pr-gate.yml`, so the engineer removing a column finds out on their own PR, before it merges — see [Catching it before the merge](#catching-it-before-the-merge).
6. Tune `undertow.yaml` — severity per finding kind, thresholds, exemptions with a required expiry.

**What it needs from you:** column-level lineage in DataHub (or findings degrade to table-level attribution), profiling for statistical checks, and a baseline per model. No baseline → exit `2`, not a guess.

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
- **Blast radius**: one dropped column reaches every downstream model, not just the one you're deploying — the fixture above is two models on two teams, sharing an upstream table neither knew the other read.
- **Root-cause attribution** walks findings back to the source asset and resolves the technical owner.
- **Native write-back**: `assertionInfo` + `assertionRunEvent`, `undertow:blocked`/`undertow:cleared` tags, and structured properties, written into DataHub as standard aspects — no separate datastore.

### Architecture

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

- **Resolver** — traverses the graph via DataHub's MCP server (or SDK fallback) into `DependencyFootprint` snapshots. Where the catalog holds column-level lineage, findings are attributed per column rather than per table, so a dropped column implicates only the features it actually reaches — on both paths, from the same `fineGrainedLineages` aspect. Where it doesn't, attribution falls back to table-level rather than going silent.
- **Differ** — set differences across schema, governance, and statistical profiles. Statistics are limited to what DataHub profiles by default (null-rate, cardinality, z-score mean shift, range, row count); a missing statistic is reported as "cannot assess," never "no drift."
- **Attributor** — origin-to-leaf `AttributionPath` chains + technical owners.
- **Policy engine** — deterministic rules against `undertow.yaml`.
- **Narrator** — bounded LLM prose (`claude-sonnet-4-6`) with a Jinja2 template fallback and URN hallucination check.
- **Reporter** — console tree, GitHub PR markdown, DataHub REST write-back.

## Why you can trust the verdict

This is the part that matters for a production gate: **the agent cannot change the outcome.**

- The investigation loop runs *after* the policy engine has already decided. It can only attach explanation.
- This isn't a prompt instruction — it's structural. `Finding` has no severity field, so there's nothing for the agent to influence even if it tried.
- `tests/test_investigator.py::test_investigation_cannot_change_the_verdict` asserts the verdict is byte-identical whether `--investigate` runs or not.
- Fails closed: if the resolver can't reach DataHub, it returns exit code `2` (error), never a false `CLEAR`. An empty footprint diffs clean against any baseline, so a broken connection must never be allowed to look like a passing check.
- Says what it didn't look at. A walk stopped by `max_hops` reports how many assets still had unwalked upstreams, because a truncated footprint and a clean one produce the same CLEAR otherwise. Set `fail_on_truncation: true` to make that exit `2` rather than a verdict.

| Exit code | Meaning |
|---|---|
| `0` | CLEAR (or WARN, unless `--fail-on-warn`) |
| `1` | BLOCK — the gate ran and said stop |
| `2` | ERROR — the gate could not produce a verdict |

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

## Configuring `--investigate`

An agent loop that reads a finding and queries DataHub's MCP tools for context an on-call engineer would want — what SQL reads the column, whether the change is documented, what else sits on the same path. Needs `--mcp` and one LLM provider. First one found wins, checked in this order:

| Provider | Set | Notes |
|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY` | Default; what this project is most tested against |
| Groq | `GROQ_API_KEY` | OpenAI-compatible; genuinely free tier, no card required |
| OpenRouter | `OPENROUTER_API_KEY` | OpenAI-compatible; routes to many models |
| Anything else OpenAI-compatible | `LLM_API_KEY` + `LLM_BASE_URL` + `LLM_MODEL` | Together, Fireworks, a local vLLM server, etc. |

Groq and OpenRouter both speak the wire format everything except Anthropic has converged on, so both go through one adapter rather than being separate integrations — anything else that speaks it works too, through the generic `LLM_API_KEY` path. Default models per provider are overridable (`GROQ_MODEL`, `OPENROUTER_MODEL`), since model lineups on these platforms change often.

No provider configured: `--investigate` prints why and continues without it. The verdict is identical either way — see [Why you can trust the verdict](#why-you-can-trust-the-verdict).

## DataHub Skill

`skills/undertow-deploy-gate/` is a DataHub Skill — it gives an agent judgment about *when* to reach for the gate, not just tools to call. The part worth reading is what it forbids:

> You do not decide whether the deploy proceeds. Undertow does.
> If you find yourself looking for a flag that makes the red box go away, stop and tell the user what the finding actually is.

It will not edit `undertow.yaml` to downgrade a BLOCK, re-baseline to silently clear a finding, or report exit code 2 as a pass. `tests/test_skill.py` asserts every command the skill references actually exists.

## Exercising the bundled fixture

The commands above work against your own DataHub. If you'd rather see the gate react to a real change before connecting it to your own catalog, there's a fixture you can seed, break, and reset in place — `datahub docker quickstart` gets you a local DataHub to point it at.

```
pip install -e ".[dev]"
datahub docker quickstart
make seed
make baseline
make break            # drops transaction_amount from transactions.raw
make blast-radius     # gates every downstream model
make check-write      # gate + write the verdict back to DataHub
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
| `undertow check --all` | Gate every model in `undertow.yaml` — also correlates shared root causes into one incident |
| `make blast-radius` | Gate every downstream model |
| `make check-write` | Gate + write verdict to DataHub |
| `make impact` | Check a proposed SQL change pre-merge |
| `undertow what-if <dataset_urn> <column>` | Ask what a column removal would break, before there's a PR to point `impact` at |
| `make history` | Every recorded verdict, from DataHub — with policy suggestions if a warning keeps recurring |
| `make check-mcp` | Resolve lineage via MCP instead of SDK |
| `make check-investigate` | Add the agent investigation loop |
| `make reset` | Restore the graph |

**Incident correlation.** `check --all` gates every model independently, exactly as before — but when two or more share a root cause, a consolidated incident prints after the individual boxes: one broken table, every model it reaches, one owner to page. Twenty models blocked by the same upstream change now read as one incident, not twenty.

**`what-if`.** Point it at a column you're considering removing — no SQL file, no PR needed. Same downstream walk `impact` runs against a parsed statement, run directly against a hypothetical change instead, so you can go have the "does anyone depend on this" conversation before writing the migration. Purely informational, always exits 0.

**Policy suggestions.** `undertow history` now reads its own recorded runs for a pattern: a finding kind that's warned on most of a model's recent checks is either a real ongoing problem or a threshold that no longer fits the asset. Either way it surfaces as a suggestion to review `undertow.yaml` — advisory only, the same way the investigator is. Needs at least 4 recorded runs before it says anything; a handful of dismissed warnings is a trend worth naming, one or two is noise.

## DataHub integration details

- **Reads** via the Agent Context Kit's MCP server (`--mcp`): `get_entities`, `get_lineage`, `list_schema_fields`, checked against the server's live `tools/list` at connect time so a signature drift fails loudly. SDK fallback (`SdkLineageSource`) resolves the same graph for environments without the MCP server.
- **Writes** via the REST emitter, not MCP — the OSS `mcp-server-datahub` build ships with mutation tools disabled, so reads and writes take different paths on purpose.
- Verified against `mcp-server-datahub` 0.6.0 on DataHub OSS v1.7.0.
- Write-back: `assertionInfo`/`assertionRunEvent` on root-cause datasets, `undertow:blocked`/`undertow:cleared` tags on `mlModel`, structured properties (`undertow_risk_verdict`, `undertow_last_checked`, `undertow_baseline`), and institutional-memory links back to the PR and run.

Every `--write-back` appends to a native DataHub assertion (a timeseries aspect), so runs accumulate rather than overwrite — visible in DataHub's own assertion timeline, not a store Undertow invented:

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

**Composing with other agents:** Undertow writes verdicts into the catalog, not into a store of its own — so the next agent to read that catalog inherits them without integrating with Undertow at all. DataHub's Analytics Agent reads documentation and context before writing SQL; after an Undertow run, a model carries a tag, a structured property, and a native assertion with run history, and any agent asking about that model gets an answer shaped by all of it.

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
