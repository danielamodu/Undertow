"""Read a model's verdict history back out of DataHub.

Undertow keeps no database. Every `--write-back` run appends an
`assertionRunEvent` to a native DataHub assertion, and because that aspect is a
*timeseries* one, successive runs accumulate rather than overwrite. The history
of a model's gate is therefore already in the catalog — it just needs reading.

That is the point worth making. A CI runner wiped between builds still knows
what the last twenty deploys of this model looked like, because the record lives
where the lineage lives rather than in a sidecar store this project would have
had to invent, secure, and back up.

The assertion URN is derived, not stored: `datahub_writer` mints it from a GUID
over `{model_urn, scope}`, so the same model always resolves to the same
assertion and its run events append to one series instead of scattering.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import datahub.emitter.mce_builder as builder
from datahub.metadata.schema_classes import AssertionRunEventClass

ASSERTION_SCOPE = "undertow_verdict"


def assertion_urn_for(model_urn: str) -> str:
    """The assertion Undertow writes verdicts to for this model.

    Must stay in step with `datahub_writer.create_verdict_mcps`; a mismatch
    would silently start a fresh series and orphan the existing history rather
    than fail, which is why `test_history.py` pins the two together.
    """
    guid = builder.datahub_guid({"model_urn": model_urn, "scope": ASSERTION_SCOPE})
    return builder.make_assertion_urn(guid)


@dataclass(frozen=True)
class VerdictRun:
    """One recorded gate run."""

    checked_at: datetime
    result: str
    severity: str
    blocking: int
    warning: int
    assets_checked: int
    run_id: str

    @property
    def passed(self) -> bool:
        return self.result != "FAILURE"

    def exit_code(self) -> int:
        """What this run told CI at the time."""
        return 1 if self.severity == "BLOCK" else 0


def _int(values: dict[str, Any], key: str) -> int:
    try:
        return int(values.get(key, 0))
    except (TypeError, ValueError):
        return 0


def _to_run(event: Any) -> VerdictRun | None:
    result = getattr(event, "result", None)
    if result is None:
        return None

    # `nativeResults` is a str->str map, so everything comes back stringified.
    native: dict[str, Any] = getattr(result, "nativeResults", None) or {}
    millis = getattr(event, "timestampMillis", 0) or 0

    return VerdictRun(
        checked_at=datetime.fromtimestamp(millis / 1000, tz=UTC),
        result=str(getattr(result, "type", "") or ""),
        severity=str(native.get("severity", "")),
        blocking=_int(native, "blocking_count"),
        warning=_int(native, "warning_count"),
        assets_checked=_int(native, "assets_checked"),
        run_id=str(getattr(event, "runId", "") or ""),
    )


def read_history(
    model_urn: str,
    *,
    graph: Any,
    limit: int = 20,
) -> list[VerdictRun]:
    """Every recorded verdict for `model_urn`, newest first.

    An empty list means no run has been written back yet — not that every run
    passed. The caller has to keep those apart, so this raises on a query
    failure rather than returning nothing.
    """
    events = graph.get_timeseries_values(
        entity_urn=assertion_urn_for(model_urn),
        aspect_type=AssertionRunEventClass,
        filter={},
        limit=limit,
    )

    runs = [run for run in (_to_run(event) for event in events) if run is not None]
    return sorted(runs, key=lambda r: r.checked_at, reverse=True)


def summarise(runs: list[VerdictRun]) -> str:
    """One line on what the history shows, for the bottom of a report."""
    if not runs:
        return "No recorded runs. Verdicts are written by `undertow check --write-back`."

    blocked = sum(1 for r in runs if r.severity == "BLOCK")
    span = ""
    if len(runs) > 1:
        days = (runs[0].checked_at - runs[-1].checked_at).days
        span = f" over {days} day(s)" if days else " today"

    return f"{len(runs)} run(s){span}, {blocked} blocked."


__all__ = ["VerdictRun", "assertion_urn_for", "read_history", "summarise"]
