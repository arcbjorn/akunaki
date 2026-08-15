"""Tests for derivation-run persistence and opaque token resolution.

Cover the create-run/resolve round trip, tenant isolation (a token cannot read
another tenant's lineage), the unknown-token None, and the disclose-roles-only
invariant (a resolved lineage never carries an id). Runs against a migrated
database through the real repository.
"""

from __future__ import annotations

import itertools
from collections.abc import Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.derivation_repository import DerivationRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import DerivationInput, FactRecord, Tenant
from akunaki.application.score_handlers import DerivationInputSpec
from akunaki.config import Settings, clear_settings_cache
from conftest import upgrade_to_head

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
DAY = "2026-07-20"
T0_S = "2026-07-20T12:00:00Z"

_IDS = itertools.count(1)


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "derivations.db"
    url = f"sqlite+libsql:///{db_path.resolve()}"
    monkeypatch.setenv("AKUNAKI_DATABASE_URL", url)
    clear_settings_cache()
    upgrade_to_head(url)
    yield url
    clear_settings_cache()


@pytest.fixture
def factory(db_url: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=db_url))
    session_factory = create_session_factory(engine)
    with session_factory() as session, session.begin():
        for tenant_id in ("tenant-1", "tenant-2"):
            session.add(
                Tenant(
                    id=tenant_id,
                    created_at="2026-07-01T00:00:00Z",
                    status="active",
                    primary_timezone="UTC",
                    display_name=tenant_id,
                )
            )
    try:
        yield session_factory
    finally:
        engine.dispose()


def _create(
    repo: DerivationRepository,
    *,
    tenant_id: str = "tenant-1",
    token: str = "opaque_tok_fixed",  # noqa: S107  (an opaque handle, not a secret)
    inputs: list[DerivationInputSpec] | None = None,
) -> str:
    created = repo.create_run(
        run_id=f"run-{next(_IDS)}",
        tenant_id=tenant_id,
        artifact_kind="score",
        local_health_day=DAY,
        formula_version="general_recovery_v0.1.0",
        dependency_hash="",
        confidence=0.9,
        freshness_at="2026-07-20T00:00:00Z",
        as_of_at=None,
        status="ok",
        inputs=inputs or [],
        generate_token=lambda: token,
        new_input_id=lambda: f"in-{next(_IDS)}",
        now=T0,
    )
    return created.provenance_token


def test_create_and_resolve_round_trip(factory: sessionmaker[Session]) -> None:
    repo = DerivationRepository(factory)
    token = _create(repo, token="opaque_tok_round")

    lineage = repo.resolve_token(tenant_id="tenant-1", token=token)
    assert lineage is not None
    assert lineage.artifact_kind == "score"
    assert lineage.local_health_day == DAY
    assert lineage.formula_version == "general_recovery_v0.1.0"
    assert lineage.status == "ok"
    assert lineage.confidence == 0.9


def test_unknown_token_resolves_to_none(factory: sessionmaker[Session]) -> None:
    repo = DerivationRepository(factory)
    _create(repo, token="opaque_tok_real")

    assert repo.resolve_token(tenant_id="tenant-1", token="opaque_tok_absent") is None
    # An empty token is never a hit.
    assert repo.resolve_token(tenant_id="tenant-1", token="") is None


def test_token_is_tenant_scoped(factory: sessionmaker[Session]) -> None:
    repo = DerivationRepository(factory)
    token = _create(repo, tenant_id="tenant-1", token="opaque_tok_t1")

    # The very same token presented by another tenant is indistinguishable from
    # an unknown one: both None, so a token cannot be probed cross-tenant.
    assert repo.resolve_token(tenant_id="tenant-2", token=token) is None
    assert repo.resolve_token(tenant_id="tenant-1", token=token) is not None


def test_run_without_inputs_resolves_with_no_roles(
    factory: sessionmaker[Session],
) -> None:
    # A run may legitimately carry no typed inputs (an insufficient day whose
    # components were all omitted): the lineage still carries its versions and
    # status, with an empty input list rather than a fabricated one.
    repo = DerivationRepository(factory)
    token = _create(repo, token="opaque_tok_noinputs", inputs=[])

    lineage = repo.resolve_token(tenant_id="tenant-1", token=token)
    assert lineage is not None
    assert lineage.inputs == ()


def _seed_fact(factory: sessionmaker[Session], *, fact_id: str) -> None:
    """A minimal current fact row, so a typed input FK resolves."""
    with factory() as session, session.begin():
        session.add(
            FactRecord(
                id=fact_id,
                tenant_id="tenant-1",
                connection_id=None,
                provider="oura",
                entity_type="overnight_vitals",
                vendor_record_id=fact_id,
                origin=None,
                method="wearable",
                utc_instant=T0_S,
                start_utc=T0_S,
                end_utc=T0_S,
                source_offset_minutes=0,
                iana_timezone="UTC",
                local_health_day=DAY,
                unit=None,
                quality="high",
                confidence=1.0,
                freshness_at=T0_S,
                raw_revision_id=None,
                raw_payload_id=None,
                schema_version="v1",
                normalizer_version="oura_vitals_v0.1.0",
                content_hash=fact_id,
                fact_key=f"overnight_vitals:{fact_id}",
                version_n=1,
                is_current=1,
                superseded_by=None,
                superseded_at=None,
                deletion_state="active",
                exclude_from_load=0,
                created_at=T0_S,
            )
        )


def test_typed_fact_inputs_are_persisted_and_disclosed(
    factory: sessionmaker[Session],
) -> None:
    """A run records the facts it was derived from, disclosed as roles."""
    _seed_fact(factory, fact_id="fact-hrv")
    repo = DerivationRepository(factory)
    token = _create(
        repo,
        token="opaque_tok_typed",
        inputs=[DerivationInputSpec(role="hrv", fact_record_id="fact-hrv")],
    )

    # The row is durable with its typed FK — the run is traceable to the fact.
    with factory() as session:
        rows = session.execute(select(DerivationInput.role, DerivationInput.fact_record_id)).all()
    assert [tuple(row) for row in rows] == [("hrv", "fact-hrv")]

    lineage = repo.resolve_token(tenant_id="tenant-1", token=token)
    assert lineage is not None
    assert [i.role for i in lineage.inputs] == ["hrv"]


def test_repeated_role_discloses_one_entry(factory: sessionmaker[Session]) -> None:
    """Several facts behind one role must not repeat it on the public lineage.

    Ids are deliberately withheld, so a repeated role would leak how many facts
    backed the day — exactly the disclosure the roles-only contract avoids.
    """
    _seed_fact(factory, fact_id="fact-a")
    _seed_fact(factory, fact_id="fact-b")
    repo = DerivationRepository(factory)
    token = _create(
        repo,
        token="opaque_tok_dupe",
        inputs=[
            DerivationInputSpec(role="hrv", fact_record_id="fact-a"),
            DerivationInputSpec(role="hrv", fact_record_id="fact-b"),
        ],
    )

    # Both rows are stored for internal lineage...
    with factory() as session:
        assert session.execute(select(func.count()).select_from(DerivationInput)).scalar_one() == 2

    # ...but the disclosed lineage names the role exactly once.
    lineage = repo.resolve_token(tenant_id="tenant-1", token=token)
    assert lineage is not None
    assert [i.role for i in lineage.inputs] == ["hrv"]
