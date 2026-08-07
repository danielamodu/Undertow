"""Render a live verdict to an SVG for the README.

An SVG rather than a screenshot: it is generated from a real run against a real
DataHub, so it cannot drift into showing output the code no longer produces, and
it stays legible at any zoom without a binary blob in the repository.

Usage (with DataHub running and the graph broken via `make break`):

    python scripts/render_verdict_svg.py docs/verdict-block.svg
"""

from __future__ import annotations

import os
import re
import sys

from rich.console import Console
from rich.terminal_theme import MONOKAI

from undertow.attributor import attribute_findings
from undertow.differ import diff_snapshots, profile_coverage
from undertow.engine import evaluate
from undertow.policy import Policy
from undertow.reporter import render_console
from undertow.resolver import SdkLineageSource, resolve_footprint

MODEL_URN = os.environ.get(
    "UNDERTOW_SVG_MODEL",
    "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detector_v3,PROD)",
)


def main() -> None:
    out_path = sys.argv[1] if len(sys.argv) > 1 else "docs/verdict-block.svg"

    policy = Policy.load("undertow.yaml")
    source = SdkLineageSource(
        gms_url=os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080"),
        token=os.environ.get("DATAHUB_GMS_TOKEN"),
    )

    footprint = resolve_footprint(MODEL_URN, source, max_hops=policy.max_hops)
    if getattr(source, "connection_error", None):
        raise SystemExit(f"cannot reach DataHub: {source.connection_error}")
    if len(footprint.snapshot.assets) <= 1:
        raise SystemExit(f"resolved nothing upstream of {MODEL_URN}")

    baseline = footprint.baseline_snapshot
    if baseline is None:
        raise SystemExit("no baseline in DataHub — run `make baseline` first")

    findings = attribute_findings(
        diff_snapshots(baseline, footprint.snapshot, policy), footprint
    )
    verdict = evaluate(
        findings,
        policy,
        model_urn=MODEL_URN,
        assets_checked=footprint.assets_checked,
        baseline_ref=baseline.baseline_ref or "none",
    )

    # `record=True` captures the styled output; the width is fixed so the image
    # is reproducible rather than a function of whoever's terminal ran it.
    console = Console(record=True, width=88, force_terminal=True)
    render_console(
        verdict, console=console, coverage=profile_coverage(baseline, footprint.snapshot)
    )

    console.save_svg(
        out_path, title=f"undertow check  ·  exit {verdict.exit_code()}", theme=MONOKAI
    )
    make_self_contained(out_path)
    print(f"wrote {out_path}  ({verdict.severity.value}, exit {verdict.exit_code()})")


def make_self_contained(path: str) -> None:
    """Drop the webfont, so the image fetches nothing when rendered.

    Rich points `@font-face` at cdnjs. GitHub serves README images through a
    proxy that will not load it, so the font never arrives either way — but an
    image that reaches out to a third party when someone opens a README is worth
    removing on its own merits. The glyphs are box-drawing and ASCII; any
    monospace face renders them.
    """
    with open(path, encoding="utf-8") as handle:
        svg = handle.read()

    svg = re.sub(r"\s*@font-face\s*\{[^}]*\}", "", svg)
    svg = svg.replace("font-family: Fira Code, monospace;", "font-family: monospace;")

    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(svg)


if __name__ == "__main__":
    main()
