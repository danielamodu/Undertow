"""Undertow investigator — the agent loop that enriches findings with context.

Structurally incapable of changing a verdict: it consumes and produces
`Finding`, which carries no severity. See `investigator.py` for the reasoning.
"""

from undertow.investigator.investigator import (
    INVESTIGATION_TOOLS,
    InvestigationUnavailable,
    investigate_findings,
)

__all__ = ["investigate_findings", "InvestigationUnavailable", "INVESTIGATION_TOOLS"]
