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
from typing import Any

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


class _ModelUnreadable(RuntimeError):
    """One model could not be resolved, during a sweep that should continue."""


def _assert_not_blind(
    source: object, footprint: object, model_urn: str, *, fatal: bool = True
) -> None:
    """Refuse to return a verdict the resolver was not equipped to reach.

    A gate has two failure modes and they are not symmetric. A false BLOCK gets
    the gate deleted; a false CLEAR gets a broken model shipped. Every path that
    leaves Undertow unable to see the graph — unreachable GMS, bad token, a model
    URN that resolved to nothing — must therefore exit as an error, never as a
    verdict. Silence is not evidence of safety.

    `fatal=False` keeps that guarantee while sweeping an inventory: the caller
    still records an error for this model, but the remaining models get checked
    rather than being hidden behind the first unreachable one.
    """

    def stop(message: str) -> None:
        err_console.print(message)
        if fatal:
            raise SystemExit(EXIT_ERROR)
        raise _ModelUnreadable(model_urn)

    connection_error = getattr(source, "connection_error", None)
    if connection_error:
        stop(
            f"[red]Cannot reach DataHub:[/red] {connection_error}\n"
            "Undertow fails closed — no verdict is issued when the graph is unreachable. "
            "Check DATAHUB_GMS_URL and DATAHUB_GMS_TOKEN."
        )

    assets = getattr(getattr(footprint, "snapshot", None), "assets", {}) or {}
    if len(assets) <= 1:
        stop(
            f"[red]Resolved nothing upstream of[/red] {model_urn}\n"
            "The model has no reachable mlFeature or dataset lineage, so there is "
            "nothing to diff. This is reported as an error rather than CLEAR: an "
            "empty footprint and a clean footprint are indistinguishable, and only "
            "one of them is safe to ship."
        )


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


def _packaged_recording() -> Path | None:
    """The recording that ships inside the installed package.

    Resolved as package data rather than a path relative to the working
    directory. `undertow demo` is the first thing anyone runs, and they will run
    it from wherever they happen to be — a relative default works only from the
    repository root, which is the one place a `pip install`ed tool cannot assume
    it is standing.
    """
    try:
        from importlib import resources

        path = Path(str(resources.files("undertow") / "data" / "recorded-graph.json"))
        return path if path.exists() else None
    except Exception:
        return None


@main.command()
@click.option(
    "--recording",
    default=None,
    help="Recorded graph to replay. Defaults to the one shipped in the package.",
)
@click.option(
    "--config",
    "config_path",
    default=DEFAULT_POLICY_PATH,
    show_default=True,
    help="Path to undertow.yaml.",
)
def demo(recording: str | None, config_path: str) -> None:
    """Run the whole gate against a recorded graph. No DataHub required.

    Everything above the resolver is the real code on real catalog data: the
    differs, attribution, policy engine and reporter are the same ones a live
    run uses. Only the source of the graph differs, and that is stated on every
    run rather than glossed over.
    """
    from undertow.resolver import RecordedLineageSource

    pol = _load_policy(config_path)

    source_path = Path(recording) if recording else _packaged_recording()
    if source_path is None or not source_path.exists():
        err_console.print(
            f"[red]No recording found[/red] at {recording or 'undertow/data/recorded-graph.json'}. "
            "Reinstall the package, or regenerate it with "
            "`python scripts/record_fixture.py` against a live DataHub."
        )
        raise SystemExit(EXIT_ERROR)

    try:
        with open(source_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        err_console.print(f"[red]Could not read {source_path}:[/red] {exc}")
        raise SystemExit(EXIT_ERROR) from exc

    models = data.get("models", {})
    fraud, churn = models.get("fraud"), models.get("churn")
    if not fraud or not churn:
        err_console.print(f"[red]{recording} does not name the demo models.[/red]")
        raise SystemExit(EXIT_ERROR)

    console.print()
    console.print("[bold]Undertow — offline demo[/bold]")
    console.print(
        f"  Replaying a graph recorded from DataHub OSS {data.get('gms_version', '?')}. "
        "No DataHub is being contacted.",
        style="dim",
    )
    console.print(
        "  The differs, attribution, policy engine and reporter below are the "
        "same code a live run uses.",
        style="dim",
    )
    console.print()

    def verdict_for(model_urn: str, state: str) -> tuple[Any, Any]:
        """Diff `state` against the recorded pre-change graph."""
        baseline = resolve_footprint(
            model_urn, RecordedLineageSource(data, "before"), max_hops=pol.max_hops
        ).snapshot
        current_source = RecordedLineageSource(data, state)
        footprint = resolve_footprint(model_urn, current_source, max_hops=pol.max_hops)
        findings = attribute_findings(
            diff_snapshots(baseline, footprint.snapshot, pol), footprint
        )
        return (
            evaluate(
                findings,
                pol,
                model_urn=model_urn,
                assets_checked=footprint.assets_checked,
                baseline_ref="recorded:before",
            ),
            profile_coverage(baseline, footprint.snapshot),
        )

    console.print("[bold]1. Before the change[/bold] — the graph as approved")
    console.print()
    clear_verdict, coverage = verdict_for(fraud, "before")
    render_console(clear_verdict, coverage=coverage)
    console.print(f"  exit code {clear_verdict.exit_code()}", style="dim")
    console.print()

    console.print(
        "[bold]2. A data engineer drops [red]transaction_amount[/red] "
        "from transactions.raw[/bold]"
    )
    console.print(
        "  Three hops above either model. Nothing model-local can see it.", style="dim"
    )
    console.print()

    exit_codes = []
    for step, (label, model_urn) in enumerate((("fraud team", fraud), ("churn team", churn)), 3):
        verdict, coverage = verdict_for(model_urn, "after")
        console.print(f"[bold]{step}. Gating {short_urn(model_urn)}[/bold]  ({label})")
        console.print()
        render_console(verdict, coverage=coverage)
        console.print(f"  exit code {verdict.exit_code()}", style="dim")
        console.print()
        exit_codes.append(verdict.exit_code())

    console.print("[bold]One column. Two models. Two teams.[/bold]")
    console.print(
        "  Neither team knew the other was downstream of the same table; the graph did.",
        style="dim",
    )
    console.print()
    console.print(
        f"  In CI both of those runs exit {exit_codes[0]}, and the deploys stop.",
        style="dim",
    )
    console.print(
        "  To run this against a real DataHub: `make seed && make baseline && make break`.",
        style="dim",
    )

    # Exit 0: the demo ran. The verdicts report their own exit codes above, and
    # conflating "the demo worked" with "the gate said stop" would be its own
    # small version of the confusion this project exists to remove.
    raise SystemExit(EXIT_OK)


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
@click.option("--model", "model_urn", default=None, help="mlModel URN to check.")
@click.option(
    "--all",
    "check_all",
    is_flag=True,
    default=False,
    help="Check every model listed under `models:` in undertow.yaml.",
)
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
    model_urn: str | None,
    check_all: bool,
    config_path: str,
    use_mcp: bool,
    write_back: bool,
    baseline_path: str | None,
    use_investigator: bool,
    fail_on_warn: bool,
) -> None:
    """Gate a model deploy on upstream lineage risk.

    `--model` gates one, which is what CI does when a pipeline already knows
    which model it is deploying. `--all` gates every model listed under
    `models:` in undertow.yaml, for the scheduled run over a team's whole
    inventory.
    """
    pol = _load_policy(config_path)

    if check_all and model_urn:
        err_console.print("[red]Use --model or --all, not both.[/red]")
        raise SystemExit(EXIT_ERROR)

    if check_all:
        targets = list(pol.models)
        if not targets:
            err_console.print(
                "[red]--all needs a model inventory.[/red] Add the models this team "
                f"gates under `models:` in {config_path}:\n\n"
                "  models:\n"
                '    - "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detector_v3,PROD)"\n'
            )
            raise SystemExit(EXIT_ERROR)
    elif model_urn:
        targets = [model_urn]
    else:
        err_console.print("[red]Give --model <urn>, or --all to use the inventory.[/red]")
        raise SystemExit(EXIT_ERROR)

    if len(targets) > 1:
        # One source for the whole sweep would be faster, but each model gets its
        # own so a failure on one cannot leave a half-torn-down MCP subprocess
        # poisoning the rest of the run.
        worst = EXIT_OK
        for index, target in enumerate(targets):
            if index:
                console.print()
            console.print(f"[bold]{short_urn(target)}[/bold]", style="dim")
            code = _gate_one(
                target,
                pol,
                use_mcp=use_mcp,
                write_back=write_back,
                baseline_path=baseline_path,
                use_investigator=use_investigator,
                fail_on_warn=fail_on_warn,
                fatal=False,
            )
            # Worst wins, and 2 outranks 1: a model the gate could not see is a
            # worse outcome than one it saw and blocked, because only the first
            # is indistinguishable from a pass.
            worst = 2 if 2 in (worst, code) else max(worst, code)

        console.print()
        console.print(f"  {len(targets)} model(s) checked, exit {worst}", style="dim")
        raise SystemExit(worst)

    raise SystemExit(
        _gate_one(
            targets[0],
            pol,
            use_mcp=use_mcp,
            write_back=write_back,
            baseline_path=baseline_path,
            use_investigator=use_investigator,
            fail_on_warn=fail_on_warn,
            fatal=True,
        )
    )


def _gate_one(
    model_urn: str,
    pol: Policy,
    *,
    use_mcp: bool,
    write_back: bool,
    baseline_path: str | None,
    use_investigator: bool,
    fail_on_warn: bool,
    fatal: bool,
) -> int:
    """Gate one model and return its exit code.

    `fatal` decides what an error does. Checking a single model, an error should
    stop the process — that is the CI contract. Sweeping an inventory, one
    unreachable model must not hide the verdicts of the other eleven, so the
    error is reported, folded into the worst code, and the sweep continues.
    """

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
            _assert_not_blind(source, footprint, model_urn, fatal=fatal)
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
    except _ModelUnreadable:
        return EXIT_ERROR
    except SystemExit:
        raise
    except Exception as exc:
        err_console.print(f"[red]Resolver error:[/red] {exc}")
        if fatal:
            raise SystemExit(EXIT_ERROR) from exc
        return EXIT_ERROR

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
    return verdict.exit_code(fail_on_warn=fail_on_warn)


if __name__ == "__main__":
    sys.exit(main())
