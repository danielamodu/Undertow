"""Suggests policy adjustments from a model's own recorded history.

Advisory only, in the same sense the investigator is: this reads history that
already exists and was already correctly recorded, and proposes a change a
human decides whether to make. Nothing here writes to `undertow.yaml`, edits a
baseline, or touches how a verdict gets computed — a `Suggestion` cannot
reach the policy engine, the same way a `Finding`'s `evidence` cannot.

The signal: a finding kind that warns on most of a model's recent runs is
either a real, ongoing problem nobody has fixed, or a threshold that no
longer fits this asset. Either way it is worth a human decision, not another
identical WARN box next week.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from undertow.history import VerdictRun

# Below this many recorded runs, any pattern is coincidence, not a trend.
_MIN_RUNS_FOR_A_SUGGESTION = 4
# A finding kind warning on at least this fraction of recent runs is chronic
# rather than occasional — the line between "flaky, keep watching" and
# "someone should look at this policy."
_CHRONIC_WARN_RATE = 0.5


@dataclass(frozen=True)
class Suggestion:
    finding_kind: str
    warn_count: int
    total_runs: int

    @property
    def rate(self) -> float:
        return self.warn_count / self.total_runs if self.total_runs else 0.0


def suggest(runs: list[VerdictRun]) -> list[Suggestion]:
    """Finding kinds that have warned often enough across `runs` to be worth
    a policy review, most-frequent first. Empty if there isn't enough history
    yet, or nothing recurs often enough to say anything about.
    """
    if len(runs) < _MIN_RUNS_FOR_A_SUGGESTION:
        return []

    counts: Counter[str] = Counter()
    for run in runs:
        counts.update(set(run.warning_kinds))

    total = len(runs)
    suggestions = [
        Suggestion(finding_kind=kind, warn_count=count, total_runs=total)
        for kind, count in counts.items()
        if count / total >= _CHRONIC_WARN_RATE
    ]
    return sorted(suggestions, key=lambda s: (-s.rate, s.finding_kind))


def format_suggestions(suggestions: list[Suggestion]) -> list[str]:
    return [
        f"{s.finding_kind} warned on {s.warn_count}/{s.total_runs} recorded runs "
        f"({s.rate:.0%}) — consider raising its threshold in undertow.yaml, or "
        "confirming it's actually being reviewed each time it fires."
        for s in suggestions
    ]


__all__ = ["Suggestion", "suggest", "format_suggestions"]
