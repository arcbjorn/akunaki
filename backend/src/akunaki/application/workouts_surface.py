"""The read-only ``/v1/workouts`` surface.

Workout facts have been written since the Polar connector shipped, but the only
thing that ever read them back was the load aggregate feeding ACWR — a single
number per day. The sessions themselves, with their zone minutes, were not
readable at all.

What is disclosed is what was **measured** (start/end, per-zone minutes) plus
the canonical ``session_load`` this system computes internally from those
minutes. There is no "workout score": v0.1.0 ships exactly one score code
(recovery), and inventing a second here would imply an accepted formula that
does not exist.

Duplicates are excluded. A workout covered by two providers is one real
session; the second provider's copy carries ``exclude_from_load = 1`` and is
omitted, so the user is never shown the same session twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "WorkoutPage",
    "WorkoutSessionSource",
    "WorkoutSummary",
    "WorkoutsSurfaceService",
]

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class WorkoutSummary:
    """One canonical workout session, as disclosed to the caller."""

    workout_id: str
    provider: str
    local_health_day: str
    start_utc: str
    end_utc: str
    session_load: float
    zone1_min: float
    zone2_min: float
    zone3_min: float
    zone4_min: float
    zone5_min: float

    @property
    def total_zone_min(self) -> float:
        """Minutes across all five heart-rate zones."""
        return self.zone1_min + self.zone2_min + self.zone3_min + self.zone4_min + self.zone5_min


@dataclass(frozen=True, slots=True)
class WorkoutPage:
    """One page of workouts plus the cursor for the next."""

    items: tuple[WorkoutSummary, ...]
    next_cursor: str | None


class WorkoutSessionSource(Protocol):
    """Port: paginated reads over a tenant's canonical workout sessions."""

    def list_workouts(
        self,
        *,
        tenant_id: str,
        limit: int,
        cursor: str | None = ...,
    ) -> tuple[list[WorkoutSummary], str | None]:
        """Return one page (newest first) and the cursor for the next."""
        ...

    def get_workout(self, *, tenant_id: str, workout_id: str) -> WorkoutSummary | None:
        """Return one workout the tenant owns, or None."""
        ...


class WorkoutsSurfaceService:
    """Build the workout list and detail views for a tenant."""

    def __init__(self, *, workouts: WorkoutSessionSource) -> None:
        self._workouts = workouts

    def list_for_tenant(
        self,
        *,
        tenant_id: str,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> WorkoutPage:
        """One page of the tenant's workouts, newest first.

        An empty page is a real answer: a user with no workout connector, or a
        genuinely rested period, has no sessions.
        """
        bounded = max(1, min(limit, MAX_PAGE_SIZE))
        items, next_cursor = self._workouts.list_workouts(
            tenant_id=tenant_id,
            limit=bounded,
            cursor=cursor,
        )
        return WorkoutPage(items=tuple(items), next_cursor=next_cursor)

    def get_for_tenant(self, *, tenant_id: str, workout_id: str) -> WorkoutSummary | None:
        """One workout, or None when it is unknown **or** another tenant's.

        The two cases are deliberately indistinguishable to the caller, so an
        id cannot be probed across tenants.
        """
        return self._workouts.get_workout(tenant_id=tenant_id, workout_id=workout_id)
