"""Fact and raw-revision ports.

Adapters implement these protocols. Domain and ports must not import
SQLAlchemy, so the application layer stays persistence-free.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from akunaki.domain.activity_normalizer import ActivityFact
from akunaki.domain.sleep_normalizer import SleepFact
from akunaki.domain.source_policy import DailySelectionSpec
from akunaki.domain.vitals_normalizer import VitalsFact
from akunaki.domain.workout_normalizer import WorkoutFact


@dataclass(frozen=True, slots=True)
class RevisionBody:
    """One immutable raw revision plus the exact body it points at."""

    revision_id: str
    connection_id: str | None
    raw_payload_id: str
    schema_version: str
    payload_text: str
    is_tombstone: bool


class RevisionReaderPort(Protocol):
    """Read immutable raw revisions and their transport bodies."""

    def get_revision(self, *, revision_id: str) -> RevisionBody | None:
        """Return the revision and its body, or None when unknown."""
        ...


class SleepProviderFactsPort(Protocol):
    """Read the competing sleep facts for a day, grouped by provider."""

    def sleep_facts_by_provider(
        self, *, tenant_id: str, local_health_day: str, is_nap: bool = False
    ) -> dict[str, list[str]]:
        """Current sleep fact ids on the day, keyed by the provider that supplied them.

        ``is_nap`` picks overnight sleep or daytime naps: they are separate
        metric families with different authoritative providers, so they are
        ranked separately.
        """
        ...


class SourceSelectionWriterPort(Protocol):
    """Persist the day's authoritative-source decision and its alternatives."""

    def record_daily_selection(
        self,
        *,
        selection_id: str,
        tenant_id: str,
        policy_version: str,
        spec: DailySelectionSpec,
        new_candidate_id: Callable[[], str],
        now: datetime,
    ) -> object:
        """Record a daily-metric decision, superseding any differing current row.

        Idempotent by the decision's content: recording the same winner, reason,
        and candidate set writes no new version.
        """
        ...


class FactWriteOutcomeLike(Protocol):
    """What one fact write persisted."""

    @property
    def is_new_version(self) -> bool:
        """True when a new fact version was appended."""
        ...


class FactWriterPort(Protocol):
    """Persist versioned facts and their typed detail rows."""

    def write_sleep_fact(
        self,
        *,
        fact_record_id: str,
        tenant_id: str,
        connection_id: str | None,
        fact: SleepFact,
        raw_revision_id: str | None,
        raw_payload_id: str | None,
        schema_version: str,
        now: datetime,
    ) -> FactWriteOutcomeLike:
        """Write one sleep fact, superseding any differing current version."""
        ...

    def write_vitals_fact(
        self,
        *,
        fact_record_id: str,
        tenant_id: str,
        connection_id: str | None,
        fact: VitalsFact,
        raw_revision_id: str | None,
        raw_payload_id: str | None,
        schema_version: str,
        now: datetime,
    ) -> FactWriteOutcomeLike:
        """Write one overnight-vitals fact, superseding any differing version."""
        ...

    def write_workout_fact(
        self,
        *,
        fact_record_id: str,
        tenant_id: str,
        connection_id: str | None,
        fact: WorkoutFact,
        raw_revision_id: str | None,
        raw_payload_id: str | None,
        schema_version: str,
        now: datetime,
    ) -> FactWriteOutcomeLike:
        """Write one workout fact, superseding any differing current version."""
        ...

    def write_activity_fact(
        self,
        *,
        fact_record_id: str,
        tenant_id: str,
        connection_id: str | None,
        fact: ActivityFact,
        raw_revision_id: str | None,
        raw_payload_id: str | None,
        schema_version: str,
        now: datetime,
    ) -> FactWriteOutcomeLike:
        """Write one daily-activity fact, superseding any differing version."""
        ...
