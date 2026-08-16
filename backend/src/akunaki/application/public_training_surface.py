"""The unauthenticated ``/v1/public/training`` surface: a 30-day training calendar.

Every other surface in this package answers a logged-in caller about their own
data. This one answers *anyone* about one operator-named tenant, so what it
discloses is cut down to the bone: for each of the last 30 local days, whether
at least one workout session was recorded — nothing else. No times, no zone
minutes, no loads, no scores, no vitals. The streaks are arithmetic over those
booleans and disclose nothing further.

The window ends on the tenant's local **today**, computed from the tenant's
stated ``primary_timezone`` — the one case where the server may pick a day
itself, because there is no caller to ask. Facts already carry the local day
they were assigned at normalization; the timezone only decides where the
window ends.

Pure: the service does no I/O beyond the injected source and takes ``today``
as an argument, so the same facts and the same day always yield the same
calendar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

__all__ = [
    "WINDOW_DAYS",
    "PublicTrainingCalendar",
    "PublicTrainingDay",
    "PublicTrainingSource",
    "PublicTrainingSurfaceService",
    "current_streak",
    "longest_streak",
]

# Fixed, not a query parameter: the disclosure window is an operator decision
# made once, and a single URL keeps the response cacheable at the edge.
WINDOW_DAYS = 30

# What makes a day count. Named so the response can state it verbatim.
DAY_DEFINITION = "at least one workout session recorded on that local day"


@dataclass(frozen=True, slots=True)
class PublicTrainingDay:
    """One local day: trained or not. Nothing else is disclosed."""

    local_health_day: str
    trained: bool


@dataclass(frozen=True, slots=True)
class PublicTrainingCalendar:
    """The public calendar plus the streaks derived from it."""

    as_of: str
    """The tenant's local today; the last day of the window."""

    window_days: int
    days: tuple[PublicTrainingDay, ...]
    """Oldest first, exactly ``window_days`` entries."""

    days_trained: int
    current_streak: int
    longest_streak: int
    sources: tuple[str, ...]
    """Providers that recorded a session inside the window, sorted."""

    definition: str


class PublicTrainingSource(Protocol):
    """Port: which local days carry a workout session, and from which providers."""

    def workout_days(
        self,
        *,
        tenant_id: str,
        local_health_days: list[str],
    ) -> dict[str, tuple[str, ...]]:
        """Trained days in the given set, each with its sorted providers."""
        ...


def current_streak(trained: tuple[bool, ...]) -> int:
    """Consecutive trained days ending today, or ending yesterday if today is not.

    Today is the last element. A day still in progress must not break a
    streak — the session may simply not have happened yet — so an untrained
    today is skipped and the count runs from yesterday. An untrained yesterday
    ends it at zero.
    """
    if not trained:
        return 0
    days = list(trained)
    if not days[-1]:
        days.pop()
    count = 0
    for flag in reversed(days):
        if not flag:
            break
        count += 1
    return count


def longest_streak(trained: tuple[bool, ...]) -> int:
    """The longest run of consecutive trained days within the window."""
    best = 0
    run = 0
    for flag in trained:
        run = run + 1 if flag else 0
        best = max(best, run)
    return best


class PublicTrainingSurfaceService:
    """Build the public 30-day calendar for one tenant."""

    def __init__(self, *, source: PublicTrainingSource) -> None:
        self._source = source

    def calendar(self, *, tenant_id: str, today: date) -> PublicTrainingCalendar:
        """The window ending on ``today``, oldest day first."""
        window = [
            (today - timedelta(days=offset)).isoformat()
            for offset in range(WINDOW_DAYS - 1, -1, -1)
        ]
        by_day = self._source.workout_days(tenant_id=tenant_id, local_health_days=window)
        days = tuple(
            PublicTrainingDay(local_health_day=day, trained=day in by_day) for day in window
        )
        flags = tuple(entry.trained for entry in days)
        sources = sorted({provider for providers in by_day.values() for provider in providers})
        return PublicTrainingCalendar(
            as_of=today.isoformat(),
            window_days=WINDOW_DAYS,
            days=days,
            days_trained=sum(flags),
            current_streak=current_streak(flags),
            longest_streak=longest_streak(flags),
            sources=tuple(sources),
            definition=DAY_DEFINITION,
        )
