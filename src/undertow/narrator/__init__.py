"""Narrator package for Undertow.

Provides bounded LLM narrative generation for verdicts with Jinja2 template fallback.
"""

from undertow.narrator.narrator import (
    generate_narrative,
    generate_narrative_detailed,
    narrate,
    render_template,
    validate_narrative_urns,
)

__all__ = [
    "generate_narrative",
    "generate_narrative_detailed",
    "narrate",
    "render_template",
    "validate_narrative_urns",
]
