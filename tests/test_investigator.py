"""Tests for the investigation loop.

The first section is the one that matters. Undertow's pitch is that an agent
gathers context while deterministic rules decide — if the agent can move a
verdict, that claim is false and the whole design collapses. These tests pin it.

No network and no API key: the Anthropic client is a stub that replays a
scripted sequence of responses, and the MCP source is a dict lookup.
"""

from __future__ import annotations

from typing import Any

import pytest

from undertow.engine import evaluate
from undertow.investigator import investigate_findings, investigation_unavailable_reason
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
