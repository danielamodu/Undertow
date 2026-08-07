"""Undertow — lineage-grounded pre-deploy gate for production ML.

The surface looks fine. The model keeps serving predictions. Underneath,
something changed upstream days ago and nobody connected the two.

Undertow walks DataHub's graph from an mlModel to its upstream data, diffs that
footprint against the last approved deploy, and returns a verdict with the
lineage path that explains it.
"""

from undertow.models import (
    AssetSnapshot,
    AttributionHop,
    AttributionPath,
    ColumnSnapshot,
    Confidence,
    FieldProfileSnapshot,
    Finding,
    FindingKind,
    HistogramSnapshot,
    ProfileSnapshot,
    QuantileSnapshot,
    RuledFinding,
    Severity,
    UndertowSnapshot,
    ValueFrequencySnapshot,
    Verdict,
)
from undertow.policy import Exemption, Policy, Thresholds

__version__ = "0.1.0"

__all__ = [
    "AssetSnapshot",
    "AttributionHop",
    "AttributionPath",
    "ColumnSnapshot",
    "Confidence",
    "Exemption",
    "FieldProfileSnapshot",
    "Finding",
    "FindingKind",
    "HistogramSnapshot",
    "Policy",
    "ProfileSnapshot",
    "QuantileSnapshot",
    "RuledFinding",
    "Severity",
    "Thresholds",
    "UndertowSnapshot",
    "ValueFrequencySnapshot",
    "Verdict",
    "__version__",
]
