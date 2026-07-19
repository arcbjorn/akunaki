"""Fact and raw-revision ports.

Adapters implement these protocols. Domain and ports must not import
SQLAlchemy, so the application layer stays persistence-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from akunaki.domain.sleep_normalizer import SleepFact


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
