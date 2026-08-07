"""Undertow investigator — the agent loop that enriches findings with context.

Structurally incapable of changing a verdict: it consumes and produces
`Finding`, which carries no severity. See `investigator.py` for the reasoning.
"""

from undertow.investigator.investigator import (
    INVESTIGATION_TOOLS,
    InvestigationUnavailable,
    investigate_findings,
)
from undertow.investigator.investigator import (
    # Qualified at the package boundary: `unavailable_reason` is unambiguous
    # inside this module and says nothing about what at the call site.
    unavailable_reason as investigation_unavailable_reason,
)

__all__ = [
    "investigate_findings",
    "investigation_unavailable_reason",
    "InvestigationUnavailable",
    "INVESTIGATION_TOOLS",
]
