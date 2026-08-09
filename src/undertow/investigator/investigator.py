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
WARN, and `test_investigator.py` pins that, for every provider below.

## Failure policy

Unlike the resolver, this layer **fails soft**. An investigation that errors,
times out, or exhausts its budget returns the findings unchanged. Enrichment is
commentary on a verdict that has already been computed from facts, so losing it
costs context, never correctness — the opposite trade-off from the resolver,
where silence would be read as safety.

## Why more than one provider

Anthropic is the default, and the one this project is built and tested
against most. But it is not free, and not everyone recording a demo or
running this in CI has a key for it. Groq and OpenRouter both offer usable
free tiers, and — this is the part that keeps this from being three separate
integrations — both speak the same OpenAI-compatible wire format everyone
else in this space has converged on. So there is exactly one non-Anthropic
code path (`_OpenAICompatBackend`), parameterised by a base URL, plus a
generic escape hatch (`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`) for
whichever OpenAI-compatible endpoint isn't one of the two presets.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from undertow.models import Finding

# Read-only tools, verified present on mcp-server-datahub 0.6.0's OSS build.
# Mutation tools are gated off in OSS, so there is nothing here that could write
# to the catalog even if the model asked — but the allowlist is explicit anyway,
# because "the server wouldn't let it" is a weaker guarantee than "we never
# offered it". Provider-neutral shape (Anthropic's tool-schema convention);
# each backend below translates it into whatever its own API expects.
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
    """No provider configured, no SDK, or no MCP source — skipped, not failed."""


# --------------------------------------------------------------------------
# Backends — one seam, two shapes behind it
# --------------------------------------------------------------------------
#
# Anthropic and OpenAI-compatible APIs disagree on almost everything about a
# tool-calling turn: the response envelope, how a tool result gets fed back,
# even whether the system prompt is a parameter or a message in the list.
# `_investigate_one` knows none of that — it only calls the six methods below.
# Adding a provider that needs genuinely different wire behaviour (not just a
# different base URL) means adding one more class here, not touching the loop.


class _Backend:
    def tools_for(self, source: Any) -> Any:
        raise NotImplementedError

    def ask(
        self, *, model: str, system: str, tools: Any, messages: list[dict[str, Any]]
    ) -> Any:
        raise NotImplementedError

    def is_tool_use(self, response: Any) -> bool:
        raise NotImplementedError

    def tool_calls(self, response: Any) -> list[tuple[str, str, dict[str, Any]]]:
        raise NotImplementedError

    def text_of(self, response: Any) -> str:
        raise NotImplementedError

    def append_assistant_turn(self, messages: list[dict[str, Any]], response: Any) -> None:
        raise NotImplementedError

    def append_tool_results(
        self, messages: list[dict[str, Any]], results: list[tuple[str, dict[str, Any]]]
    ) -> None:
        raise NotImplementedError


class _AnthropicBackend(_Backend):
    """Claude's Messages API — the default, and the most-tested path."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def tools_for(self, source: Any) -> list[dict[str, Any]]:
        return tools_for(source)

    def ask(
        self, *, model: str, system: str, tools: Any, messages: list[dict[str, Any]]
    ) -> Any:
        return self._client.messages.create(
            model=model,
            max_tokens=4096,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},
            tools=tools,
            messages=messages,
        )

    def is_tool_use(self, response: Any) -> bool:
        return bool(response.stop_reason == "tool_use")

    def tool_calls(self, response: Any) -> list[tuple[str, str, dict[str, Any]]]:
        return [
            (block.id, block.name, block.input)
            for block in response.content
            if getattr(block, "type", None) == "tool_use"
        ]

    def text_of(self, response: Any) -> str:
        return _text_of(response)

    def append_assistant_turn(self, messages: list[dict[str, Any]], response: Any) -> None:
        messages.append({"role": "assistant", "content": response.content})

    def append_tool_results(
        self, messages: list[dict[str, Any]], results: list[tuple[str, dict[str, Any]]]
    ) -> None:
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": call_id, **payload}
                    for call_id, payload in results
                ],
            }
        )


class _OpenAICompatBackend(_Backend):
    """Chat Completions with function-calling — Groq, OpenRouter, or anything
    else the `openai` package can point a `base_url` at.

    Not a claim that every such provider is feature-complete with OpenAI —
    Groq's own docs describe their compatibility layer as "mostly compatible,
    not feature-complete." What this backend uses (tool calls, a system
    message, `max_tokens`) is the common subset every provider with a preset
    below actually supports.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def tools_for(self, source: Any) -> list[dict[str, Any]]:
        return [_to_openai_tool(tool) for tool in tools_for(source)]

    def ask(
        self, *, model: str, system: str, tools: Any, messages: list[dict[str, Any]]
    ) -> Any:
        full_messages = [{"role": "system", "content": system}, *messages]
        return self._client.chat.completions.create(
            model=model,
            max_tokens=4096,
            messages=full_messages,
            tools=tools,
        )

    def is_tool_use(self, response: Any) -> bool:
        return bool(response.choices[0].finish_reason == "tool_calls")

    def tool_calls(self, response: Any) -> list[tuple[str, str, dict[str, Any]]]:
        message = response.choices[0].message
        calls = getattr(message, "tool_calls", None) or []
        parsed: list[tuple[str, str, dict[str, Any]]] = []
        for call in calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except (TypeError, ValueError):
                # A provider that returns malformed JSON for its own tool call
                # is a provider bug, not a reason to crash the loop — treat it
                # as a call with no arguments and let the tool dispatcher (or
                # the model, reading the resulting error) sort it out.
                args = {}
            parsed.append((call.id, call.function.name, args))
        return parsed

    def text_of(self, response: Any) -> str:
        text = response.choices[0].message.content
        return (text or "").strip() or "No additional context found."

    def append_assistant_turn(self, messages: list[dict[str, Any]], response: Any) -> None:
        message = response.choices[0].message
        entry: dict[str, Any] = {"role": "assistant", "content": message.content}
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            entry["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in tool_calls
            ]
        messages.append(entry)

    def append_tool_results(
        self, messages: list[dict[str, Any]], results: list[tuple[str, dict[str, Any]]]
    ) -> None:
        # Unlike Anthropic, which batches a whole turn's results into one
        # message, Chat Completions wants one `tool` message per call, each
        # tagged with the `tool_call_id` it answers.
        for call_id, payload in results:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": payload.get("content", ""),
                }
            )


def _to_openai_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
        },
    }


# --------------------------------------------------------------------------
# Provider selection
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _OpenAICompatPreset:
    key_env: str
    label: str
    base_url: str
    model_env: str
    # Not a permanent claim about which model is best — model lineups on
    # these platforms change often (Groq deprecated its own recommended
    # default mid-2026). It's a starting point, always overridable by the
    # model_env var above, precisely so a future deprecation doesn't strand
    # anyone the way a hardcoded, unconfigurable choice would.
    default_model: str


_OPENAI_COMPAT_PRESETS: tuple[_OpenAICompatPreset, ...] = (
    _OpenAICompatPreset(
        key_env="GROQ_API_KEY",
        label="Groq",
        base_url="https://api.groq.com/openai/v1",
        model_env="GROQ_MODEL",
        default_model="openai/gpt-oss-120b",
    ),
    _OpenAICompatPreset(
        key_env="OPENROUTER_API_KEY",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        model_env="OPENROUTER_MODEL",
        default_model="openai/gpt-4o-mini",
    ),
)


def _select_backend() -> tuple[_Backend, str]:
    """First provider with a usable key wins. Returns (backend, model).

    Order: Anthropic, then the presets above in the order declared, then a
    fully generic OpenAI-compatible endpoint. Anthropic first because it's
    what this project is built and most tested against; the rest exist
    because not everyone has, or wants to pay for, a key for it.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic
        except ImportError as exc:
            raise InvestigationUnavailable(
                "ANTHROPIC_API_KEY is set but the `anthropic` package is not "
                'installed (`pip install -e ".[llm]"`).'
            ) from exc
        model = os.environ.get("UNDERTOW_ANTHROPIC_MODEL", "claude-opus-5")
        return _AnthropicBackend(anthropic.Anthropic()), model

    for preset in _OPENAI_COMPAT_PRESETS:
        api_key = os.environ.get(preset.key_env)
        if not api_key:
            continue
        model = os.environ.get(preset.model_env, preset.default_model)
        return _OpenAICompatBackend(_openai_client(api_key, preset.base_url)), model

    generic_key = os.environ.get("LLM_API_KEY")
    if generic_key:
        generic_url = os.environ.get("LLM_BASE_URL")
        generic_model = os.environ.get("LLM_MODEL")
        if not generic_url or not generic_model:
            raise InvestigationUnavailable(
                "LLM_API_KEY is set, but LLM_BASE_URL and LLM_MODEL are both also "
                "required for a custom endpoint — there's no default to fall back to "
                "for a provider this code has never heard of."
            )
        return _OpenAICompatBackend(_openai_client(generic_key, generic_url)), generic_model

    raise InvestigationUnavailable(
        "no LLM is configured. Set one of: ANTHROPIC_API_KEY, GROQ_API_KEY, "
        "OPENROUTER_API_KEY, or LLM_API_KEY + LLM_BASE_URL + LLM_MODEL for any other "
        "OpenAI-compatible endpoint."
    )


def _openai_client(api_key: str, base_url: str | None) -> Any:
    try:
        import openai
    except ImportError as exc:
        raise InvestigationUnavailable(
            'the `openai` package is not installed (`pip install -e ".[llm]"`).'
        ) from exc
    return openai.OpenAI(api_key=api_key, base_url=base_url)


def unavailable_reason() -> str | None:
    """Why investigation cannot run, or `None` if it can.

    Exists so a caller can say *why* nothing happened before doing the work.
    Skipping quietly is the failure mode that matters here: `--investigate`
    with no provider configured produces output byte-identical to a run
    without the flag, and the only available reading of that is that the
    agent does nothing.
    """
    try:
        _select_backend()
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
    backend: _Backend | None = None,
    model: str | None = None,
    max_turns: int = 6,
    max_findings: int = 3,
    on_skip: Callable[[str], None] | None = None,
    on_tool_call: Callable[[str, dict[str, Any]], None] | None = None,
) -> list[Finding]:
    """Enrich findings with investigated context. Never changes severity.

    `client` is an Anthropic-shaped client, kept for direct injection and
    backward compatibility — it always resolves to `_AnthropicBackend`.
    `backend` overrides provider selection entirely, for anything not
    Anthropic. Neither given: providers are auto-detected from the
    environment, in the order described in `_select_backend`.

    `max_findings` bounds cost: a footprint that produces twenty findings does
    not warrant twenty agent loops in a CI gate. The most severe are already
    sorted first by the time a reporter sees them, but this runs before ruling,
    so it investigates in the order the differs emitted.

    `on_skip` is called with a reason when investigation cannot run at all. The
    findings still come back unchanged — enrichment failing must never fail a
    gate — but the caller gets the chance to say so out loud.

    `on_tool_call` is called with (tool_name, args) the moment each call is
    about to run, before its result comes back. Without this the loop is
    invisible end to end: nothing prints while it runs, and the finished report
    never showed the transcript either — a `--investigate` run looked identical
    to one without it, which reads as "the agent does nothing" rather than "the
    agent did real work and only the terminal hid it."
    """
    if not findings:
        return findings

    if backend is not None:
        resolved_backend, resolved_model = backend, (model or "claude-opus-5")
    elif client is not None:
        resolved_backend, resolved_model = _AnthropicBackend(client), (model or "claude-opus-5")
    else:
        try:
            resolved_backend, provider_model = _select_backend()
        except InvestigationUnavailable as exc:
            if on_skip is not None:
                on_skip(str(exc))
            return findings
        resolved_model = model or provider_model

    enriched: list[Finding] = []
    for index, finding in enumerate(findings):
        if index >= max_findings:
            enriched.append(finding)
            continue
        try:
            summary, calls = _investigate_one(
                finding,
                source,
                backend=resolved_backend,
                model=resolved_model,
                max_turns=max_turns,
                on_tool_call=on_tool_call,
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


def _investigate_one(
    finding: Finding,
    source: Any,
    *,
    backend: _Backend,
    model: str,
    max_turns: int,
    on_tool_call: Callable[[str, dict[str, Any]], None] | None = None,
) -> tuple[str, int]:
    """One bounded read → reason → call → read loop. Returns (summary, tool_call_count).

    Provider-neutral: everything that differs between Anthropic and an
    OpenAI-compatible endpoint is behind `backend`. This function only knows
    the shape of the loop, never the shape of a response.
    """
    messages: list[dict[str, Any]] = [{"role": "user", "content": _brief(finding)}]
    tool_calls = 0
    tools = backend.tools_for(source)

    for _ in range(max_turns):
        response = backend.ask(model=model, system=_SYSTEM, tools=tools, messages=messages)

        if not backend.is_tool_use(response):
            return backend.text_of(response), tool_calls

        backend.append_assistant_turn(messages, response)

        results: list[tuple[str, dict[str, Any]]] = []
        for call_id, name, args in backend.tool_calls(response):
            tool_calls += 1
            # Fired before the call runs, not after — a reader watching this
            # scroll should see the question the moment it's asked, not the
            # whole loop's worth of questions arriving in a burst once it ends.
            if on_tool_call is not None:
                on_tool_call(name, args)
            results.append((call_id, _run_tool(source, name, args)))
        backend.append_tool_results(messages, results)

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
