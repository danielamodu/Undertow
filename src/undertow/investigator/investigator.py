"""The investigation loop — an agent that gathers context the graph won't hand you.

The differs answer *what changed*. The policy engine answers *how bad is it*.
Neither can answer the questions an engineer actually asks next: is this drop
already documented? What SQL actually reads this column? Which other models sit
on this path? Those need a loop — read a finding, decide which tool would help,
call it, read the result, decide again.

## The constraint that makes this safe

**The investigator cannot change a verdict.** That is enforced by the type
system rather than by discipline: this module consumes and produces `Finding`,
and a `Finding` has no severity field. Severity is assigned by
`undertow.engine.evaluate` from `Finding.kind`, which the investigator never
writes. The worst a compromised or hallucinating agent can do is attach
misleading prose to `evidence` — it physically cannot argue a BLOCK down to a
WARN, and `test_investigator.py` pins that.

That split is the whole reason this is bolted onto a deterministic gate rather
than replacing one. The agent investigates; the rules decide.

## Failure policy

Unlike the resolver, this layer **fails soft**. An investigation that errors,
times out, or exhausts its budget returns the findings unchanged. Enrichment is
commentary on a verdict that has already been computed from facts, so losing it
costs context, never correctness — the opposite trade-off from the resolver,
where silence would be read as safety.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

from undertow.models import Finding

# Read-only tools, verified present on mcp-server-datahub 0.6.0's OSS build.
# Mutation tools are gated off in OSS, so there is nothing here that could write
# to the catalog even if the model asked — but the allowlist is explicit anyway,
# because "the server wouldn't let it" is a weaker guarantee than "we never
# offered it".
INVESTIGATION_TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_dataset_queries",
        "description": (
            "Get SQL queries that read a dataset or a specific column. Call this to find "
            "out what actually consumes a column — lineage edges record declared "
            "dependencies, but a query log records real usage. A dropped column that no "
            "query reads is a different risk than one read by a nightly job."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "urn": {"type": "string", "description": "Dataset URN."},
                "column": {"type": "string", "description": "Optional column name to narrow to."},
            },
            "required": ["urn"],
        },
    },
    {
        "name": "search",
        "description": (
            "Full-text search across DataHub entities. Call this to find other assets, "
            "models, or features related to a change — for example, other models that "
            "reference the same upstream table."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_documents",
        "description": (
            "Search documentation stored in DataHub. Call this to check whether a change "
            "was announced or explained anywhere — a documented deprecation is a "
            "materially different situation from a silent drop."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_lineage",
        "description": (
            "Get upstream or downstream lineage for an entity. Call this to widen the "
            "blast radius — for example, to find every model downstream of a changed "
            "table, not just the one being gated."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "urn": {"type": "string", "description": "Entity URN."},
                "upstream": {
                    "type": "boolean",
                    "description": "True for upstream, false for downstream.",
                },
            },
            "required": ["urn", "upstream"],
        },
    },
]

_SYSTEM = """You are Undertow's investigator. A deterministic gate has already \
detected a change upstream of a production ML model and assigned it a severity. \
Your job is NOT to judge the change — that decision is made and you cannot alter it.

Your job is to gather the context an on-call engineer would want in the next \
sixty seconds, using the DataHub tools available to you. Useful questions:

- What SQL actually reads the affected column? (get_dataset_queries)
- Is this change documented or announced anywhere? (search_documents)
- Which OTHER models or assets sit on this path? (get_lineage downstream, search)

Rules:
- Ground every claim in a tool result. If a tool returns nothing, say so plainly.
- Never speculate about whether the deploy should proceed. That is not your call.
- Be brief. Three sentences of specific, sourced context beats a paragraph of hedging.
- If the tools reveal nothing useful, say "No additional context found." and stop.

End with a short plain-text summary. No preamble, no markdown headers."""


class InvestigationUnavailable(RuntimeError):
    """No API key, no SDK, or no MCP source — investigation is skipped, not failed."""


def unavailable_reason() -> str | None:
    """Why investigation cannot run, or `None` if it can.

    Exists so a caller can say *why* nothing happened before doing the work.
    Skipping quietly is the failure mode that matters here: `--investigate` with
    no key produces output byte-identical to a run without the flag, and the
    only available reading of that is that the agent does nothing.
    """
    try:
        _default_client()
    except InvestigationUnavailable as exc:
        return str(exc)
    return None


def tools_for(source: Any) -> list[dict[str, Any]]:
    """The subset of `INVESTIGATION_TOOLS` the connected server actually offers.

    Not every documented tool exists on every build. `search_documents` is in
    mcp-server-datahub's published surface and absent from the OSS v1.7.0
    handshake — offering it anyway costs a turn on a tool error that teaches the
    model nothing, and a bounded loop has few turns to spare.

    When a source cannot say what it has, every tool is offered and the dispatch
    layer reports failures individually. That is the older, worse behaviour, but
    it is the only correct one without a handshake to consult.
    """
    available = getattr(source, "available_tools", None)
    if not available:
        return list(INVESTIGATION_TOOLS)
    return [tool for tool in INVESTIGATION_TOOLS if tool["name"] in available]


def investigate_findings(
    findings: list[Finding],
    source: Any,
    *,
    client: Any = None,
    model: str = "claude-opus-5",
    max_turns: int = 6,
    max_findings: int = 3,
    on_skip: Callable[[str], None] | None = None,
) -> list[Finding]:
    """Enrich findings with investigated context. Never changes severity.

    `max_findings` bounds cost: a footprint that produces twenty findings does
    not warrant twenty agent loops in a CI gate. The most severe are already
    sorted first by the time a reporter sees them, but this runs before ruling,
    so it investigates in the order the differs emitted.

    `on_skip` is called with a reason when investigation cannot run at all. The
    findings still come back unchanged — enrichment failing must never fail a
    gate — but the caller gets the chance to say so out loud.
    """
    if not findings:
        return findings

    try:
        client = client or _default_client()
    except InvestigationUnavailable as exc:
        if on_skip is not None:
            on_skip(str(exc))
        return findings

    enriched: list[Finding] = []
    for index, finding in enumerate(findings):
        if index >= max_findings:
            enriched.append(finding)
            continue
        try:
            summary, calls = _investigate_one(
                finding, source, client=client, model=model, max_turns=max_turns
            )
        except Exception as exc:  # noqa: BLE001 - enrichment must never fail a run
            evidence = dict(finding.evidence)
            evidence["investigation_error"] = f"{type(exc).__name__}: {exc}"
            enriched.append(finding.model_copy(update={"evidence": evidence}))
            continue

        evidence = dict(finding.evidence)
        evidence["investigation"] = summary
        evidence["investigation_tool_calls"] = calls
        enriched.append(finding.model_copy(update={"evidence": evidence}))

    return enriched


def _default_client() -> Any:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise InvestigationUnavailable("ANTHROPIC_API_KEY is not set.")

    try:
        import anthropic
    except ImportError as exc:
        raise InvestigationUnavailable(
            'the `anthropic` package is not installed (`pip install -e ".[llm]"`).'
        ) from exc
    return anthropic.Anthropic()


def _investigate_one(
    finding: Finding,
    source: Any,
    *,
    client: Any,
    model: str,
    max_turns: int,
) -> tuple[str, int]:
    """One bounded read → reason → call → read loop. Returns (summary, tool_call_count)."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": _brief(finding)}]
    tool_calls = 0

    for _ in range(max_turns):
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=_SYSTEM,
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},
            tools=tools_for(source),
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            return _text_of(response), tool_calls

        messages.append({"role": "assistant", "content": response.content})

        results: list[dict[str, Any]] = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            tool_calls += 1
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    **_run_tool(source, block.name, block.input),
                }
            )
        messages.append({"role": "user", "content": results})

    # Budget exhausted. Whatever was learned is still worth keeping.
    return "Investigation reached its turn limit before concluding.", tool_calls


def _run_tool(source: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch to the MCP source. Tool errors are returned to the model, not raised.

    A failed tool is information the model can route around — it might try a
    different question. Raising here would abort an investigation over one
    unavailable endpoint.
    """
    try:
        if name == "get_dataset_queries":
            result = source.get_dataset_queries(args["urn"], column=args.get("column"))
        elif name == "search":
            result = source.search(args["query"])
        elif name == "search_documents":
            result = source.search_documents(args["query"])
        elif name == "get_lineage":
            direction = "UPSTREAM" if args.get("upstream", True) else "DOWNSTREAM"
            result = [
                {"target": e.target_urn, "relationship": e.relationship}
                for e in source.get_lineage(args["urn"], direction=direction)
            ]
        else:
            return {"content": f"Unknown tool: {name}", "is_error": True}
    except Exception as exc:  # noqa: BLE001 - surfaced to the model as a tool error
        return {"content": f"{type(exc).__name__}: {exc}", "is_error": True}

    return {"content": _truncate(result)}


def _truncate(result: Any, limit: int = 6000) -> str:
    """Tool results can be large; the model does not need all of a query log."""
    try:
        text = json.dumps(result, default=str)
    except (TypeError, ValueError):
        text = str(result)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated, {len(text)} chars total]"


def _text_of(response: Any) -> str:
    parts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    return "\n".join(p for p in parts if p).strip() or "No additional context found."


def _brief(finding: Finding) -> str:
    """What the agent is told. Deliberately excludes severity.

    Withholding the verdict is not just tidiness — it removes the frame in which
    "should this block?" is even a question the model might try to answer, and
    keeps it on the task that is actually useful.
    """
    lines = [
        f"A change was detected on {finding.subject_urn}.",
        f"Kind: {finding.kind.value}",
        f"Summary: {finding.summary}",
    ]
    if finding.subject_column:
        lines.append(f"Column: {finding.subject_column}")
    if finding.affected_feature_urn:
        lines.append(f"Reaches ML feature: {finding.affected_feature_urn}")
    if finding.path and finding.path.hops:
        chain = " -> ".join(h.short_label() for h in finding.path.hops)
        lines.append(f"Lineage path: {chain}")
    lines.append("\nInvestigate and report what an on-call engineer should know.")
    return "\n".join(lines)


__all__ = [
    "investigate_findings",
    "unavailable_reason",
    "InvestigationUnavailable",
    "INVESTIGATION_TOOLS",
]
