# Undertow Deploy Gate

Decide whether an ML model is safe to deploy, and explain the answer to the
person who caused it.

## What it does

1. Resolves the model URN from a name
2. Makes sure a baseline exists to diff against
3. Runs the gate through DataHub's MCP server
4. Reads the exit code — `0` proceed, `1` blocked, `2` blind
5. On BLOCK: names the column, the path, and the owner, then checks the blast
   radius across every other model on that path

## Capabilities

- **Pre-deploy gate** — is this model safe to ship right now?
- **Root cause** — which upstream column changed, and who owns it
- **Blast radius** — every model a schema change reaches, not just the one being deployed
- **Baseline approval** — accept an intentional change deliberately, with an owner
- **Owner notification** — draft the message to the engineer whose change caused it

## Usage

```
/undertow-deploy-gate is fraud_detector_v3 safe to deploy?
/undertow-deploy-gate why did the gate block my deploy?
/undertow-deploy-gate what breaks if I drop transaction_amount from transactions.raw?
/undertow-deploy-gate the column drop was intentional, approve the new baseline
```

## What it will not do

The verdict is computed by a deterministic policy engine, not by the agent. This
skill explains verdicts; it does not negotiate them. It will not edit
`undertow.yaml` to downgrade a BLOCK, re-baseline to make a finding disappear
without explicit confirmation, or report exit code `2` — the gate could not see
the graph — as a pass.

That constraint is the product, not a limitation of the skill. See
[the architecture notes](../../README.md#key-architectural-guarantees).

## Requirements

- `undertow` installed — `pip install -e ".[dev]"` from the repository root
- A reachable DataHub instance (`DATAHUB_GMS_URL`, `DATAHUB_GMS_TOKEN`), or
  `/datahub-setup` already run
- `ANTHROPIC_API_KEY` only if you want `--investigate`

## Files

| Path | Contents |
| --- | --- |
| `SKILL.md` | The workflow |
| `references/verdicts.md` | Every finding kind, its confidence, and its severity |
| `templates/owner-notification.md` | Message templates for the root-cause owner |
