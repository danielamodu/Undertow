"""Console Rich tree output reporter matching Undertow product design."""

from __future__ import annotations

import io
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.tree import Tree

from undertow.models import Severity, Verdict, short_urn


def render_console(
    verdict: Verdict,
    *,
    console: Console | None = None,
    written_to_datahub: bool = False,
) -> None:
    """Render structured Rich output for a Verdict to the console."""
    con = console or Console()

    # Severity emoji & header with encoding fallback
    try:
        "🔴".encode(con.encoding or "utf-8")
        emoji = (
            "🔴"
            if verdict.severity is Severity.BLOCK
            else ("🟡" if verdict.severity is Severity.WARN else "🟢")
        )
    except Exception:
        emoji = "●"
    n_block = len(verdict.blocking)
    n_warn = len(verdict.warnings)

    model_name = short_urn(verdict.model_urn)

    if verdict.severity is Severity.CLEAR:
        header_text = Text(
            f"{emoji} CLEAR — {model_name} ({verdict.assets_checked} assets checked, no material change)",
            style="bold green",
        )
    else:
        style_color = "bold red" if verdict.severity is Severity.BLOCK else "bold yellow"
        header_text = Text(
            f"{emoji} {verdict.severity.value} — {model_name} ({n_block} blocking, {n_warn} warning)",
            style=style_color,
        )

    con.print(header_text)
    con.print()

    # Panel for BLOCKING findings
    if verdict.blocking:
        blocking_content = Text()
        for i, rf in enumerate(verdict.blocking):
            finding = rf.finding
            feat = finding.affected_feature_urn
            feat_short = short_urn(feat) if feat else "unattributed"

            headline = f"Feature `{feat_short}` — {finding.summary}\n\n"
            blocking_content.append(headline, style="bold red")

            if finding.path and finding.path.hops:
                hops = finding.path.hops
                root_label = hops[0].short_label()
                owners_str = (
                    f" (@{', @'.join(short_urn(o) for o in finding.path.owners)})"
                    if finding.path.owners
                    else ""
                )

                # Build tree view
                tree = Tree(f"[bold]{root_label}[/bold]{owners_str}")
                current_node = tree
                for hop in hops[1:]:
                    via_info = f" [{hop.via}]" if hop.via else ""
                    current_node = current_node.add(f"{hop.short_label()}{via_info}")
                
                # Render tree to string for panel
                tree_console = Console(file=io.StringIO(), force_terminal=False)
                tree_console.print(tree)
                tree_str = tree_console.file.getvalue()
                blocking_content.append(tree_str + "\n")
            else:
                subj = finding.subject_column or finding.subject_urn
                blocking_content.append(f"  Root: {subj}\n\n")

            blocking_content.append(
                f"  Confidence: {finding.confidence.value} ({finding.kind.value.lower().replace('_', ' ')})\n",
                style="dim",
            )
            if i < len(verdict.blocking) - 1:
                blocking_content.append("\n" + "─" * 40 + "\n\n")

        con.print(
            Panel(
                blocking_content,
                title="[bold red]BLOCKING[/bold red]",
                border_style="red",
                expand=False,
            )
        )
        con.print()

    # Panel for WARNING findings
    if verdict.warnings:
        warning_content = Text()
        for i, rf in enumerate(verdict.warnings):
            finding = rf.finding
            feat = finding.affected_feature_urn
            feat_short = short_urn(feat) if feat else "unattributed"

            headline = f"Feature `{feat_short}` — {finding.summary}\n"
            warning_content.append(headline, style="bold yellow")

            if finding.path and finding.path.hops:
                origin = finding.path.root.short_label()
                depth = finding.path.depth
                warning_content.append(f"  origin: {origin} ({depth} hops)\n", style="dim")
            else:
                warning_content.append(
                    f"  subject: {short_urn(finding.subject_urn)}\n", style="dim"
                )

            warning_content.append(
                f"  Confidence: {finding.confidence.value} ({finding.kind.value.lower().replace('_', ' ')})\n",
                style="dim",
            )
            if i < len(verdict.warnings) - 1:
                warning_content.append("\n")

        con.print(
            Panel(
                warning_content,
                title="[bold yellow]WARNING[/bold yellow]",
                border_style="yellow",
                expand=False,
            )
        )
        con.print()

    if written_to_datahub:
        con.print(f"✍ Written to DataHub → {model_name}")


def format_console(verdict: Verdict, *, written_to_datahub: bool = False) -> str:
    """Format Verdict console Rich output as a string."""
    buf = io.StringIO()
    con = Console(file=buf, force_terminal=False, width=80)
    render_console(verdict, console=con, written_to_datahub=written_to_datahub)
    return buf.getvalue()
