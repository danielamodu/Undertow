"""Command-line entrypoint for Undertow.

Exit codes:
    0  proceed — CLEAR, or WARN without --fail-on-warn
    1  blocked — the gate ran and said stop
    2  error   — the gate could not produce a verdict

`Verdict.exit_code()` owns the 0/1 decision; this module never re-derives it.
Code 2 is the CLI's alone, because "no verdict" is not a verdict.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import click
from datahub.emitter.rest_emitter import DatahubRestEmitter
from rich.console import Console
from rich.table import Table

from undertow import __version__
from undertow.attributor import attribute_findings
from undertow.differ import diff_snapshots, profile_coverage
from undertow.engine import PolicyViolation, evaluate, validate_policy
from undertow.investigator import investigate_findings, investigation_unavailable_reason
from undertow.models import FindingKind, UndertowSnapshot, short_urn
from undertow.narrator import generate_narrative_detailed
from undertow.policy import Policy
from undertow.reporter import (
    MLModelPatchBuilder,
    format_github_comment,
    render_console,
    write_verdict_to_datahub,
)
from undertow.resolver import (
    McpError,
    McpLineageSource,
    McpToolExecutor,
    SdkLineageSource,
    resolve_footprint,
)
from undertow.resolver.base import LineageSource
from undertow.resolver.profiles import TimeseriesProfileReader

# The verdict box is drawn with box-drawing characters and the report carries
# owner handles, so a console that cannot encode UTF-8 turns a BLOCK into a
# UnicodeEncodeError — a gate that crashes while trying to say "stop". Windows
# CI is the usual culprit. Best-effort: if the stream cannot be reconfigured,
# `errors="replace"` was never available and there is nothing else to try.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        with suppress(Exception):
            _stream.reconfigure(encoding="utf-8", errors="replace")

# Diagnostics go to stderr so stdout stays clean for piped output.
console = Console()
err_console = Console(stderr=True)

EXIT_OK = 0
EXIT_CLEAR = 0
EXIT_BLOCK = 1
EXIT_ERROR = 2

DEFAULT_POLICY_PATH = "undertow.yaml"


@contextmanager
def _lineage_source(use_mcp: bool) -> Iterator[LineageSource]:
    """Yield a connected lineage source, tearing down the MCP subprocess after.

    The MCP path owns a child process and an initialised session, so it has a
    lifetime the SDK path does not. Both are handed out through one context
    manager rather than making every command remember the difference.
    """
    gms_url = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
    token = os.environ.get("DATAHUB_GMS_TOKEN")

    if not use_mcp:
        yield SdkLineageSource(gms_url=gms_url, token=token)
        return

    # The server resolves its own connection through `DataHubClient.from_env()`,
    # which reads the environment and then falls back to `~/.datahubenv`. A CI
    # runner has neither, so without handing the resolved URL down explicitly the
    # subprocess dies during startup and the only symptom is a 20s timeout. Both
    # paths now agree on where DataHub is, including the localhost default.
    mcp_env = {"DATAHUB_GMS_URL": gms_url}
    if token:
        mcp_env["DATAHUB_GMS_TOKEN"] = token

    try:
        executor = McpToolExecutor(env=mcp_env)
        executor.start()
    except McpError as exc:
        err_console.print(
            f"[red]Could not start the DataHub MCP server:[/red] {exc}\n"
            "Install it with `pip install mcp-server-datahub`, and confirm "
            "DATAHUB_GMS_URL / DATAHUB_GMS_TOKEN are set."
        )
        raise SystemExit(EXIT_ERROR) from exc

    try:
        # Statistics are not on the MCP tool surface, so they come from the
        # timeseries API. Without this the MCP path resolves every asset
        # unprofiled and disagrees with the SDK path on the same graph.
        yield McpLineageSource(
            executor,
            profile_reader=TimeseriesProfileReader(gms_url=gms_url, token=token),
        )
    finally:
        executor.close()


def _load_baseline(
    footprint: object, baseline_path: str | None, model_urn: str
) -> UndertowSnapshot | None:
    """Find the last approved snapshot, in order of specificity.

    An explicit `--baseline` wins; then the copy DataHub holds in the model's
    structured properties (which is what lets a fresh CI runner with no local
    cache still have something to diff against); then the local file.
    """
    if baseline_path and Path(baseline_path).exists():
        try:
            with open(baseline_path, encoding="utf-8") as f:
                return UndertowSnapshot.model_validate(json.load(f))
        except Exception as exc:
            err_console.print(
                f"[yellow]Could not load baseline from {baseline_path}:[/yellow] {exc}"
            )

    from_graph: UndertowSnapshot | None = getattr(footprint, "baseline_snapshot", None)
    if from_graph is not None:
        return from_graph

    model_id = model_urn.split(",")[-2] if "," in model_urn else "default_model"
    local = Path(".undertow/snapshots") / f"{model_id}.json"
    if local.exists():
        try:
            with open(local, encoding="utf-8") as f:
                return UndertowSnapshot.model_validate(json.load(f))
        except Exception:
            pass

    return None


def _assert_not_blind(source: object, footprint: object, model_urn: str) -> None:
    """Refuse to return a verdict the resolver was not equipped to reach.

    A gate has two failure modes and they are not symmetric. A false BLOCK gets
    the gate deleted; a false CLEAR gets a broken model shipped. Every path that
    leaves Undertow unable to see the graph — unreachable GMS, bad token, a model
    URN that resolved to nothing — must therefore exit as an error, never as a
    verdict. Silence is not evidence of safety.
    """
    connection_error = getattr(source, "connection_error", None)
    if connection_error:
        err_console.print(
            f"[red]Cannot reach DataHub:[/red] {connection_error}\n"
            "Undertow fails closed — no verdict is issued when the graph is unreachable. "
            "Check DATAHUB_GMS_URL and DATAHUB_GMS_TOKEN."
        )
        raise SystemExit(EXIT_ERROR)

    assets = getattr(getattr(footprint, "snapshot", None), "assets", {}) or {}
    if len(assets) <= 1:
        err_console.print(
            f"[red]Resolved nothing upstream of[/red] {model_urn}\n"
            "The model has no reachable mlFeature or dataset lineage, so there is "
            "nothing to diff. This is reported as an error rather than CLEAR: an "
            "empty footprint and a clean footprint are indistinguishable, and only "
            "one of them is safe to ship."
        )
        raise SystemExit(EXIT_ERROR)


def _load_policy(path: str | None) -> Policy:
    """Load and validate, converting failure modes into exit code 2."""
    try:
        policy = Policy.load(path)
        validate_policy(policy)
    except PolicyViolation as exc:
        err_console.print(f"[red]Invalid policy:[/red] {exc}")
        raise SystemExit(EXIT_ERROR) from exc
    except Exception as exc:
        err_console.print(f"[red]Could not load policy from {path!r}:[/red] {exc}")
        raise SystemExit(EXIT_ERROR) from exc
    return policy


@click.group()
@click.version_option(__version__, prog_name="undertow")
def main() -> None:
    """Lineage-grounded pre-deploy gate for production ML, built on DataHub."""


@main.group()
def policy() -> None:
    """Inspect and validate the risk policy."""


@policy.command("validate")
@click.option(
    "--config",
    "config_path",
    default=DEFAULT_POLICY_PATH,
    show_default=True,
    help="Path to undertow.yaml. Missing file means built-in defaults.",
)
def policy_validate(config_path: str) -> None:
    """Check a policy file for internal inconsistencies."""
    exists = Path(config_path).exists()
    loaded = _load_policy(config_path)

    source = config_path if exists else "built-in defaults (no file found)"
    console.print(f"[green]OK[/green] — policy valid: {source}")
    console.print(
        f"  {len(loaded.rules)} rule override(s), "
        f"{len(loaded.exemptions)} exemption(s), max_hops={loaded.max_hops}"
    )
    if loaded.allow_probable_block:
        console.print(
            "  [yellow]allow_probable_block is ON[/yellow] — statistical findings "
            "can stop a deploy. This is off by default for a reason."
        )
    raise SystemExit(EXIT_OK)


@policy.command("show")
@click.option(
    "--config",
    "config_path",
    default=DEFAULT_POLICY_PATH,
    show_default=True,
    help="Path to undertow.yaml. Missing file means built-in defaults.",
)
def policy_show(config_path: str) -> None:
    """Print the effective severity for every finding kind."""
    loaded = _load_policy(config_path)

    table = Table(title="Effective policy", header_style="bold")
    table.add_column("Finding kind")
    table.add_column("Severity")
    table.add_column("Confidence")
    table.add_column("Source")

    for kind in FindingKind:
        rule = loaded.rule_for(kind)
        overridden = kind.value in loaded.rules
        colour = {"BLOCK": "red", "WARN": "yellow", "CLEAR": "green"}[rule.severity.value]
        table.add_row(
            kind.value,
            f"[{colour}]{rule.severity.value}[/{colour}]",
            kind.confidence.value,
            "override" if overridden else "default",
        )

    console.print(table)
    raise SystemExit(EXIT_OK)


@main.command()
@click.option("--model", "model_urn", required=True, help="mlModel URN to resolve.")
@click.option(
    "--mcp/--no-mcp",
    "use_mcp",
    default=False,
    help="Use DataHub MCP server instead of Python SDK.",
)
def resolve(model_urn: str, use_mcp: bool) -> None:
    """Walk the graph from a model to its upstream data footprint."""
    try:
        with _lineage_source(use_mcp) as source:
            footprint = resolve_footprint(model_urn, source)
            console.print(
                f"[green]Footprint resolved:[/green] {len(footprint.snapshot.assets)} assets, "
                f"{len(footprint.paths)} attribution paths."
            )
            for urn, asset in footprint.snapshot.assets.items():
                console.print(f"  • {urn} ({asset.entity_type}, {len(asset.columns)} cols)")
    except SystemExit:
        raise
    except Exception as exc:
        err_console.print(f"[red]Error resolving footprint:[/red] {exc}")
        raise SystemExit(EXIT_ERROR) from exc


@main.command()
@click.option("--model", "model_urn", required=True, help="mlModel URN to baseline.")
@click.option(
    "--config",
    "config_path",
    default=DEFAULT_POLICY_PATH,
    show_default=True,
    help="Path to undertow.yaml.",
)
@click.option(
    "--mcp/--no-mcp",
    "use_mcp",
    default=False,
    help="Use DataHub MCP server instead of Python SDK.",
)
def baseline(model_urn: str, config_path: str, use_mcp: bool) -> None:
    """Capture current state as baseline and write to DataHub structured properties."""
    pol = _load_policy(config_path)
    try:
        with _lineage_source(use_mcp) as source:
            footprint = resolve_footprint(model_urn, source, max_hops=pol.max_hops)
            _assert_not_blind(source, footprint, model_urn)
        snapshot = footprint.snapshot
        snapshot_json = snapshot.model_dump_json()

        # Emit structuredProperty "undertow_baseline" to DataHub
        patch_builder = MLModelPatchBuilder(model_urn)
        patch_builder.set_structured_property("undertow_baseline", snapshot_json)

        gms_url = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
        token = os.environ.get("DATAHUB_GMS_TOKEN")
        emitter = DatahubRestEmitter(gms_server=gms_url, token=token)

        for proposal in patch_builder.build():
            emitter.emit_mcp(proposal)

        # Save locally
        os.makedirs(".undertow/snapshots", exist_ok=True)
        model_id = model_urn.split(",")[-2] if "," in model_urn else "default_model"
        local_path = Path(".undertow/snapshots") / f"{model_id}.json"
        with open(local_path, "w", encoding="utf-8") as f:
            f.write(snapshot_json)

        console.print(f"[green]Baseline captured and stored for {model_urn}[/green]")
        console.print(
            f"  Stored in DataHub structuredProperty 'undertow_baseline' and {local_path}"
        )
        raise SystemExit(EXIT_OK)
    except SystemExit:
        raise
    except Exception as exc:
        err_console.print(f"[red]Failed to capture baseline:[/red] {exc}")
        raise SystemExit(EXIT_ERROR) from exc


@main.command()
@click.argument("sql_files", nargs=-1, required=True, type=click.Path(exists=True))
@click.option(
    "--platform", default="snowflake", show_default=True, help="Data platform for URN resolution."
)
@click.option("--env", default="PROD", show_default=True, help="DataHub environment.")
@click.option(
    "--fail-on-impact",
    is_flag=True,
    default=False,
    help="Exit 1 when a removed column reaches a model. Off by default: a PR check informs.",
)
@click.option("--max-hops", default=6, show_default=True, help="How far downstream to walk.")
def impact(
    sql_files: tuple[str, ...], platform: str, env: str, fail_on_impact: bool, max_hops: int
) -> None:
    """Check changed SQL against the catalog, before it merges.

    Parses each statement, compares the columns it would produce against the
    columns the table has in DataHub today, and walks downstream from anything
    that disappears. Run it on the SQL a pull request touches.
    """
    from datahub.ingestion.graph.client import DataHubGraph, DataHubGraphConfig
    from datahub.sql_parsing.schema_resolver import SchemaResolver

    from undertow.impact import analyse_sql, format_pr_comment
    from undertow.reporter.github import PROJECT_URL

    gms_url = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
    token = os.environ.get("DATAHUB_GMS_TOKEN")

    try:
        graph = DataHubGraph(
            DataHubGraphConfig(server=gms_url, token=token, timeout_sec=30)
        )
        # Backed by the live graph, so the parse binds against the schemas the
        # catalog actually holds rather than a second description of them.
        resolver = SchemaResolver(platform=platform, env=env, graph=graph)
    except Exception as exc:
        err_console.print(f"[red]Cannot reach DataHub:[/red] {exc}")
        raise SystemExit(EXIT_ERROR) from exc

    source = SdkLineageSource(gms_url=gms_url, token=token)
    if getattr(source, "connection_error", None):
        err_console.print(f"[red]Cannot reach DataHub:[/red] {source.connection_error}")
        raise SystemExit(EXIT_ERROR)

    impacts = []
    for path in sql_files:
        try:
            result = analyse_sql(
                path, source=source, schema_resolver=resolver, max_hops=max_hops
            )
        except Exception as exc:
            err_console.print(f"[red]Failed to analyse {path}:[/red] {exc}")
            raise SystemExit(EXIT_ERROR) from exc
        if result is not None:
            impacts.append(result)

    if not impacts:
        console.print("[green]No table-building statements in the given files.[/green]")
        raise SystemExit(EXIT_OK)

    breaking = [i for i in impacts if i.is_breaking]

    for item in impacts:
        if item.parse_error:
            err_console.print(f"[yellow]{item.sql_file}:[/yellow] {item.parse_error}")
            continue
        if not item.dropped_columns:
            console.print(f"[green]{item.table_name}[/green] — no columns removed")
            continue

        dropped = ", ".join(item.dropped_columns)
        colour = "red" if item.impacted else "yellow"
        console.print(f"[{colour}]{item.table_name}[/{colour}] — removes {dropped}")
        for model in item.impacted:
            owners = ", ".join(f"@{short_urn(o)}" for o in model.owners) or "unassigned"
            console.print(f"    reaches {model.name} ({owners})")
            console.print(f"      via {model.route()}", style="dim")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(format_pr_comment(impacts, project_url=PROJECT_URL) + "\n")

    # Informational by default. This check knows a column is going away; it does
    # not know whether that is intended, and a PR check that blocks on every
    # deliberate column removal gets switched off within a week.
    raise SystemExit(EXIT_BLOCK if (breaking and fail_on_impact) else EXIT_OK)


@main.command()
@click.option("--model", "model_urn", required=True, help="mlModel URN to inspect.")
@click.option("--limit", default=20, show_default=True, help="Most recent runs to show.")
def history(model_urn: str, limit: int) -> None:
    """Show recorded verdicts for a model, newest first.

    Read from the native DataHub assertion Undertow writes to. Undertow keeps no
    database of its own — this is the catalog's own record, so a wiped CI runner
    still knows what the last deploys looked like.
    """
    from datahub.ingestion.graph.client import DataHubGraph, DataHubGraphConfig

    from undertow.history import assertion_urn_for, read_history, summarise

    gms_url = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
    try:
        graph = DataHubGraph(
            DataHubGraphConfig(
                server=gms_url, token=os.environ.get("DATAHUB_GMS_TOKEN"), timeout_sec=15
            )
        )
        runs = read_history(model_urn, graph=graph, limit=limit)
    except Exception as exc:
        err_console.print(f"[red]Could not read verdict history:[/red] {exc}")
        raise SystemExit(EXIT_ERROR) from exc

    table = Table(title=f"Verdict history — {model_urn.split(',')[-2]}", header_style="bold")
    table.add_column("Checked at")
    table.add_column("Verdict")
    table.add_column("Blocking", justify="right")
    table.add_column("Warning", justify="right")
    table.add_column("Assets", justify="right")
    table.add_column("Exit", justify="right")

    for run in runs:
        colour = {"BLOCK": "red", "WARN": "yellow", "CLEAR": "green"}.get(run.severity, "dim")
        table.add_row(
            run.checked_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
            f"[{colour}]{run.severity or run.result}[/{colour}]",
            str(run.blocking),
            str(run.warning),
            str(run.assets_checked),
            str(run.exit_code()),
        )

    if runs:
        console.print(table)
    console.print(f"  {summarise(runs)}", style="dim")
    console.print(f"  assertion: {assertion_urn_for(model_urn)}", style="dim")
    raise SystemExit(EXIT_OK)


@main.command()
@click.option("--model", "model_urn", required=True, help="mlModel URN to check.")
@click.option(
    "--config",
    "config_path",
    default=DEFAULT_POLICY_PATH,
    show_default=True,
    help="Path to undertow.yaml.",
)
@click.option(
    "--mcp/--no-mcp",
    "use_mcp",
    default=False,
    help="Use DataHub MCP server instead of Python SDK.",
)
@click.option(
    "--write-back/--no-write-back",
    "write_back",
    default=False,
    help="Write verdict back to DataHub.",
)
@click.option(
    "--baseline",
    "baseline_path",
    default=None,
    help="Path to baseline snapshot JSON file.",
)
@click.option(
    "--investigate/--no-investigate",
    "use_investigator",
    default=False,
    help=(
        "Run the agent investigation loop over findings for extra context. "
        "Requires --mcp and ANTHROPIC_API_KEY. Never changes the verdict."
    ),
)
@click.option(
    "--fail-on-warn",
    "fail_on_warn",
    is_flag=True,
    default=False,
    help="Exit 1 on WARN as well as BLOCK. Off by default: a warning annotates a deploy.",
)
def check(
    model_urn: str,
    config_path: str,
    use_mcp: bool,
    write_back: bool,
    baseline_path: str | None,
    use_investigator: bool,
    fail_on_warn: bool,
) -> None:
    """Gate a model deploy on upstream lineage risk."""
    pol = _load_policy(config_path)

    if use_investigator and not use_mcp:
        err_console.print(
            "[yellow]--investigate requires --mcp[/yellow] — the investigation tools "
            "(get_dataset_queries, search, get_lineage) exist only on the DataHub MCP "
            "server. Continuing without investigation."
        )
        use_investigator = False

    # Say it before the work starts, not after. An investigation that cannot run
    # produces output identical to a run without the flag, and a reader with no
    # message to go on will conclude the agent does nothing rather than that it
    # was never reachable.
    if use_investigator:
        reason = investigation_unavailable_reason()
        if reason:
            err_console.print(
                f"[yellow]--investigate is not available:[/yellow] {reason}\n"
                "Continuing without investigation — the verdict is unaffected either "
                "way, since the agent only ever adds context to it."
            )
            use_investigator = False

    # Steps 1–4 all need the graph, so they share one source lifetime: closing it
    # after resolution would tear down the MCP subprocess before the investigator
    # could ask a single question. Everything after this block is pure.
    try:
        with _lineage_source(use_mcp) as source:
            # 1. Resolve
            footprint = resolve_footprint(model_urn, source, max_hops=pol.max_hops)
            _assert_not_blind(source, footprint, model_urn)
            current_snapshot = footprint.snapshot

            # 2. Baseline
            baseline_snapshot = _load_baseline(footprint, baseline_path, model_urn)

            # 3. Diff, then attribute
            raw_findings = diff_snapshots(baseline_snapshot, current_snapshot, pol)
            findings = attribute_findings(raw_findings, footprint)

            # 4. Investigate. Enrichment only — see undertow/investigator.
            if use_investigator and findings:
                findings = investigate_findings(
                    findings,
                    source,
                    on_skip=lambda why: err_console.print(
                        f"[yellow]Investigation skipped:[/yellow] {why}"
                    ),
                )
    except SystemExit:
        raise
    except Exception as exc:
        err_console.print(f"[red]Resolver error:[/red] {exc}")
        raise SystemExit(EXIT_ERROR) from exc

    # 5. Policy Engine
    baseline_ref = baseline_snapshot.baseline_ref if baseline_snapshot else "none"
    verdict = evaluate(
        findings,
        pol,
        model_urn=model_urn,
        assets_checked=footprint.assets_checked,
        baseline_ref=baseline_ref,
    )

    # 6. Narrator. The fallback renders the same findings the reporter already
    #    prints, so it is only worth a "Narrative Summary" heading when an LLM
    #    actually wrote something the structured report does not already say.
    narrative, narrative_is_llm = generate_narrative_detailed(verdict)

    # 7. Write-back happens *before* rendering so the report can state what
    #    actually landed. Printing "Written to DataHub" from the flag rather than
    #    the result is how a demo claims a write it never made.
    wrote_to_datahub = False
    if write_back:
        try:
            write_verdict_to_datahub(
                verdict,
                gms_server=os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080"),
                token=os.environ.get("DATAHUB_GMS_TOKEN"),
            )
            wrote_to_datahub = True
        except Exception as exc:
            err_console.print(f"[yellow]DataHub write-back failed:[/yellow] {exc}")

    # 8. Reporter. Coverage rides along so the report can qualify a quiet
    #    verdict: silence from the statistical differ means "nothing found"
    #    only where there was something to look at.
    coverage = (
        profile_coverage(baseline_snapshot, current_snapshot) if baseline_snapshot else None
    )
    render_console(verdict, written_to_datahub=wrote_to_datahub, coverage=coverage)

    # GitHub PR summary if in GitHub Actions
    if os.environ.get("GITHUB_ACTIONS") or os.environ.get("GITHUB_STEP_SUMMARY"):
        gh_comment = format_github_comment(
            verdict, narrative=narrative if narrative_is_llm else None
        )
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(gh_comment + "\n")

    # The verdict decides its own exit status; this is a hand-off, not a branch.
    raise SystemExit(verdict.exit_code(fail_on_warn=fail_on_warn))


if __name__ == "__main__":
    sys.exit(main())
