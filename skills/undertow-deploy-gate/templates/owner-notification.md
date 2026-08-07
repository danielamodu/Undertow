# Owner notification template

Drafted for the owner of the **root-cause asset** — the person whose change
caused the block. That is usually not the person deploying the model, and often
someone on another team who has never heard of it.

Surface the draft. Do not send it.

---

## Blocking finding

> **Subject:** `{{ root_asset }}.{{ column }}` — blocks `{{ model_name }}` deploy
>
> Hi {{ owner_handle }},
>
> A change to `{{ root_asset }}` is blocking a production model deploy.
>
> **What changed:** {{ finding_summary }}
> **Detected:** {{ checked_at }} · confidence `{{ confidence }}`
>
> **How it reaches the model:**
>
> ```
> {{ attribution_path }}
> ```
>
> `{{ model_name }}` is owned by {{ model_owner }} and serves
> {{ deployment_name }}. Its deploy is currently gated.
>
> If this change was intentional, the model's baseline needs re-approving and
> {{ model_owner }} should know the feature is affected. If it wasn't, this is
> the fastest signal you'll get.
>
> — Undertow ({{ run_url }})

## Blast radius, more than one model affected

Use when the same change blocks models owned by different teams. The point of
the message is that the owner cannot see this from where they sit.

> **Subject:** `{{ root_asset }}.{{ column }}` — blocks {{ n }} models across {{ n_teams }} teams
>
> Hi {{ owner_handle }},
>
> A change to `{{ root_asset }}` reaches {{ n }} production models:
>
> | Model | Owner | Feature affected |
> | --- | --- | --- |
> | {{ model_a }} | {{ owner_a }} | {{ feature_a }} |
> | {{ model_b }} | {{ owner_b }} | {{ feature_b }} |
>
> **What changed:** {{ finding_summary }}
>
> These sit behind {{ shared_asset }}, so the dependency isn't visible from
> `{{ root_asset }}` without walking the graph. Both deploys are gated.
>
> — Undertow ({{ run_url }})

## Statistical finding — WARN, not BLOCK

Note the different register. This one is not an accusation.

> **Subject:** Possible drift in `{{ root_asset }}.{{ column }}`
>
> Hi {{ owner_handle }},
>
> Undertow flagged a distribution change upstream of `{{ model_name }}`:
>
> **{{ finding_summary }}** — confidence `PROBABLE`.
>
> This did **not** block the deploy. Statistical findings are evidence something
> may have changed, not proof that anything is wrong, so the gate reports them
> and gets out of the way.
>
> Worth a look if you weren't expecting it.
>
> — Undertow ({{ run_url }})

---

## Filling these in

Every field comes from the verdict — nothing here should be guessed:

| Placeholder | Source |
| --- | --- |
| `root_asset`, `column` | The first hop of the attribution path |
| `owner_handle` | `owners` on the root-cause asset |
| `finding_summary` | The finding's summary line, verbatim |
| `confidence` | `CERTAIN` or `PROBABLE` — never omit it |
| `attribution_path` | The rendered hop tree, copied as-is |
| `model_owner` | `owners` on the `mlModel` |
| `deployment_name` | `deployments` on the model, if present |

If a field has no value in the graph, say so rather than inventing one.
"Unassigned" is a finding in itself — an upstream asset nobody owns is a
different problem, and `OWNERSHIP_LOST` exists for exactly that.
