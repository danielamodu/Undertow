"""Console Rich tree output reporter matching Undertow product design."""

from __future__ import annotations

import io

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.tree import Tree

from undertow.differ import ProfileCoverage
from undertow.models import Finding, Severity, Verdict, short_urn


def _append_investigation(content: Text, finding: Finding) -> None:
    """Render what `--investigate` learned, directly under the finding it's about.

    Without this, the investigation loop ran, spent real tool calls and tokens,
    and the report looked identical to one where it never ran at all — the
    enrichment existed only in `evidence`, a dict the console never opened. A
    reader has no way to tell "the agent added nothing here" from "the agent
    never ran," which is the one distinction that matters for trusting it.
    """
    error = finding.evidence.get("investigation_error")
    if error:
        content.append(f"  Investigation failed: {error}\n", style="dim red")
        return

    summary = finding.evidence.get("investigation")
    if not summary:
        return

    calls = finding.evidence.get("investigation_tool_calls")
    call_note = f" ({calls} tool call{'s' if calls != 1 else ''})" if calls else ""
    content.append(f"  Investigation{call_note}:\n", style="dim cyan")
    for line in str(summary).splitlines() or [""]:
        content.append(f"    {line}\n", style="dim")


def render_console(
    verdict: Verdict,
    *,
    console: Console | None = None,
    written_to_datahub: bool = False,
    coverage: ProfileCoverage | None = None,
    truncation: str | None = None,
) -> None:
    """Render structured Rich output for a Verdict to the console.

    `coverage` states how much of the footprint the statistical differ could
    actually inspect. It matters most on the verdicts that look like good news:
    "no drift found" across assets that were never profiled is a much weaker
    claim than the same words across assets that were, and a report that renders
    them identically is quietly misleading.

    `truncation` is the same idea one layer down: how much of the graph the
    resolver reached at all. Passed as a formatted line rather than a footprint
    so the reporter keeps knowing nothing about resolver types.
    """
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
            f"{emoji} CLEAR — {model_name} "
            f"({verdict.assets_checked} assets checked, no material change)",
            style="bold green",
        )
    else:
        style_color = "bold red" if verdict.severity is Severity.BLOCK else "bold yellow"
        header_text = Text(
            f"{emoji} {verdict.severity.value} — {model_name} "
            f"({n_block} blocking, {n_warn} warning)",
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

                # Render tree to string for panel. The buffer is held by name
                # rather than read back off `Console.file`, which is typed as a
                # plain `IO[str]` and so has no `getvalue()` to reach for.
                buffer = io.StringIO()
                tree_console = Console(file=buffer, force_terminal=False)
                tree_console.print(tree)
                blocking_content.append(buffer.getvalue() + "\n")
            else:
                subj = finding.subject_column or finding.subject_urn
                blocking_content.append(f"  Root: {subj}\n\n")

            kind_label = finding.kind.value.lower().replace("_", " ")
            blocking_content.append(
                f"  Confidence: {finding.confidence.value} ({kind_label})\n",
                style="dim",
            )
            _append_investigation(blocking_content, finding)
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

            kind_label = finding.kind.value.lower().replace("_", " ")
            warning_content.append(
                f"  Confidence: {finding.confidence.value} ({kind_label})\n",
                style="dim",
            )
            _append_investigation(warning_content, finding)
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

    if coverage is not None:
        # Dim, and last: it qualifies the verdict rather than competing with it.
        # Blind coverage is promoted to yellow — a CLEAR that inspected nothing
        # is the one case where the qualifier matters more than the verdict.
        style = "yellow" if coverage.is_blind else "dim"
        con.print(f"  statistics: {coverage.summary()}", style=style)

    if truncation is not None:
        # Never dim. Unlike coverage, which qualifies how thoroughly a reached
        # asset was examined, this says part of the graph was not reached — so
        # the finding that would have mattered may simply not be in the report.
        con.print(f"  coverage: {truncation}", style="yellow")

    if written_to_datahub:
        con.print(f"✍ Written to DataHub → {model_name}")


def format_console(
    verdict: Verdict,
    *,
    written_to_datahub: bool = False,
    coverage: ProfileCoverage | None = None,
    truncation: str | None = None,
) -> str:
    """Format Verdict console Rich output as a string."""
    buf = io.StringIO()
    con = Console(file=buf, force_terminal=False, width=80)
    render_console(
        verdict,
        console=con,
        written_to_datahub=written_to_datahub,
        coverage=coverage,
        truncation=truncation,
    )
    return buf.getvalue()
