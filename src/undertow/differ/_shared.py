"""Helpers shared by the three differs.

Private to the package. Kept here rather than duplicated because `short_urn`
appears in every summary string a user will read, and two differs disagreeing
about how a URN is abbreviated is exactly the kind of small incoherence that
makes a report look machine-generated.
"""

from __future__ import annotations

import math

_FABRICS = frozenset(
    {"PROD", "DEV", "QA", "UAT", "TEST", "STAGING", "CORP", "NON_PROD", "EI", "SANDBOX"}
)


def short_urn(urn: str) -> str:
    """`urn:li:dataset:(urn:li:dataPlatform:snowflake,raw.payments,PROD)` -> `raw.payments`.

    Reports are read in a PR comment; full URNs make them unreadable. Falls back
    to the URN untouched when the shape is unfamiliar, rather than guessing.

    The tuple shapes are not uniform, which is why this is not a one-liner:
    datasets and models are `(platform, name, fabric)`, `mlFeatureTable` is
    `(platform, name)` with no fabric, and `mlFeature` is `(namespace, name)`
    with no platform *and* no fabric. Dropping the platform (itself a URN) and
    then the trailing fabric leaves the name in every case.
    """
    if not urn.startswith("urn:li:"):
        return urn
    if "(" in urn and ")" in urn:
        body = urn[urn.find("(") + 1 : urn.rfind(")")]
    else:
        body = urn.rpartition(":")[2]

    parts = [p for p in body.split(",") if p and not p.startswith("urn:li:")]
    if not parts:
        return urn
    if len(parts) >= 2 and parts[-1] in _FABRICS:
        return parts[-2]
    return parts[-1]


def as_float(value: str | float | int | None) -> float | None:
    """Parse a DataHub statistic, which is stored as a string.

    `min`, `max`, `mean`, `median`, `stdev`, and `Quantile.value` are all typed
    `string` in the PDL so that non-numeric columns can be described with the
    same shape. Anything that is not a finite number is `None` — which the
    differs read as *cannot assess*, never as *no drift*.
    """
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def relative_change(old: float, new: float) -> float | None:
    """Signed fractional change, or `None` when the baseline is zero.

    `None` is not "no change" — a baseline of zero makes the ratio undefined,
    and callers have to decide what that means for their signal rather than
    dividing and getting an infinity into a JSON report.
    """
    if old == 0:
        return None
    return (new - old) / abs(old)


__all__ = ["as_float", "relative_change", "short_urn"]
