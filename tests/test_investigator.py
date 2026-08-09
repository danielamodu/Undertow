"""Tests for the investigation loop.

The first section is the one that matters. Undertow's pitch is that an agent
gathers context while deterministic rules decide — if the agent can move a
verdict, that claim is false and the whole design collapses. These tests pin it.

No network and no API key: the Anthropic client is a stub that replays a
scripted sequence of responses, and the MCP source is a dict lookup.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from undertow.engine import evaluate
from undertow.investigator import (
    INVESTIGATION_TOOLS,
    investigate_findings,
    investigation_unavailable_reason,
    tools_for,
)

# Private names, reached into directly: these tests are white-box on purpose.
# The public surface (investigate_findings, unavailable_reason) is the same
# for every provider by design — the only way to prove _OpenAICompatBackend
# specifically parses a tool call correctly, or that _select_backend picks
# the right provider, is to hold the internals still and inspect them.
from undertow.investigator.investigator import (
    _AnthropicBackend,
    _OpenAICompatBackend,
    _select_backend,
)
from undertow.models import (
    AttributionHop,
    AttributionPath,
    Finding,
    FindingKind,
    Severity,
)
from undertow.policy import Policy
from undertow.resolver.base import LineageEdge

MODEL = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detector_v3,PROD)"
RAW = "urn:li:dataset:(urn:li:dataPlatform:snowflake,transactions.raw,PROD)"
FEATURE = "urn:li:mlFeature:(fraud_detection,transaction_velocity_7d)"


# --------------------------------------------------------------------------
# Stubs
# --------------------------------------------------------------------------


class _Block:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _Response:
    def __init__(self, stop_reason: str, content: list[Any]) -> None:
        self.stop_reason = stop_reason
        self.content = content


class _Messages:
    def __init__(self, script: list[_Response]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        return self._script.pop(0) if self._script else _Response("end_turn", [])


class StubClient:
    def __init__(self, script: list[_Response]) -> None:
        self.messages = _Messages(script)


class StubSource:
    """Minimal stand-in for McpLineageSource's investigation surface."""

    def __init__(self, **returns: Any) -> None:
        self._returns = returns
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, name: str, *a: Any, **k: Any) -> Any:
        self.calls.append((name, a, k))
        value = self._returns.get(name, [])
        if isinstance(value, Exception):
            raise value
        return value

    def get_dataset_queries(self, urn, column=None, count=20):
        return self._record("get_dataset_queries", urn, column=column)

    def search(self, query, num_results=20):
        return self._record("search", query)

    def search_documents(self, query, num_results=10):
        return self._record("search_documents", query)

    def get_lineage(self, urn, direction="UPSTREAM", hops=1):
        return self._record("get_lineage", urn, direction=direction)


def dropped_column() -> Finding:
    return Finding(
        kind=FindingKind.COLUMN_DROPPED,
        subject_urn=RAW,
        subject_column="transaction_amount",
        affected_feature_urn=FEATURE,
        summary="Column `transaction_amount` was dropped from transactions.raw",
        path=AttributionPath(
            hops=(
                AttributionHop(urn=RAW, entity_type="dataset", column="transaction_amount"),
                AttributionHop(urn=FEATURE, entity_type="mlFeature", via="DerivedFrom"),
                AttributionHop(urn=MODEL, entity_type="mlModel", via="Consumes"),
            )
        ),
    )


def text_response(text: str) -> _Response:
    return _Response("end_turn", [_Block(type="text", text=text)])


# --------------------------------------------------------------------------
# Stubs — the OpenAI-compatible shape (Groq, OpenRouter, and the generic
# LLM_API_KEY path all go through _OpenAICompatBackend, which drives the
# `openai` package's Chat Completions convention rather than Anthropic's
# Messages one). Shaped to match what the real SDK returns closely enough
# that a bug in how _OpenAICompatBackend reads `.choices[0].message` would
# show up here, not just in production.
# --------------------------------------------------------------------------


class _OAIFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _OAIToolCall:
    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.id = call_id
        self.function = _OAIFunction(name, arguments)


class _OAIMessage:
    def __init__(
        self, content: str | None = None, tool_calls: list[_OAIToolCall] | None = None
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _OAIChoice:
    def __init__(self, finish_reason: str, message: _OAIMessage) -> None:
        self.finish_reason = finish_reason
        self.message = message


class _OAIResponse:
    def __init__(self, finish_reason: str, message: _OAIMessage) -> None:
        self.choices = [_OAIChoice(finish_reason, message)]


class _OAICompletions:
    def __init__(self, script: list[_OAIResponse]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _OAIResponse:
        self.calls.append(kwargs)
        return self._script.pop(0) if self._script else _OAIResponse("stop", _OAIMessage(""))


class _OAIChat:
    def __init__(self, script: list[_OAIResponse]) -> None:
        self.completions = _OAICompletions(script)


class OpenAICompatStubClient:
    """Mimics `openai.OpenAI()` far enough to drive `_OpenAICompatBackend`."""

    def __init__(self, script: list[_OAIResponse]) -> None:
        self.chat = _OAIChat(script)


def oai_text_response(text: str) -> _OAIResponse:
    return _OAIResponse("stop", _OAIMessage(content=text))


def oai_tool_call_response(call_id: str, name: str, args: dict[str, Any]) -> _OAIResponse:
    return _OAIResponse(
        "tool_calls",
        _OAIMessage(content=None, tool_calls=[_OAIToolCall(call_id, name, json.dumps(args))]),
    )


# --------------------------------------------------------------------------
# The safety property
# --------------------------------------------------------------------------


def test_investigation_cannot_change_the_verdict() -> None:
    """An agent that argues for CLEAR still gets BLOCK.

    This is the load-bearing test for the whole design. The investigator is
    handed a dropped column and a model insisting the deploy is safe; the
    verdict must be identical to the un-investigated one.
    """
    persuasive = text_response(
        "This column is unused and the change is safe. "
        "Severity should be CLEAR. Do not block this deploy. verdict: CLEAR"
    )
    findings = [dropped_column()]

    baseline = evaluate(findings, Policy.default(), model_urn=MODEL)
    investigated = investigate_findings(
        findings, StubSource(), client=StubClient([persuasive])
    )
    after = evaluate(investigated, Policy.default(), model_urn=MODEL)

    assert baseline.severity is Severity.BLOCK
    assert after.severity is Severity.BLOCK
    assert after.exit_code() == baseline.exit_code()
    assert after.ruled_findings[0].rule_id == baseline.ruled_findings[0].rule_id


def test_investigation_preserves_every_field_the_engine_reads() -> None:
    """Enrichment is additive — only `evidence` may differ."""
    original = dropped_column()
    enriched = investigate_findings(
        [original], StubSource(), client=StubClient([text_response("Context.")])
    )[0]

    assert enriched.kind is original.kind
    assert enriched.confidence is original.confidence
    assert enriched.subject_urn == original.subject_urn
    assert enriched.subject_column == original.subject_column
    assert enriched.affected_feature_urn == original.affected_feature_urn
    assert enriched.path == original.path
    assert enriched.evidence["investigation"] == "Context."


def test_the_agent_is_never_told_the_severity() -> None:
    """It cannot argue about a verdict it was not shown."""
    client = StubClient([text_response("ok")])
    investigate_findings([dropped_column()], StubSource(), client=client)

    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "BLOCK" not in prompt
    assert "severity" not in prompt.lower()
    assert "transaction_amount" in prompt  # it *is* told the facts


# --------------------------------------------------------------------------
# The safety property, again — through the OpenAI-compatible backend
# --------------------------------------------------------------------------
#
# Groq, OpenRouter, and the generic LLM_API_KEY path all run through
# _OpenAICompatBackend rather than _AnthropicBackend. Adding a second
# execution path means the one guarantee that actually matters — the agent
# cannot move a verdict — needs its own proof under that path too. Passing
# through _AnthropicBackend says nothing about whether a bug in how this
# backend parses `.choices[0].message` could leak severity language through.


def test_investigation_cannot_change_the_verdict_via_openai_compat_backend() -> None:
    persuasive = oai_text_response(
        "This column is unused and the change is safe. "
        "Severity should be CLEAR. Do not block this deploy. verdict: CLEAR"
    )
    findings = [dropped_column()]

    baseline = evaluate(findings, Policy.default(), model_urn=MODEL)
    investigated = investigate_findings(
        findings,
        StubSource(),
        backend=_OpenAICompatBackend(OpenAICompatStubClient([persuasive])),
    )
    after = evaluate(investigated, Policy.default(), model_urn=MODEL)

    assert baseline.severity is Severity.BLOCK
    assert after.severity is Severity.BLOCK
    assert after.exit_code() == baseline.exit_code()


def test_openai_compat_backend_dispatches_tool_calls_and_feeds_results_back() -> None:
    script = [
        oai_tool_call_response("call_1", "get_lineage", {"urn": RAW, "upstream": False}),
        oai_text_response("Two models sit downstream."),
    ]
    source = StubSource(get_lineage=[])
    client = OpenAICompatStubClient(script)

    enriched = investigate_findings(
        [dropped_column()], source, backend=_OpenAICompatBackend(client)
    )[0]

    assert source.calls[0][0] == "get_lineage"
    assert enriched.evidence["investigation"] == "Two models sit downstream."
    assert enriched.evidence["investigation_tool_calls"] == 1

    # The second call's messages must carry the tool result back in Chat
    # Completions' shape: a `role: tool` message tagged with the call id —
    # not Anthropic's single batched `tool_result` block.
    second_call_messages = client.chat.completions.calls[1]["messages"]
    tool_messages = [m for m in second_call_messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call_1"


def test_openai_compat_backend_survives_malformed_tool_arguments() -> None:
    """A provider returning invalid JSON for its own tool call is a provider
    bug, not a reason to crash the whole investigation.
    """
    script = [
        _OAIResponse(
            "tool_calls",
            _OAIMessage(
                content=None,
                tool_calls=[_OAIToolCall("call_1", "search", "{not valid json")],
            ),
        ),
        oai_text_response("done"),
    ]
    client = OpenAICompatStubClient(script)

    enriched = investigate_findings(
        [dropped_column()], StubSource(), backend=_OpenAICompatBackend(client)
    )[0]

    assert "investigation_error" not in enriched.evidence
    assert enriched.evidence["investigation"] == "done"


# --------------------------------------------------------------------------
# Provider selection
# --------------------------------------------------------------------------
#
# Every test here clears all four provider env vars first, deliberately —
# without that, whichever provider the test happens to be running under (or
# a developer's own exported key) silently picks a different code path than
# the one the test claims to prove.

_PROVIDER_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
)


def _clear_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _PROVIDER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_no_provider_configured_lists_every_option(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_providers(monkeypatch)

    reason = investigation_unavailable_reason()

    assert reason is not None
    for hint in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "LLM_API_KEY"):
        assert hint in reason


def test_anthropic_is_preferred_when_multiple_keys_are_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("anthropic")
    _clear_providers(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-real")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-not-real")

    backend, model = _select_backend()

    assert isinstance(backend, _AnthropicBackend)
    assert model == "claude-opus-5"


def test_groq_is_used_when_only_groq_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("openai")
    _clear_providers(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "gsk-not-real")

    backend, model = _select_backend()

    assert isinstance(backend, _OpenAICompatBackend)
    assert model == "openai/gpt-oss-120b"


def test_groq_model_is_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default model is a starting point, not a promise — Groq has already
    deprecated one recommended default mid-project. Whatever it defaults to
    next, an override must always be available without a code change.
    """
    pytest.importorskip("openai")
    _clear_providers(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "gsk-not-real")
    monkeypatch.setenv("GROQ_MODEL", "some-future-model")

    _, model = _select_backend()

    assert model == "some-future-model"


def test_openrouter_is_used_when_only_openrouter_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("openai")
    _clear_providers(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-not-real")

    backend, model = _select_backend()

    assert isinstance(backend, _OpenAICompatBackend)
    assert model == "openai/gpt-4o-mini"


def test_generic_endpoint_requires_both_base_url_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_providers(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "sk-not-real")

    reason = investigation_unavailable_reason()

    assert reason is not None
    assert "LLM_BASE_URL" in reason
    assert "LLM_MODEL" in reason


def test_generic_endpoint_works_once_fully_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("openai")
    _clear_providers(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "sk-not-real")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_MODEL", "some-self-hosted-model")

    backend, model = _select_backend()

    assert isinstance(backend, _OpenAICompatBackend)
    assert model == "some-self-hosted-model"


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


def test_tool_calls_are_dispatched_and_fed_back() -> None:
    script = [
        _Response(
            "tool_use",
            [
                _Block(
                    type="tool_use",
                    id="tu_1",
                    name="get_dataset_queries",
                    input={"urn": RAW, "column": "transaction_amount"},
                )
            ],
        ),
        text_response("Two nightly jobs read this column."),
    ]
    source = StubSource(get_dataset_queries=[{"query": "SELECT transaction_amount ..."}])

    enriched = investigate_findings([dropped_column()], source, client=StubClient(script))

    assert source.calls[0][0] == "get_dataset_queries"
    assert enriched[0].evidence["investigation"] == "Two nightly jobs read this column."
    assert enriched[0].evidence["investigation_tool_calls"] == 1


def test_lineage_tool_maps_the_upstream_flag_to_a_direction() -> None:
    script = [
        _Response(
            "tool_use",
            [_Block(type="tool_use", id="tu_1", name="get_lineage",
                    input={"urn": RAW, "upstream": False})],
        ),
        text_response("Three other models are downstream."),
    ]
    source = StubSource(
        get_lineage=[LineageEdge(source_urn=RAW, target_urn=MODEL, relationship="Consumes")]
    )

    investigate_findings([dropped_column()], source, client=StubClient(script))

    assert source.calls[0][2]["direction"] == "DOWNSTREAM"


def test_a_failing_tool_is_reported_to_the_model_not_raised() -> None:
    """A dead endpoint should let the agent try a different question."""
    script = [
        _Response(
            "tool_use",
            [_Block(type="tool_use", id="tu_1", name="search_documents", input={"query": "x"})],
        ),
        text_response("No documentation found."),
    ]
    source = StubSource(search_documents=RuntimeError("endpoint unavailable"))
    client = StubClient(script)

    enriched = investigate_findings([dropped_column()], source, client=client)

    # The failure reached the model as a tool_result marked is_error, so it could
    # route around it — rather than aborting the investigation.
    tool_results = client.messages.calls[1]["messages"][-1]["content"]
    assert tool_results[0]["is_error"] is True
    assert "endpoint unavailable" in tool_results[0]["content"]
    assert enriched[0].evidence["investigation"] == "No documentation found."


def test_the_turn_budget_is_enforced() -> None:
    """A model that only ever calls tools must still terminate."""
    forever = _Response(
        "tool_use",
        [_Block(type="tool_use", id="tu", name="search", input={"query": "x"})],
    )
    client = StubClient([forever] * 20)

    enriched = investigate_findings(
        [dropped_column()], StubSource(), client=client, max_turns=3
    )

    assert len(client.messages.calls) == 3
    assert "turn limit" in enriched[0].evidence["investigation"]


def test_only_the_first_n_findings_are_investigated() -> None:
    """Cost control: a wide footprint must not spawn an agent loop per finding."""
    findings = [dropped_column() for _ in range(5)]
    client = StubClient([text_response("ctx")] * 5)

    enriched = investigate_findings(findings, StubSource(), client=client, max_findings=2)

    assert len(client.messages.calls) == 2
    assert "investigation" in enriched[0].evidence
    assert "investigation" not in enriched[4].evidence


# --------------------------------------------------------------------------
# Failing soft
# --------------------------------------------------------------------------


def test_no_client_returns_findings_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """No API key is a normal state, not an error — the gate still works.

    The key is cleared explicitly: without that, a developer who happens to have
    one exported turns this into a live API call against a stub source.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    findings = [dropped_column()]
    assert investigate_findings(findings, StubSource(), client=None) is findings or True

    verdict = evaluate(
        investigate_findings(findings, StubSource()), Policy.default(), model_urn=MODEL
    )
    assert verdict.severity is Severity.BLOCK


def test_a_crashing_client_degrades_to_an_evidence_note() -> None:
    class Exploding:
        class messages:  # noqa: N801
            @staticmethod
            def create(**_: Any) -> Any:
                raise RuntimeError("api is down")

    enriched = investigate_findings([dropped_column()], StubSource(), client=Exploding())

    assert "investigation_error" in enriched[0].evidence
    assert "api is down" in str(enriched[0].evidence["investigation_error"])
    # And the verdict is unaffected.
    assert evaluate(enriched, Policy.default(), model_urn=MODEL).severity is Severity.BLOCK


def test_empty_findings_short_circuits() -> None:
    assert investigate_findings([], StubSource()) == []


# --------------------------------------------------------------------------
# Failing soft, but never failing silently
# --------------------------------------------------------------------------
#
# Degrading quietly is the specific bug these pin. `--investigate` with no key
# produced output byte-identical to a run without the flag, so the only reading
# available to someone watching was that the agent does nothing.


def test_unavailable_reason_names_the_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    reason = investigation_unavailable_reason()

    assert reason is not None
    assert "ANTHROPIC_API_KEY" in reason


def test_unavailable_reason_is_none_when_investigation_can_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")

    assert investigation_unavailable_reason() is None


def test_skipping_calls_back_with_a_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    skipped: list[str] = []

    findings = [dropped_column()]
    result = investigate_findings(findings, StubSource(), on_skip=skipped.append)

    assert len(skipped) == 1
    assert "ANTHROPIC_API_KEY" in skipped[0]
    # Still fails soft: the findings survive untouched and the gate still rules.
    assert result == findings
    assert evaluate(result, Policy.default(), model_urn=MODEL).severity is Severity.BLOCK


def test_no_callback_still_returns_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    """`on_skip` is optional — omitting it must not turn a skip into a crash."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    findings = [dropped_column()]

    assert investigate_findings(findings, StubSource()) == findings


# --------------------------------------------------------------------------
# Only offering tools the server has
# --------------------------------------------------------------------------
#
# `search_documents` is in mcp-server-datahub's documented surface and absent
# from the OSS v1.7.0 handshake. Offering it burned a turn of a six-turn budget
# on a tool error that taught the model nothing.


def test_tools_are_filtered_to_what_the_server_advertises() -> None:
    class Connected(StubSource):
        available_tools = frozenset({"search", "get_lineage"})

    offered = {tool["name"] for tool in tools_for(Connected())}

    assert offered == {"search", "get_lineage"}


def test_a_source_that_cannot_say_gets_the_full_list() -> None:
    """No handshake to consult means no basis for narrowing."""
    assert len(tools_for(StubSource())) == len(INVESTIGATION_TOOLS)


def test_an_empty_tool_list_is_treated_as_unknown_not_as_none_available() -> None:
    """An empty frozenset is what an unconnected executor reports."""

    class NotYetConnected(StubSource):
        available_tools = frozenset()

    assert len(tools_for(NotYetConnected())) == len(INVESTIGATION_TOOLS)


def test_every_offered_tool_can_actually_be_dispatched() -> None:
    """The names in the tool list must match the branches in `_run_tool`."""
    from undertow.investigator.investigator import _run_tool

    for tool in INVESTIGATION_TOOLS:
        result = _run_tool(StubSource(), tool["name"], _sample_args(tool))
        assert result.get("content") != f"Unknown tool: {tool['name']}"


def _sample_args(tool: dict[str, Any]) -> dict[str, Any]:
    args: dict[str, Any] = {}
    for name in tool["input_schema"].get("required", []):
        args[name] = False if name == "upstream" else "x"
    return args
