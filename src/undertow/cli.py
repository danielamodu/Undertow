"""Command-line entrypoint for Undertow.

Exit codes:
    0  CLEAR (EXIT_OK) — proceed with deploy
    1  WARN (EXIT_WARN) — proceed with caution
    2  BLOCK (EXIT_BLOCK / EXIT_ERROR) — stop the deploy or error
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click
from datahub.emitter.rest_emitter import DatahubRestEmitter
from rich.console import Console
from rich.table import Table

from undertow import __version__
from undertow.attributor import attribute_findings
from undertow.differ import diff_snapshots
from undertow.engine import PolicyViolation, evaluate, validate_policy
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
from undertow.resolver import McpLineageSource, SdkLineageSource, resolve_footprint

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
EXIT_WARN = 1
EXIT_BLOCK = 2
EXIT_ERROR = 2

DEFAULT_POLICY_PATH = "undertow.yaml"


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
        source = McpLineageSource() if use_mcp else SdkLineageSource()
        footprint = resolve_footprint(model_urn, source)
        console.print(
            f"[green]Footprint resolved:[/green] {len(footprint.snapshot.assets)} assets, "
            f"{len(footprint.paths)} attribution paths."
        )
        for urn, asset in footprint.snapshot.assets.items():
            console.print(f"  • {urn} ({asset.entity_type}, {len(asset.columns)} cols)")
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
        source = McpLineageSource() if use_mcp else SdkLineageSource()
        footprint = resolve_footprint(model_urn, source, max_hops=pol.max_hops)
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
def check(
    model_urn: str,
    config_path: str,
    use_mcp: bool,
    write_back: bool,
    baseline_path: str | None,
) -> None:
    """Gate a model deploy on upstream lineage risk."""
    pol = _load_policy(config_path)

    # 1. Resolve lineage footprint
    try:
        source = McpLineageSource() if use_mcp else SdkLineageSource()
        footprint = resolve_footprint(model_urn, source, max_hops=pol.max_hops)
        current_snapshot = footprint.snapshot
    except Exception as exc:
        err_console.print(f"[red]Resolver error:[/red] {exc}")
        raise SystemExit(EXIT_ERROR) from exc

    # 2. Load baseline snapshot from file, resolved graph, or local snapshot file
    baseline_snapshot: UndertowSnapshot | None = None

    if baseline_path and Path(baseline_path).exists():
        try:
            with open(baseline_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                baseline_snapshot = UndertowSnapshot.model_validate(data)
        except Exception as exc:
            err_console.print(f"[yellow]Could not load baseline from {baseline_path}:[/yellow] {exc}")
    elif footprint.baseline_snapshot is not None:
        baseline_snapshot = footprint.baseline_snapshot
    else:
        # Check default local snapshot location
        model_id = model_urn.split(",")[-2] if "," in model_urn else "default_model"
        default_local = Path(".undertow/snapshots") / f"{model_id}.json"
        if default_local.exists():
            try:
                with open(default_local, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    baseline_snapshot = UndertowSnapshot.model_validate(data)
            except Exception:
                pass

    # 3. Differ
    raw_findings = diff_snapshots(baseline_snapshot, current_snapshot, pol)

    # 4. Attributor
    attributed_findings = attribute_findings(raw_findings, footprint)

    # 5. Policy Engine
    baseline_ref = baseline_snapshot.baseline_ref if baseline_snapshot else "none"
    verdict = evaluate(
        attributed_findings,
        pol,
        model_urn=model_urn,
        assets_checked=footprint.assets_checked,
        baseline_ref=baseline_ref,
    )

    # 6. Narrator
    narrative = narrate(verdict)

    # 7. Reporter
    render_console(verdict, written_to_datahub=write_back)

    # GitHub PR summary if in GitHub Actions
    if os.environ.get("GITHUB_ACTIONS") or os.environ.get("GITHUB_STEP_SUMMARY"):
        gh_comment = format_github_comment(verdict, narrative=narrative)
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(gh_comment + "\n")

    # Write-back to DataHub
    if write_back:
        try:
            write_verdict_to_datahub(verdict)
        except Exception as exc:
            err_console.print(f"[yellow]DataHub write-back failed:[/yellow] {exc}")

    # Exit codes: 0=CLEAR, 1=WARN, 2=BLOCK
    if verdict.severity is Severity.CLEAR:
        raise SystemExit(EXIT_CLEAR)
    elif verdict.severity is Severity.WARN:
        raise SystemExit(EXIT_WARN)
    else:
        raise SystemExit(EXIT_BLOCK)


if __name__ == "__main__":
    sys.exit(main())
