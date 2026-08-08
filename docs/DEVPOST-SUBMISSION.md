# Devpost submission — ready to paste

Fill the video URL in two places (marked `<<VIDEO_URL>>`), then paste each block
into the matching Devpost field. Nothing here needs rewriting.

---

## Project name

```
Undertow
```

## Tagline / elevator pitch

```
An agent that stops a production ML model from deploying on broken data — and names the engineer whose change broke it.
```

## Challenge

**Production ML Agents.** (It also satisfies *Agents That Do Real Work*: it reads
DataHub, acts, and writes results back so the next run inherits the knowledge.)

## Built with

```
python, datahub, mcp, model-context-protocol, anthropic, claude, sqlglot, github-actions, snowflake, mlops, data-lineage
```

## Links

- **Repository:** https://github.com/danielamodu/Undertow
- **Video:** `<<VIDEO_URL>>`
- **Upstream contribution:** https://github.com/datahub-project/datahub/pull/18979

---

## Description

Paste this into the main "About the project" field. Devpost renders Markdown.

```markdown
## Try it in 60 seconds

```bash
git clone https://github.com/danielamodu/Undertow.git && cd Undertow
pip install -e ".[dev]"
undertow demo
```

No Docker, no DataHub, no API key. It replays a graph recorded from a live
DataHub OSS v1.7.0 — the differs, attribution, policy engine and reporter are
the same code a live run uses.

## The problem

A data engineer drops a column from a Snowflake table. Three hops downstream a
fraud model keeps serving predictions, and nothing errors — it just gets quietly
worse. The engineer has no idea the model exists. The ML team finds out when
accuracy falls off a cliff.

## What Undertow does

It walks DataHub's graph from an `mlModel` back through its features and staging
layers to the raw tables, diffs what it finds against the last approved deploy,
and blocks the release in CI when a breaking change reaches the model. Then it
writes the verdict back into the catalog as a native assertion, so the next run
on any machine starts from what the last one learned.

**One column, two teams.** In the demo, dropping `transaction_amount` from
`transactions.raw` blocks `fraud_detector_v3` (owned by @ml_eng_alex) *and*
`churn_predictor_v1` (owned by @ml_eng_priya, on a different team, reaching the
same table through the same staging layer). Neither team knew the other was
downstream. The graph did.

## The agent, and the constraint that makes it safe

An investigation loop reads each finding and goes looking through DataHub's MCP
server — what SQL actually reads this column, was the change documented, what
else sits on this path. What it learns is attached to the report.

**It cannot change the verdict**, and that's enforced by the type system rather
than a prompt: the loop consumes and produces a `Finding`, and a `Finding` has no
severity field. Severity is assigned by a deterministic policy engine the agent
never touches. There's a test asserting the verdict is byte-identical with and
without the agent running.

An agent free to argue its way to a green light isn't a gate. A gate with no
agent leaves an engineer holding a diff and no context. This is both.

## What it knows versus what it infers

A dropped column is a fact — `CERTAIN`, and it blocks. A distribution moving 4.8σ
is an inference — `PROBABLE`, so it warns and gets out of the way. The policy
engine refuses to let a `PROBABLE` finding block unless a team explicitly opts
in, because a gate that stops deploys on statistical hunches gets switched off
within a month.

Every report ends with what it could actually inspect:

```
statistics: Compared statistics on 3/14 columns across 3/6 assets.
```

"No drift found" across assets that were never profiled is a much weaker claim
than the same words across assets that were, and it says which.

## Before the merge, not after the deploy

`undertow impact` runs on the pull request that changes the SQL. It parses the
proposed statement, compares the columns it *would* produce against the columns
that table has in DataHub today, and walks downstream from anything that
disappears — then comments on the PR removing the column, addressed to the
person removing it.

## How it uses DataHub

- **MCP Server / Agent Context Kit** — a real MCP client spawns the server over
  stdio, validates tool names against the server's own `tools/list` at connect
  time, and keeps one session alive per run. A Python SDK path resolves the same
  graph and produces the same verdict.
- **DataHub Skill** — `skills/undertow-deploy-gate/`, written to the
  datahub-skills format, encoding the workflow around the gate and the rule that
  the agent explains verdicts rather than negotiating them.
- **Native write-back** — `assertionInfo` + `assertionRunEvent` (timeseries, so
  verdict history accumulates), `globalTags`, `structuredProperties`,
  `institutionalMemory`. Undertow has no database of its own.
- **DataHub's own SQL parser** — the fixture's lineage isn't hand-written. The
  staging table is defined by one SQL file, and `sqlglot_lineage` derives both
  its schema and its column-level lineage from that statement.

## Challenges we ran into

Running it against a live instance found bugs that 200 passing tests had not.

The statistical differ had **never executed**. `datasetProfile` is a *timeseries*
aspect, so it never appears in `get_entity_semityped`, so the branch reading it
was dead against any real DataHub. Every asset resolved unprofiled, every
comparison was skipped, and the verdict came back clean because nothing had been
examined. The tests missed it because they hand the resolver synthetic aspects
with the profile inlined — the one shape a live GMS never produces.

That pattern repeated. The two lineage paths silently disagreed. The investigator
offered the model a tool this server build doesn't have. `getattr(some_dict,
"values")` returned `dict.values` — a bound method, and truthy. Nine in total,
all fixed, each with a guard for its whole class rather than the instance.

## Accomplishments

- 409 tests, zero skips, strict mypy, CI on two Python versions
- Verified from a clean clone into a fresh venv, and from a built wheel
- An upstream PR to datahub-project/datahub adding the `MLModelPatchBuilder`
  that `datahub/specific/` was missing, with 13 tests

## What's next

Continuous watch mode over DataHub's Kafka events, and Slack delivery to the
owner named in the finding.
```

---

## Checklist before you hit submit

- [ ] Video uploaded to YouTube, set to **public** (not unlisted), under 3:00
- [ ] Video URL pasted into Devpost **and** into the README
- [ ] Repository is public and the Apache 2.0 licence shows in the About panel
- [ ] `<<VIDEO_URL>>` replaced in both places above
- [ ] **Opt into the Most Valuable Feedback Survey** — $50 × 10 winners, ~15 minutes
- [ ] Submit. Do not leave this for deadline day.
