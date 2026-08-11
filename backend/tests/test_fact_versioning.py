"""Fact versioning: supersede, never update in place.

The engine reads current facts, and provenance depends on prior versions
remaining readable, so these are the invariants that make a fact history
trustworthy.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.adapters.db.models import (
    Connection,
    DailyActivity,
    FactRecord,
    SleepSession,
    Tenant,
)
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.activity_normalizer import ENTITY_TYPE as ACTIVITY_ENTITY_TYPE
from akunaki.domain.activity_normalizer import ActivityFact, normalize_activity_payload
from akunaki.domain.jobs import to_utc_rfc3339
from akunaki.domain.sleep_normalizer import (
    ENTITY_TYPE,
    NORMALIZER_VERSION,
    SleepFact,
    normalize_sleep_payload,
)

T0 = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
ConstraintError = (IntegrityError, ValueError)

_IDS = itertools.count(1)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def fact_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "facts.db"
    url = f"sqlite+libsql:///{db_path.resolve()}"
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", url)
    clear_settings_cache()
    cfg = Config(str(_backend_root() / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(_backend_root() / "src" / "akunaki" / "migrations"))
    command.upgrade(cfg, "head")
    yield url
    clear_settings_cache()


@pytest.fixture
def factory(fact_db: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=fact_db))
    session_factory = create_session_factory(engine)
    with session_factory() as session, session.begin():
        session.add(
            Tenant(
                id="tenant-1",
                created_at=NOW_S,
                status="active",
                primary_timezone="UTC",
                display_name="Test",
            )
        )
        session.add(
            Connection(
                id="conn-1",
                tenant_id="tenant-1",
                provider="oura",
                status="active",
                scopes_granted_json='["daily"]',
                connected_at=NOW_S,
                updated_at=NOW_S,
            )
        )
    try:
        yield session_factory
    finally:
        engine.dispose()


def _fact(**overrides: object) -> SleepFact:
    record: dict[str, object] = {
        "id": "sleep-1",
        "bedtime_start": "2026-07-18T23:10:00+02:00",
        "bedtime_end": "2026-07-19T07:20:00+02:00",
        "total_sleep_duration": 27000,
        "time_in_bed": 29400,
        "light_sleep_duration": 15000,
        "deep_sleep_duration": 6000,
        "rem_sleep_duration": 6000,
        "awake_time": 2400,
        "efficiency": 92,
        "type": "long_sleep",
    }
    record.update(overrides)
    [fact] = normalize_sleep_payload(json.dumps({"data": [record]}))
    return fact


def _write(
    factory: sessionmaker[Session],
    fact: SleepFact,
    *,
    now: datetime = T0,
) -> object:
    return FactRepository(factory).write_sleep_fact(
        fact_record_id=f"fact-{next(_IDS)}",
        tenant_id="tenant-1",
        connection_id="conn-1",
        fact=fact,
        raw_revision_id=None,
        raw_payload_id=None,
        schema_version="oura.v2",
        now=now,
    )


# ---------------------------------------------------------------------------
# First write
# ---------------------------------------------------------------------------


def test_first_write_creates_version_one(factory: sessionmaker[Session]) -> None:
    outcome = _write(factory, _fact())

    assert outcome.version_n == 1  # type: ignore[attr-defined]
    assert outcome.is_new_version is True  # type: ignore[attr-defined]

    with factory() as session:
        record = session.scalars(select(FactRecord)).one()
        detail = session.scalars(select(SleepSession)).one()

    assert record.is_current == 1
    assert record.entity_type == ENTITY_TYPE
    assert record.normalizer_version == NORMALIZER_VERSION
    assert record.local_health_day == "2026-07-19"
    assert detail.duration_min == 450.0


def test_detail_row_is_one_to_one_with_header(factory: sessionmaker[Session]) -> None:
    outcome = _write(factory, _fact())

    with factory() as session:
        detail = session.get(SleepSession, outcome.fact_record_id)  # type: ignore[attr-defined]
        assert detail is not None
        assert detail.fact_record_id == outcome.fact_record_id  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Re-normalization
# ---------------------------------------------------------------------------


def test_identical_content_writes_no_new_version(factory: sessionmaker[Session]) -> None:
    """Re-running the normalizer over unchanged raw data is a no-op."""
    first = _write(factory, _fact())
    second = _write(factory, _fact())

    assert first.version_n == 1  # type: ignore[attr-defined]
    assert second.is_new_version is False  # type: ignore[attr-defined]
    assert second.fact_record_id == first.fact_record_id  # type: ignore[attr-defined]

    with factory() as session:
        assert len(session.scalars(select(FactRecord)).all()) == 1


def test_changed_values_supersede_rather_than_update(
    factory: sessionmaker[Session],
) -> None:
    """A corrected night appends a version; the original stays readable."""
    first = _write(factory, _fact())
    later = T0 + timedelta(days=1)
    second = _write(factory, _fact(total_sleep_duration=28800), now=later)

    assert second.version_n == 2  # type: ignore[attr-defined]
    assert second.superseded_id == first.fact_record_id  # type: ignore[attr-defined]

    with factory() as session:
        records = session.scalars(select(FactRecord).order_by(FactRecord.version_n)).all()

    assert len(records) == 2
    old, new = records
    # The original is retained, not overwritten.
    assert old.is_current == 0
    assert old.superseded_by == new.id
    assert old.superseded_at == to_utc_rfc3339(later)
    assert new.is_current == 1
    assert new.superseded_by is None


def test_only_one_current_version_exists(factory: sessionmaker[Session]) -> None:
    _write(factory, _fact())
    _write(factory, _fact(total_sleep_duration=28800))
    _write(factory, _fact(total_sleep_duration=30000))

    with factory() as session:
        current = session.scalars(select(FactRecord).where(FactRecord.is_current == 1)).all()
        allv = session.scalars(select(FactRecord)).all()

    assert len(current) == 1
    assert len(allv) == 3
    assert current[0].version_n == 3


def test_prior_detail_rows_survive_superseding(factory: sessionmaker[Session]) -> None:
    """Provenance needs the old measurement values, not just the header."""
    first = _write(factory, _fact())
    _write(factory, _fact(total_sleep_duration=28800))

    with factory() as session:
        old_detail = session.get(SleepSession, first.fact_record_id)  # type: ignore[attr-defined]
        assert old_detail is not None
        assert old_detail.duration_min == 450.0
        assert len(session.scalars(select(SleepSession)).all()) == 2


def test_distinct_sessions_version_independently(factory: sessionmaker[Session]) -> None:
    _write(factory, _fact(id="sleep-1"))
    _write(factory, _fact(id="sleep-2", total_sleep_duration=20000))
    _write(factory, _fact(id="sleep-1", total_sleep_duration=28800))

    with factory() as session:
        current = session.scalars(select(FactRecord).where(FactRecord.is_current == 1)).all()

    by_key = {r.fact_key: r.version_n for r in current}
    assert by_key == {f"{ENTITY_TYPE}:sleep-1": 2, f"{ENTITY_TYPE}:sleep-2": 1}


# ---------------------------------------------------------------------------
# Schema-enforced invariants
# ---------------------------------------------------------------------------


def test_two_current_versions_are_rejected_by_the_index(
    factory: sessionmaker[Session],
) -> None:
    """The partial unique index is the backstop for the versioning logic."""
    _write(factory, _fact())

    with pytest.raises(ConstraintError), factory() as session, session.begin():
        session.add(
            FactRecord(
                id="fact-rogue",
                tenant_id="tenant-1",
                connection_id="conn-1",
                provider="oura",
                entity_type=ENTITY_TYPE,
                method="wearable",
                quality="high",
                confidence=0.9,
                schema_version="oura.v2",
                normalizer_version=NORMALIZER_VERSION,
                fact_key=f"{ENTITY_TYPE}:sleep-1",
                version_n=99,
                is_current=1,
                deletion_state="active",
                exclude_from_load=0,
                created_at=NOW_S,
            )
        )


def test_superseded_row_must_not_be_current(factory: sessionmaker[Session]) -> None:
    with pytest.raises(ConstraintError), factory() as session, session.begin():
        session.add(
            FactRecord(
                id="fact-bad",
                tenant_id="tenant-1",
                provider="oura",
                entity_type=ENTITY_TYPE,
                method="wearable",
                quality="high",
                confidence=0.9,
                schema_version="oura.v2",
                normalizer_version=NORMALIZER_VERSION,
                fact_key="k",
                version_n=1,
                is_current=1,
                superseded_by="other",
                superseded_at=NOW_S,
                deletion_state="active",
                exclude_from_load=0,
                created_at=NOW_S,
            )
        )


def test_confidence_must_be_a_probability(factory: sessionmaker[Session]) -> None:
    with pytest.raises(ConstraintError), factory() as session, session.begin():
        session.add(
            FactRecord(
                id="fact-bad2",
                tenant_id="tenant-1",
                provider="oura",
                entity_type=ENTITY_TYPE,
                method="wearable",
                quality="high",
                confidence=1.5,
                schema_version="oura.v2",
                normalizer_version=NORMALIZER_VERSION,
                fact_key="k2",
                version_n=1,
                is_current=1,
                deletion_state="active",
                exclude_from_load=0,
                created_at=NOW_S,
            )
        )


def test_current_facts_query_returns_only_current(
    factory: sessionmaker[Session],
) -> None:
    _write(factory, _fact())
    _write(factory, _fact(total_sleep_duration=28800))

    ids = FactRepository(factory).current_sleep_facts(
        tenant_id="tenant-1", local_health_day="2026-07-19"
    )
    assert len(ids) == 1


def test_same_vendor_id_across_tenants_does_not_collide(
    factory: sessionmaker[Session],
) -> None:
    """A vendor record id is unique only *within* a tenant.

    Regression: the one-current index was originally keyed on ``fact_key``
    alone, so a second tenant's fact was treated as a new version of the
    first tenant's and its data was never stored.
    """
    with factory() as session, session.begin():
        session.add(
            Tenant(
                id="tenant-2",
                created_at=NOW_S,
                status="active",
                primary_timezone="UTC",
                display_name="Other",
            )
        )

    fact = _fact()
    repository = FactRepository(factory)
    for tenant_id in ("tenant-1", "tenant-2"):
        repository.write_sleep_fact(
            fact_record_id=f"fact-{tenant_id}",
            tenant_id=tenant_id,
            connection_id=None,
            fact=fact,
            raw_revision_id=None,
            raw_payload_id=None,
            schema_version="oura.v2",
            now=T0,
        )

    with factory() as session:
        records = session.scalars(select(FactRecord)).all()

    # Two independent facts, each current for its own tenant.
    assert len(records) == 2
    assert {r.tenant_id for r in records} == {"tenant-1", "tenant-2"}
    assert all(r.is_current == 1 for r in records)
    assert all(r.version_n == 1 for r in records)


# ---------------------------------------------------------------------------
# Daily-activity facts
#
# The activity write path had **no** database coverage: `_normalize_activity`
# is tested against fake ports, so the dispatch was pinned while ~80 statements
# of supersede/versioning logic had never run against real SQL.
# ---------------------------------------------------------------------------


def _activity_fact(**overrides: object) -> ActivityFact:
    point: dict[str, object] = {
        "startTime": "2026-07-18T00:00:00+02:00",
        "endTime": "2026-07-19T00:00:00+02:00",
        "steps": 8500,
        "activeMinutes": 42.5,
    }
    point.update(overrides)
    [fact] = normalize_activity_payload(json.dumps({"dataPoints": [point]}))
    return fact


def _write_activity(
    factory: sessionmaker[Session],
    fact: ActivityFact,
    *,
    now: datetime = T0,
) -> object:
    return FactRepository(factory).write_activity_fact(
        fact_record_id=f"act-{next(_IDS)}",
        tenant_id="tenant-1",
        connection_id="conn-1",
        fact=fact,
        raw_revision_id=None,
        raw_payload_id=None,
        schema_version="google_activity.v1",
        now=now,
    )


def _activity_rows(factory: sessionmaker[Session]) -> list[FactRecord]:
    with factory() as session:
        return list(
            session.scalars(
                select(FactRecord)
                .where(FactRecord.entity_type == ACTIVITY_ENTITY_TYPE)
                .order_by(FactRecord.version_n)
            ).all()
        )


def test_a_first_activity_fact_is_written_current(factory: sessionmaker[Session]) -> None:
    outcome = _write_activity(factory, _activity_fact())

    assert outcome.is_new_version is True
    [row] = _activity_rows(factory)
    assert row.version_n == 1
    assert row.is_current == 1
    assert row.superseded_by is None


def test_the_activity_detail_row_carries_the_totals(
    factory: sessionmaker[Session],
) -> None:
    _write_activity(factory, _activity_fact())

    with factory() as session:
        [detail] = session.scalars(select(DailyActivity)).all()

    assert detail.steps == 8500
    assert detail.active_minutes == 42.5


def test_an_identical_activity_fact_does_not_version(
    factory: sessionmaker[Session],
) -> None:
    """Content-hash dedupe: a re-fetched day must not grow the history."""
    _write_activity(factory, _activity_fact())

    outcome = _write_activity(factory, _activity_fact())

    assert outcome.is_new_version is False
    assert len(_activity_rows(factory)) == 1


def test_a_changed_activity_fact_supersedes_rather_than_updates(
    factory: sessionmaker[Session],
) -> None:
    """A vendor revising the day writes v2; v1 stays readable for provenance."""
    _write_activity(factory, _activity_fact())

    outcome = _write_activity(factory, _activity_fact(steps=9100), now=T0 + timedelta(hours=1))

    assert outcome.is_new_version is True
    first, second = _activity_rows(factory)
    assert first.version_n == 1
    assert first.is_current == 0
    assert first.superseded_by == second.id
    assert second.version_n == 2
    assert second.is_current == 1


def test_only_one_activity_version_is_current(factory: sessionmaker[Session]) -> None:
    """The engine reads current facts; two would make a day ambiguous."""
    _write_activity(factory, _activity_fact())
    _write_activity(factory, _activity_fact(steps=9100), now=T0 + timedelta(hours=1))
    _write_activity(factory, _activity_fact(steps=9700), now=T0 + timedelta(hours=2))

    rows = _activity_rows(factory)

    assert [row.version_n for row in rows] == [1, 2, 3]
    assert sum(row.is_current for row in rows) == 1
    assert rows[-1].is_current == 1


def test_superseding_preserves_the_prior_detail_row(
    factory: sessionmaker[Session],
) -> None:
    """Supersede, never update in place: the old totals stay exactly as read."""
    _write_activity(factory, _activity_fact())
    _write_activity(factory, _activity_fact(steps=9100), now=T0 + timedelta(hours=1))

    with factory() as session:
        details = session.scalars(
            select(DailyActivity).join(FactRecord).order_by(FactRecord.version_n)
        ).all()

    assert [detail.steps for detail in details] == [8500, 9100]


def test_activity_facts_are_tenant_scoped(factory: sessionmaker[Session]) -> None:
    """The same vendor day for another tenant is a separate history."""
    _write_activity(factory, _activity_fact())
    with factory() as session, session.begin():
        session.add(
            Tenant(
                id="tenant-2",
                created_at=NOW_S,
                status="active",
                primary_timezone="UTC",
                display_name="Other",
            )
        )

    FactRepository(factory).write_activity_fact(
        fact_record_id="act-other",
        tenant_id="tenant-2",
        connection_id=None,
        fact=_activity_fact(),
        raw_revision_id=None,
        raw_payload_id=None,
        schema_version="google_activity.v1",
        now=T0,
    )

    rows = _activity_rows(factory)
    assert len(rows) == 2
    assert {row.tenant_id for row in rows} == {"tenant-1", "tenant-2"}
    # Both are current: they are different tenants' facts, not versions.
    assert all(row.is_current == 1 for row in rows)


def test_an_empty_tenant_id_is_rejected(factory: sessionmaker[Session]) -> None:
    with pytest.raises(ValueError, match="must be non-empty"):
        FactRepository(factory).write_activity_fact(
            fact_record_id="act-x",
            tenant_id="",
            connection_id=None,
            fact=_activity_fact(),
            raw_revision_id=None,
            raw_payload_id=None,
            schema_version="google_activity.v1",
            now=T0,
        )
