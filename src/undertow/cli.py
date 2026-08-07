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
from contextlib import contextmanager
from pathlib import Path

import click
from datahub.emitter.rest_emitter import DatahubRestEmitter
from rich.console import Console
from rich.table import Table

from undertow import __version__
from undertow.attributor import attribute_findings
from undertow.differ import diff_snapshots
from undertow.engine import PolicyViolation, evaluate, validate_policy
from undertow.investigator import investigate_findings
from undertow.models import FindingKind, Severity, UndertowSnapshot
from undertow.narrator import narrate
from undertow.policy import Policy
from undertow.reporter import (
    MLModelPatchBuilder,
    format_console,
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

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Diagnostics go to stderr so stdout stays clean for piped output.
console = Console()
err_console = Console(stderr=True)

EXIT_OK = 0
EXIT_CLEAR = 0
EXIT_BLOCK = 1
EXIT_ERROR = 2

DEFAULT_POLICY_PATH = "undertow.yaml"


@contextmanager
def _lineage_source(use_mcp: bool) -> Iterator[object]:
    """Yield a connected lineage source, tearing down the MCP subprocess after.

    The MCP path owns a child process and an initialised session, so it has a
    lifetime the SDK path does not. Both are handed out through one context
    manager rather than making every command remember the difference.
    """
    if not use_mcp:
        yield SdkLineageSource(
            gms_url=os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080"),
            token=os.environ.get("DATAHUB_GMS_TOKEN"),
        )
        return

    try:
        executor = McpToolExecutor()
        executor.start()
    except McpError as exc:
        err_console.print(
            f"[red]Could not start the DataHub MCP server:[/red] {exc}\n"
            "Install it with `pip install mcp-server-datahub`, and confirm "
            "DATAHUB_GMS_URL / DATAHUB_GMS_TOKEN are set."
        )
        raise SystemExit(EXIT_ERROR) from exc

    try:
        yield McpLineageSource(executor)
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

    from_graph = getattr(footprint, "baseline_snapshot", None)
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
        console.print(f"  Stored in DataHub structuredProperty 'undertow_baseline' and {local_path}")
        raise SystemExit(EXIT_OK)
    except SystemExit:
        raise
    except Exception as exc:
        err_console.print(f"[red]Failed to capture baseline:[/red] {exc}")
        raise SystemExit(EXIT_ERROR) from exc


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
            "(get_dataset_queries, search_documents, …) exist only on the DataHub MCP "
            "server. Continuing without investigation."
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
                findings = investigate_findings(findings, source)
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

    # 6. Narrator
    narrative = narrate(verdict)

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

    # 8. Reporter
    render_console(verdict, written_to_datahub=wrote_to_datahub)

    # GitHub PR summary if in GitHub Actions
    if os.environ.get("GITHUB_ACTIONS") or os.environ.get("GITHUB_STEP_SUMMARY"):
        gh_comment = format_github_comment(verdict, narrative=narrative)
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(gh_comment + "\n")

    # The verdict decides its own exit status; this is a hand-off, not a branch.
    raise SystemExit(verdict.exit_code(fail_on_warn=fail_on_warn))


if __name__ == "__main__":
    sys.exit(main())
