"""Sealed tokens persist to connection_secrets as ciphertext and open again.

This closes the loop the connection schema opened: the column stores an
envelope produced by the real sealer, the stored bytes contain no readable
token, and rotation re-seals under a new key version.
"""

from __future__ import annotations

import base64
from collections.abc import Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.crypto.config import build_sealer
from akunaki.adapters.crypto.envelope import KEY_BYTES, EnvelopeSealer
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import Connection, ConnectionSecret, Tenant
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339
from akunaki.domain.secrets import SealedSecret, SecretDecryptionError

T0 = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)

KEK_V1 = b"\x11" * KEY_BYTES
KEK_V2 = b"\x22" * KEY_BYTES
REFRESH_TOKEN = b'{"refresh_token":"oura-rt-super-secret","expires_in":86400}'


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def secrets_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "secrets.db"
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
def factory(secrets_db: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=secrets_db))
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
                scopes_granted_json='["daily","sleep"]',
                external_user_id="oura-user-1",
                connected_at=NOW_S,
                updated_at=NOW_S,
            )
        )
    try:
        yield session_factory
    finally:
        engine.dispose()


def _sealer(active: str = "v1") -> EnvelopeSealer:
    return EnvelopeSealer(keys={"v1": KEK_V1, "v2": KEK_V2}, active_key_version=active)


def _store(
    factory: sessionmaker[Session],
    sealed: SealedSecret,
    *,
    connection_id: str = "conn-1",
) -> None:
    with factory() as session, session.begin():
        session.merge(
            ConnectionSecret(
                connection_id=connection_id,
                tenant_id="tenant-1",
                ciphertext=sealed.ciphertext,
                key_version=sealed.key_version,
                rotated_at=NOW_S,
            )
        )


def _load(factory: sessionmaker[Session], connection_id: str = "conn-1") -> SealedSecret:
    with factory() as session:
        row = session.get(ConnectionSecret, connection_id)
        assert row is not None
        return SealedSecret(ciphertext=row.ciphertext, key_version=row.key_version)


def test_sealed_token_roundtrips_through_the_database(factory: sessionmaker[Session]) -> None:
    sealer = _sealer()
    # AAD binds the envelope to its owning connection.
    _store(factory, sealer.seal(REFRESH_TOKEN, aad=b"conn-1"))

    reopened = sealer.open(_load(factory), aad=b"conn-1")
    assert reopened == REFRESH_TOKEN


def test_stored_bytes_contain_no_readable_token(
    factory: sessionmaker[Session], secrets_db: str
) -> None:
    _store(factory, _sealer().seal(REFRESH_TOKEN, aad=b"conn-1"))

    # Read the raw column, bypassing the ORM, as an operator or a leaked dump
    # would see it.
    engine = create_db_engine(Settings(database_url=secrets_db))
    try:
        with engine.connect() as conn:
            stored = conn.execute(text("SELECT ciphertext FROM connection_secrets")).scalar_one()
    finally:
        engine.dispose()

    assert isinstance(stored, bytes)
    assert REFRESH_TOKEN not in stored
    assert b"oura-rt-super-secret" not in stored
    assert b"refresh_token" not in stored


def test_envelope_cannot_be_moved_to_another_connection(
    factory: sessionmaker[Session],
) -> None:
    """AAD binding stops a stolen row being replayed onto a different connection."""
    sealer = _sealer()
    with factory() as session, session.begin():
        session.add(
            Connection(
                id="conn-2",
                tenant_id="tenant-1",
                provider="polar",
                status="active",
                scopes_granted_json="[]",
                connected_at=NOW_S,
                updated_at=NOW_S,
            )
        )

    sealed = sealer.seal(REFRESH_TOKEN, aad=b"conn-1")
    # Copy conn-1's ciphertext onto conn-2's row.
    _store(factory, sealed, connection_id="conn-2")

    with pytest.raises(SecretDecryptionError):
        sealer.open(_load(factory, "conn-2"), aad=b"conn-2")


def test_key_version_column_matches_envelope(factory: sessionmaker[Session]) -> None:
    _store(factory, _sealer(active="v2").seal(REFRESH_TOKEN, aad=b"conn-1"))

    with factory() as session:
        row = session.get(ConnectionSecret, "conn-1")
        assert row is not None
        assert row.key_version == "v2"


def test_rotation_reseals_row_under_new_key_version(factory: sessionmaker[Session]) -> None:
    """Re-encrypt on read: open with the old KEK, store under the new one."""
    old = _sealer(active="v1")
    _store(factory, old.seal(REFRESH_TOKEN, aad=b"conn-1"))
    assert _load(factory).key_version == "v1"

    rotated = _sealer(active="v2")
    plaintext = rotated.open(_load(factory), aad=b"conn-1")
    _store(factory, rotated.seal(plaintext, aad=b"conn-1"))

    refreshed = _load(factory)
    assert refreshed.key_version == "v2"
    assert rotated.open(refreshed, aad=b"conn-1") == REFRESH_TOKEN


def test_secret_row_is_deleted_with_its_connection(factory: sessionmaker[Session]) -> None:
    _store(factory, _sealer().seal(REFRESH_TOKEN, aad=b"conn-1"))

    with factory() as session, session.begin():
        connection = session.get(Connection, "conn-1")
        assert connection is not None
        session.delete(connection)

    # Revoking a connection must not leave orphaned token ciphertext behind.
    with factory() as session:
        assert session.get(ConnectionSecret, "conn-1") is None


def test_settings_configured_sealer_persists_and_reopens(
    factory: sessionmaker[Session], secrets_db: str
) -> None:
    """The configured production path works end to end, not just the test sealer."""
    settings = Settings(
        database_url=secrets_db,
        secret_keks=f"prod-v1:{base64.b64encode(KEK_V1).decode()}",
    )
    sealer = build_sealer(settings)

    _store(factory, sealer.seal(REFRESH_TOKEN, aad=b"conn-1"))
    stored = _load(factory)

    assert stored.key_version == "prod-v1"
    assert sealer.open(stored, aad=b"conn-1") == REFRESH_TOKEN
