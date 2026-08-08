# Demo video — shot list

Target 2:40, hard ceiling 3:00. Public on YouTube.

Record against a **live DataHub** for the main run — the offline demo exists for
people who won't stand one up, not for the video, and "no DataHub required" is a
weaker claim than showing the real thing working.

Two terminals side by side, or one terminal and a browser. Font large enough to
read at 720p — bump it two sizes past comfortable.

## Before you record

Windows does not ship `make`. Use `.\demo.ps1 <target>` — same target names, no
install. On macOS or Linux the `make <target>` equivalents work identically.

Reset to a clean, approved state:

```powershell
.\demo.ps1 reset
.\demo.ps1 baseline
```

Have a browser tab open on `http://localhost:9002` at `fraud_detector_v3`.

Short commands read better on camera than a 70-character URN, which is the only
reason this script exists.

---

## 0:00–0:18 — The problem, stated once

**On screen:** the lineage chain, either the README diagram or DataHub's own lineage view.

> "A data engineer drops a column from a Snowflake table. Three hops downstream, a
> fraud model is still serving predictions — and nothing errors. It just gets
> quietly worse. The engineer who broke it has no idea the model exists."

Don't explain the architecture yet. Land the problem.

## 0:18–0:30 — Credit DataHub, then say the delta

**On screen:** DataHub's lineage tab for `transactions.raw`.

> "DataHub already knows what's downstream of this table — that's its impact
> analysis, and it's why this is built on DataHub rather than beside it. What it
> doesn't do is compare today's graph against the last approved deploy and stop
> the release. That's Undertow."

This is 12 seconds and it removes the single biggest credibility risk. Do not cut it.

## 0:30–1:05 — The break, and the blast radius

```powershell
.\demo.ps1 break
.\demo.ps1 blast-radius
```

**On screen:** both red BLOCK boxes. Let them sit. This is the money shot.

> "One column. Two models. `fraud_detector_v3` belongs to Alex. `churn_predictor_v1`
> belongs to Priya, on a different team, reaching the same table through the same
> staging layer. Neither team knew the other was there. The graph did."

Point at `@data_eng_tom` on screen.

> "And it names the engineer whose change caused it — not the person deploying."

## 1:05–1:25 — Certain versus probable

```powershell
.\demo.ps1 reset
.\demo.ps1 baseline
.\demo.ps1 break-stats
.\demo.ps1 check
```

**On screen:** the yellow WARN, exit 0.

> "A dropped column is a fact — certain, and it blocks. A distribution moving 4.8
> sigma is an inference — probable, so it warns and gets out of the way. A gate
> that stops deploys on a statistical hunch gets switched off within a month."

Point at the last line.

> "And it reports how much it could actually inspect. No drift found across columns
> that were never profiled is a much weaker claim, and it says so."

## 1:25–1:50 — The agent

```powershell
.\demo.ps1 check-investigate
```

**On screen:** the investigation context attached to the finding.

> "An agent loop reads each finding and goes looking — what SQL actually reads this
> column, was the change documented, what else is on this path. It runs through
> DataHub's MCP server, the Agent Context Kit's own surface."

Beat.

> "What it cannot do is change the verdict. Not because it's told not to — because
> it consumes and produces a Finding, and a Finding has no severity field. There's a
> test asserting the verdict is byte-identical with and without the agent running."

That constraint is the most interesting thing in the project. Say it slowly.

## 1:50–2:10 — It writes back

```powershell
.\demo.ps1 check-write
```

**On screen:** switch to the browser, refresh the model in DataHub. Show the
`undertow:blocked` tag and the native assertion.

> "The verdict goes back into the catalog as a native DataHub assertion, tags, and
> structured properties. Undertow has no database. The next run, on any machine,
> starts from what the last one learned —"

```bash
.\demo.ps1 history
```

> "— including the whole history, because assertion run events accumulate."

## 2:10–2:30 — Before the merge

```bash
undertow impact examples/pr-drops-amount.sql
```

> "And it doesn't have to wait for a deploy. Point it at the SQL a pull request
> changes, and it compares the columns that statement would produce against the
> columns the table has today. Same finding, but on the PR that removes the
> column, addressed to the person removing it."

## 2:30–2:50 — Close

> "Undertow is built on DataHub's MCP server, ships a DataHub Skill, and writes
> back in native aspects. The patch builder it needed didn't exist upstream, so
> that's an open PR on datahub-project/datahub."

Optional, if you have room and want the engineering line:

> "While building it, running against a real instance showed the statistical
> differ had never actually executed — profiles are a timeseries aspect, and it
> was looking in the wrong place. Two hundred tests hadn't caught it. That's
> fixed, with a guard, and it's why every claim in the README is one I've watched
> run."

> "Try it yourself in sixty seconds with no Docker: `pip install -e .` then
> `undertow demo`."

---

## Cut order, if you run long

1. The engineering line at 2:40
2. `.\demo.ps1 history` at 2:05
3. `undertow impact` at 2:10

**Never cut:** the DataHub credit at 0:18, the blast radius at 0:30, or the
"cannot change the verdict" line at 1:40.

## After recording

- Watch it once at 1x with sound off. If the red box isn't legible, refilm.
- Upload **public**, not unlisted — the rules ask for public visibility.
- Put the URL in the Devpost submission *and* at the top of the README.
