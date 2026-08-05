"""``UserRepository.account_for``: the account read behind ``GET /v1/me``.

The tenant scope is tested here rather than through the route on purpose. A
session's tenant is derived from the user row when it is issued, so the API
cannot present a mismatched ``(user_id, tenant_id)`` pair — the guard is
defence-in-depth, and the repository is the only place its behaviour is
observable.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import Tenant, User
from akunaki.adapters.db.user_repository import UserRepository
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339

NOW_S = to_utc_rfc3339(datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC))


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "user_account.db"
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
def factory(db_url: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=db_url))
    session_factory = create_session_factory(engine)
    with session_factory() as session, session.begin():
        for suffix, tz in (("1", "Europe/Berlin"), ("2", "Asia/Tokyo")):
            session.add(
                Tenant(
                    id=f"tenant-{suffix}",
                    created_at=NOW_S,
                    status="active",
                    primary_timezone=tz,
                    display_name=f"Person {suffix}",
                )
            )
            session.add(
                User(
                    id=f"user-{suffix}",
                    tenant_id=f"tenant-{suffix}",
                    oidc_issuer="https://idp.example.com",
                    oidc_subject=f"subject-{suffix}",
                    email=f"person{suffix}@example.com",
                    created_at=NOW_S,
                )
            )
    try:
        yield session_factory
    finally:
        engine.dispose()


def test_reads_the_account_and_its_tenant(factory: sessionmaker[Session]) -> None:
    account = UserRepository(factory).account_for(user_id="user-1", tenant_id="tenant-1")

    assert account is not None
    assert account.user_id == "user-1"
    assert account.tenant_id == "tenant-1"
    assert account.email == "person1@example.com"
    assert account.primary_timezone == "Europe/Berlin"
    assert account.display_name == "Person 1"
    assert account.tenant_status == "active"


def test_a_mismatched_pair_reads_nothing(factory: sessionmaker[Session]) -> None:
    """The tenant scope, not the user id, is what makes this empty.

    Filtering on the user alone would return tenant-1's account for a caller
    claiming tenant-2 — the exact cross-tenant read the pair prevents.
    """
    account = UserRepository(factory).account_for(user_id="user-1", tenant_id="tenant-2")

    assert account is None


def test_an_unknown_user_reads_nothing(factory: sessionmaker[Session]) -> None:
    assert UserRepository(factory).account_for(user_id="ghost", tenant_id="tenant-1") is None


def test_each_tenant_reads_its_own_timezone(factory: sessionmaker[Session]) -> None:
    """Two tenants, two timezones — no shared or defaulted value."""
    users = UserRepository(factory)

    first = users.account_for(user_id="user-1", tenant_id="tenant-1")
    second = users.account_for(user_id="user-2", tenant_id="tenant-2")

    assert first is not None
    assert second is not None
    assert first.primary_timezone == "Europe/Berlin"
    assert second.primary_timezone == "Asia/Tokyo"
