"""Tests for versioned daily source-selection persistence.

Cover recording a selected-provider decision with its candidates, the
missing-authoritative path, idempotence on an identical decision, supersession
on a changed decision, the one-current invariant, and the consistency guards.
Runs against a migrated database through the real repository.
"""

from __future__ import annotations

import itertools
from collections.abc import Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import FactRecord, SourceSelection, SourceSelectionCandidate, Tenant
from akunaki.adapters.db.source_selection_repository import (
    CandidateSpec,
    SelectionSpec,
    SourceSelectionRepository,
)
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339

T0 = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)
DAY = "2026-07-24"
POLICY = "source_policy_v0.1.0"

_IDS = itertools.count(1)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "source_selection.db"
    url = f"sqlite+libsql:///{db_path.resolve()}"
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", url)
    clear_settings_cache()
    cfg = Config(str(_backend_root() / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(_backend_root() / "alembic"))
    command.upgrade(cfg, "head")
    yield url
    clear_settings_cache()


@pytest.fixture
def factory(db_url: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=db_url))
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
    try:
        yield session_factory
    finally:
        engine.dispose()


def _seed_fact(factory: sessionmaker[Session], *, fact_id: str, provider: str) -> None:
    with factory() as session, session.begin():
        session.add(
            FactRecord(
                id=fact_id,
                tenant_id="tenant-1",
                connection_id=None,
                provider=provider,
                entity_type="sleep_session",
                vendor_record_id=fact_id,
                origin=None,
                method="wearable",
                utc_instant=NOW_S,
                start_utc=NOW_S,
                end_utc=NOW_S,
                source_offset_minutes=0,
                iana_timezone="UTC",
                local_health_day=DAY,
                unit=None,
                quality="high",
                confidence=1.0,
                freshness_at=NOW_S,
                raw_revision_id=None,
                raw_payload_id=None,
                schema_version="v1",
                normalizer_version="n",
                content_hash=fact_id,
                fact_key=f"sleep_session:{fact_id}",
                version_n=1,
                is_current=1,
                superseded_by=None,
                superseded_at=None,
                deletion_state="active",
                exclude_from_load=0,
                created_at=NOW_S,
            )
        )


def _new_id() -> str:
    return f"cand-{next(_IDS)}"


def _record(repo: SourceSelectionRepository, spec: SelectionSpec, *, sel_id: str):
    return repo.record_daily_selection(
        selection_id=sel_id,
        tenant_id="tenant-1",
        policy_version=POLICY,
        spec=spec,
        new_candidate_id=_new_id,
        now=T0,
    )


def test_records_a_selected_decision_with_candidates(
    factory: sessionmaker[Session],
) -> None:
    _seed_fact(factory, fact_id="oura-1", provider="oura")
    _seed_fact(factory, fact_id="google-1", provider="google_health")
    repo = SourceSelectionRepository(factory)

    spec = SelectionSpec(
        metric_family="sleep_session",
        local_health_day=DAY,
        selected_fact_record_id="oura-1",
        selection_reason="policy_match",
        missing_reason=None,
        candidates=(
            CandidateSpec(
                fact_record_id="oura-1",
                rank=0,
                eligibility="eligible",
                reason="authoritative_provider",
            ),
            CandidateSpec(
                fact_record_id="google-1",
                rank=1,
                eligibility="ineligible",
                reason="non_authoritative_provider",
            ),
        ),
    )
    written = _record(repo, spec, sel_id="sel-1")
    assert written.is_new_version is True
    assert written.version_n == 1

    with factory() as session:
        row = session.scalars(select(SourceSelection)).one()
        cands = session.scalars(select(SourceSelectionCandidate)).all()
    assert row.selected_fact_record_id == "oura-1"
    assert row.selection_reason == "policy_match"
    assert row.source_policy_version_id == POLICY
    assert row.grain_key == DAY
    # Both providers are recorded as candidates for the Why.
    assert len(cands) == 2


def test_missing_authoritative_has_no_fact(factory: sessionmaker[Session]) -> None:
    repo = SourceSelectionRepository(factory)
    spec = SelectionSpec(
        metric_family="sleep_session",
        local_health_day=DAY,
        selected_fact_record_id=None,
        selection_reason="missing_authoritative",
        missing_reason="no_fact_for_grain",
        candidates=(),
    )
    written = _record(repo, spec, sel_id="sel-1")
    assert written.is_new_version is True

    with factory() as session:
        row = session.scalars(select(SourceSelection)).one()
    assert row.selected_fact_record_id is None
    assert row.missing_reason == "no_fact_for_grain"


def test_identical_decision_is_idempotent(factory: sessionmaker[Session]) -> None:
    _seed_fact(factory, fact_id="oura-1", provider="oura")
    repo = SourceSelectionRepository(factory)
    spec = SelectionSpec(
        metric_family="sleep_session",
        local_health_day=DAY,
        selected_fact_record_id="oura-1",
        selection_reason="only_source",
        missing_reason=None,
        candidates=(
            CandidateSpec(
                fact_record_id="oura-1", rank=0, eligibility="eligible", reason="only_source"
            ),
        ),
    )
    _record(repo, spec, sel_id="sel-1")
    second = _record(repo, spec, sel_id="sel-2")
    assert second.is_new_version is False
    assert second.selection_id == "sel-1"

    with factory() as session:
        rows = session.scalars(select(SourceSelection)).all()
    assert len(rows) == 1  # no new version for an identical decision


def test_changed_decision_supersedes(factory: sessionmaker[Session]) -> None:
    _seed_fact(factory, fact_id="oura-1", provider="oura")
    _seed_fact(factory, fact_id="google-1", provider="google_health")
    repo = SourceSelectionRepository(factory)

    first = SelectionSpec(
        metric_family="sleep_session",
        local_health_day=DAY,
        selected_fact_record_id="google-1",
        selection_reason="only_source",
        missing_reason=None,
        candidates=(
            CandidateSpec(
                fact_record_id="google-1", rank=0, eligibility="eligible", reason="only_source"
            ),
        ),
    )
    _record(repo, first, sel_id="sel-1")
    # Oura arrives later and wins: a new current version supersedes the first.
    second = SelectionSpec(
        metric_family="sleep_session",
        local_health_day=DAY,
        selected_fact_record_id="oura-1",
        selection_reason="policy_match",
        missing_reason=None,
        candidates=(
            CandidateSpec(
                fact_record_id="oura-1",
                rank=0,
                eligibility="eligible",
                reason="authoritative_provider",
            ),
            CandidateSpec(
                fact_record_id="google-1",
                rank=1,
                eligibility="ineligible",
                reason="non_authoritative_provider",
            ),
        ),
    )
    written = _record(repo, second, sel_id="sel-2")
    assert written.version_n == 2

    with factory() as session:
        rows = session.scalars(select(SourceSelection).order_by(SourceSelection.version_n)).all()
    assert len(rows) == 2
    assert rows[0].is_current == 0
    assert rows[0].superseded_by == "sel-2"
    assert rows[1].is_current == 1
    # Exactly one current row for the grain (the partial unique index holds).
    current = SourceSelectionRepository(factory).current_selection(
        tenant_id="tenant-1", metric_family="sleep_session", local_health_day=DAY
    )
    assert current is not None
    assert current.selected_fact_record_id == "oura-1"


def test_inconsistent_missing_reason_is_rejected(
    factory: sessionmaker[Session],
) -> None:
    repo = SourceSelectionRepository(factory)
    # missing_authoritative with a selected fact is a contradiction.
    bad = SelectionSpec(
        metric_family="sleep_session",
        local_health_day=DAY,
        selected_fact_record_id="oura-1",
        selection_reason="missing_authoritative",
        missing_reason="no_fact_for_grain",
        candidates=(),
    )
    with pytest.raises(ValueError, match="missing_authoritative requires"):
        _record(repo, bad, sel_id="sel-1")

    # A non-missing reason without a fact is equally rejected.
    bad2 = SelectionSpec(
        metric_family="sleep_session",
        local_health_day=DAY,
        selected_fact_record_id=None,
        selection_reason="policy_match",
        missing_reason=None,
        candidates=(),
    )
    with pytest.raises(ValueError, match="requires a selected fact"):
        _record(repo, bad2, sel_id="sel-2")
