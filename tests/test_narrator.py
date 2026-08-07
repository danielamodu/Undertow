"""Tests for Undertow LLM Narrator and Jinja2 fallback."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from undertow.models import (
    AttributionHop,
    AttributionPath,
    Finding,
    FindingKind,
    RuledFinding,
    Severity,
    Verdict,
)
from undertow.narrator import (
    generate_narrative,
    generate_narrative_detailed,
    render_template,
    validate_narrative_urns,
)


@pytest.fixture
def sample_verdict() -> Verdict:
    model_urn = "urn:li:mlModel:(urn:li:dataPlatform:mlflow,fraud_detector_v3,PROD)"
    ds_urn = "urn:li:dataset:(urn:li:dataPlatform:snowflake,staging.payments_clean,PROD)"
    feat_urn = "urn:li:mlFeature:(txn_aggregates,avg_txn_30d)"

    path = AttributionPath(
        hops=(
            AttributionHop(urn=ds_urn, entity_type="dataset", column="amount_usd"),
            AttributionHop(urn=feat_urn, entity_type="mlFeature", via="DerivedFrom"),
            AttributionHop(urn=model_urn, entity_type="mlModel", via="Consumes"),
        ),
        owners=("data-eng-tom",),
    )

    finding = Finding(
        kind=FindingKind.COLUMN_DROPPED,
        subject_urn=ds_urn,
        subject_column="amount_usd",
        affected_feature_urn=feat_urn,
        summary="Column amount_usd was dropped from upstream table",
        path=path,
    )

    ruled = RuledFinding(
        finding=finding,
        severity=Severity.BLOCK,
        rule_id="upstream-column-dropped",
    )

    return Verdict(
        model_urn=model_urn,
        severity=Severity.BLOCK,
        ruled_findings=(ruled,),
        assets_checked=10,
        checked_at=datetime.now(UTC),
    )


def test_jinja2_template_fallback_render(sample_verdict: Verdict) -> None:
    output = render_template(sample_verdict)
    assert "Undertow ML Deploy Verdict: BLOCK" in output
    assert "COLUMN_DROPPED" in output
    assert "amount_usd" in output
    assert "data-eng-tom" in output
    assert "Checked 10 upstream assets" in output


def test_urn_validation(sample_verdict: Verdict) -> None:
    allowed = {
        "urn:li:mlModel:(urn:li:dataPlatform:mlflow,fraud_detector_v3,PROD)",
        "urn:li:dataset:(urn:li:dataPlatform:snowflake,staging.payments_clean,PROD)",
        "urn:li:mlFeature:(txn_aggregates,avg_txn_30d)",
    }

    valid_text = (
        "The model urn:li:mlModel:(urn:li:dataPlatform:mlflow,fraud_detector_v3,PROD) "
        "is blocked due to changes in "
        "urn:li:dataset:(urn:li:dataPlatform:snowflake,staging.payments_clean,PROD)."
    )
    assert validate_narrative_urns(valid_text, allowed) is True

    hallucinated_text = (
        "The model urn:li:mlModel:(urn:li:dataPlatform:mlflow,fraud_detector_v3,PROD) "
        "depends on hallucinated urn:li:dataset:(urn:li:dataPlatform:snowflake,unknown_table,PROD)."
    )
    assert validate_narrative_urns(hallucinated_text, allowed) is False


def test_generate_narrative_no_api_key(
    sample_verdict: Verdict, monkeypatch: pytest.MonkeyPatch
) -> None:
    # When no API key is provided, should render template
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    output = generate_narrative(sample_verdict, api_key=None)
    assert "Undertow ML Deploy Verdict: BLOCK" in output


def test_the_template_fallback_is_flagged_as_not_llm_written(
    sample_verdict: Verdict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Callers must be able to tell prose from a re-render of the same findings.

    Without this the PR comment prints every finding twice: once as a
    "Narrative Summary" and once as the structured report beneath it.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    text, used_llm = generate_narrative_detailed(sample_verdict, api_key=None)

    assert used_llm is False
    assert text == render_template(sample_verdict)


def test_generate_narrative_with_valid_mock_client(sample_verdict: Verdict) -> None:
    mock_client = MagicMock()
    mock_block = MagicMock()
    mock_block.text = (
        "Deployment blocked for model "
        "urn:li:mlModel:(urn:li:dataPlatform:mlflow,fraud_detector_v3,PROD) "
        "because column amount_usd was dropped in dataset "
        "urn:li:dataset:(urn:li:dataPlatform:snowflake,staging.payments_clean,PROD)."
    )
    mock_response = MagicMock()
    mock_response.content = [mock_block]
    mock_client.messages.create.return_value = mock_response

    output, used_llm = generate_narrative_detailed(sample_verdict, client=mock_client)
    assert "Deployment blocked for model" in output
    assert used_llm is True
    assert mock_client.messages.create.called


def test_generate_narrative_rejects_hallucinated_urn(sample_verdict: Verdict) -> None:
    mock_client = MagicMock()
    mock_block = MagicMock()
    mock_block.text = (
        "Deployment blocked because of foreign table "
        "urn:li:dataset:(urn:li:dataPlatform:snowflake,fake_table_123,PROD)."
    )
    mock_response = MagicMock()
    mock_response.content = [mock_block]
    mock_client.messages.create.return_value = mock_response

    # Should reject the output and fall back to Jinja2 template
    output = generate_narrative(sample_verdict, client=mock_client)
    assert "Undertow ML Deploy Verdict: BLOCK" in output
