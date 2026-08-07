"""LLM Narrator for Undertow verdicts with Jinja2 template fallback."""

from __future__ import annotations

import os
import re
from typing import Any

import jinja2

from undertow.models import Verdict

_JINJA_TEMPLATE = """
### Undertow ML Deploy Verdict: {{ verdict.severity.value }}

**Headline:** {{ verdict.headline() }}

{% if verdict.blocking %}
#### 🔴 Blocking Findings ({{ verdict.blocking|length }})
{% for rf in verdict.blocking %}
- **{{ rf.finding.kind.value }}** on `{{ rf.finding.subject_column or rf.finding.subject_urn }}`
  - **Summary:** {{ rf.finding.summary }}
  {% if rf.finding.affected_feature_urn
  %}- **Affected Feature:** `{{ rf.finding.affected_feature_urn }}`{% endif %}
  {% if rf.finding.path and rf.finding.path.owners
  %}- **Owners:** {{ rf.finding.path.owners|join(', ') }}{% endif %}
  {% if rf.exempted_by
  %}- **Exemption:** {{ rf.exempted_by }}{% if rf.exemption_expires
  %} (expires {{ rf.exemption_expires }}){% endif %}{% endif %}
{% endfor %}
{% endif %}

{% if verdict.warnings %}
#### 🟡 Warning Findings ({{ verdict.warnings|length }})
{% for rf in verdict.warnings %}
- **{{ rf.finding.kind.value }}** on `{{ rf.finding.subject_column or rf.finding.subject_urn }}`
  - **Summary:** {{ rf.finding.summary }}
  {% if rf.finding.affected_feature_urn
  %}- **Affected Feature:** `{{ rf.finding.affected_feature_urn }}`{% endif %}
  {% if rf.finding.path and rf.finding.path.owners
  %}- **Owners:** {{ rf.finding.path.owners|join(', ') }}{% endif %}
{% endfor %}
{% endif %}

*Checked {{ verdict.assets_checked }} upstream assets.*
""".strip()

_URN_REGEX = re.compile(r"urn:li:[a-zA-Z0-9_:\-\(\)\.,]+")


def render_template(verdict: Verdict) -> str:
    """Render verdict as prose using Jinja2 template fallback."""
    env = jinja2.Environment(autoescape=False)
    template = env.from_string(_JINJA_TEMPLATE)
    return template.render(verdict=verdict)


def extract_input_urns(verdict: Verdict) -> set[str]:
    """Collect all valid URNs present in the input Verdict."""
    urns: set[str] = {verdict.model_urn}
    for rf in verdict.ruled_findings:
        urns.add(rf.finding.subject_urn)
        if rf.finding.affected_feature_urn:
            urns.add(rf.finding.affected_feature_urn)
        if rf.finding.path:
            for hop in rf.finding.path.hops:
                urns.add(hop.urn)
    return urns


def validate_narrative_urns(narrative: str, allowed_urns: set[str]) -> bool:
    """Validate that any URN mentioned in narrative exists in allowed_urns."""
    found_raw = set(_URN_REGEX.findall(narrative))
    for raw_urn in found_raw:
        clean_urn = raw_urn.rstrip(".,;:")
        if clean_urn not in allowed_urns:
            return False
    return True


def generate_narrative_detailed(
    verdict: Verdict,
    *,
    api_key: str | None = None,
    model: str = "claude-sonnet-4-6",
    client: Any = None,
) -> tuple[str, bool]:
    """Generate prose for a Verdict, and say whether an LLM actually wrote it.

    Callers need the flag because the fallback is a *rendering of the same
    findings the reporter already lays out*. Presenting it as a "narrative
    summary" alongside them prints everything twice, which reads as a bug in
    the report rather than as a graceful degradation.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    allowed_urns = extract_input_urns(verdict)

    if key or client is not None:
        try:
            if client is None:
                import anthropic

                client = anthropic.Anthropic(api_key=key)

            prompt = (
                "Explain the following Undertow ML Deploy Verdict in clear "
                "technical prose for a PR comment:\n"
                f"Verdict Severity: {verdict.severity.value}\n"
                f"Headline: {verdict.headline()}\n"
                f"Assets Checked: {verdict.assets_checked}\n"
                f"Ruled Findings JSON: {verdict.model_dump_json()}\n\n"
                f"CRITICAL CONSTRAINTS:\n"
                f"1. You MUST NOT change the verdict severity ({verdict.severity.value}).\n"
                f"2. You MUST NOT invent findings or statistics not in the input.\n"
                f"3. You MUST ONLY reference URNs from this allowed set: {sorted(allowed_urns)}.\n"
                f"4. Do NOT include any URNs outside that set."
            )

            response = client.messages.create(
                model=model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )

            output_text = ""
            if hasattr(response, "content") and response.content:
                for block in response.content:
                    if hasattr(block, "text"):
                        output_text += block.text

            if output_text and validate_narrative_urns(output_text, allowed_urns):
                return output_text.strip(), True
        except Exception:
            pass

    return render_template(verdict), False


def generate_narrative(
    verdict: Verdict,
    *,
    api_key: str | None = None,
    model: str = "claude-sonnet-4-6",
    client: Any = None,
) -> str:
    """Prose for a Verdict, falling back to the template when no LLM is available."""
    text, _ = generate_narrative_detailed(
        verdict, api_key=api_key, model=model, client=client
    )
    return text


# Alias for convenience
narrate = generate_narrative
