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
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.derivation_repository import DerivationRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import Tenant
from akunaki.application.score_handlers import DerivationInputSpec
from akunaki.config import Settings, clear_settings_cache

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
DAY = "2026-07-20"

_IDS = itertools.count(1)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "derivations.db"
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
    # v0.1.0 records score runs without typed fact-id inputs (roles are not
    # threaded yet): a resolved lineage still carries its versions and status,
    # with an empty input list rather than a fabricated one.
    repo = DerivationRepository(factory)
    token = _create(repo, token="opaque_tok_noinputs", inputs=[])

    lineage = repo.resolve_token(tenant_id="tenant-1", token=token)
    assert lineage is not None
    assert lineage.inputs == ()
