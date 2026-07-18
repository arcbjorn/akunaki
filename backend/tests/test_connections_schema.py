"""Connection lifecycle schema: model/migration agreement and enforced constraints.

The connection tables carry the security-relevant invariants of the ingestion
design: one connection per provider per tenant, provider/status vocabularies,
and token material that exists only as envelope-encrypted ciphertext.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import Connection, ConnectionHealth, ConnectionSecret, Tenant
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339

T0 = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)

# sqlalchemy-libsql surfaces some SQLite constraint failures as ValueError.
ConstraintError = (IntegrityError, ValueError)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def connections_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "connections.db"
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
def factory(connections_db: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=connections_db))
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


def _connection(**overrides: object) -> Connection:
    values: dict[str, object] = {
        "id": "conn-1",
        "tenant_id": "tenant-1",
        "provider": "oura",
        "status": "pending",
        "scopes_granted_json": '["daily","sleep"]',
        "external_user_id": None,
        "connected_at": NOW_S,
        "updated_at": NOW_S,
    }
    values.update(overrides)
    return Connection(**values)  # type: ignore[arg-type]


def _add(factory: sessionmaker[Session], *rows: object) -> None:
    with factory() as session, session.begin():
        for row in rows:
            session.add(row)


# ---------------------------------------------------------------------------
# Model / migration agreement
# ---------------------------------------------------------------------------


def test_connection_tables_match_models(connections_db: str) -> None:
    engine = create_db_engine(Settings(database_url=connections_db))
    try:
        insp = inspect(engine)
        assert {"connections", "connection_secrets", "connection_health"} <= set(
            insp.get_table_names()
        )
        for table, model in (
            ("connections", Connection),
            ("connection_secrets", ConnectionSecret),
            ("connection_health", ConnectionHealth),
        ):
            migration_cols = {c["name"] for c in insp.get_columns(table)}
            assert migration_cols == {c.name for c in model.__table__.columns}, table
    finally:
        engine.dispose()


def test_connection_foreign_keys_and_indexes(connections_db: str) -> None:
    engine = create_db_engine(Settings(database_url=connections_db))
    try:
        insp = inspect(engine)
        tenant_fks = insp.get_foreign_keys("connections")
        assert any(
            fk["referred_table"] == "tenants" and fk["constrained_columns"] == ["tenant_id"]
            for fk in tenant_fks
        )
        for table in ("connection_secrets", "connection_health"):
            fks = insp.get_foreign_keys(table)
            assert any(
                fk["referred_table"] == "connections"
                and fk["constrained_columns"] == ["connection_id"]
                for fk in fks
            ), table

        index_names = {ix["name"] for ix in insp.get_indexes("connections")}
        assert "ix_connections_tenant_status" in index_names
    finally:
        engine.dispose()


def test_ciphertext_column_is_binary_not_text(connections_db: str) -> None:
    # Token material must be stored as opaque bytes, never a text column that
    # could accidentally hold a readable token.
    engine = create_db_engine(Settings(database_url=connections_db))
    try:
        cols = {c["name"]: c for c in inspect(engine).get_columns("connection_secrets")}
        assert "BLOB" in str(cols["ciphertext"]["type"]).upper()
        # No column anywhere in the connection tables is named like a token.
        for table in ("connections", "connection_secrets", "connection_health"):
            names = {c["name"] for c in inspect(engine).get_columns(table)}
            assert not {n for n in names if "token" in n or "secret_value" in n}, table
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Enforced constraints
# ---------------------------------------------------------------------------


def test_one_connection_per_provider_per_tenant(factory: sessionmaker[Session]) -> None:
    _add(factory, _connection(id="conn-1"))
    with pytest.raises(ConstraintError):
        _add(factory, _connection(id="conn-2"))


def test_same_provider_different_tenants_allowed(factory: sessionmaker[Session]) -> None:
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
    _add(factory, _connection(id="conn-1"))
    _add(factory, _connection(id="conn-2", tenant_id="tenant-2"))

    with factory() as session:
        assert session.query(Connection).count() == 2


def test_multiple_providers_per_tenant_allowed(factory: sessionmaker[Session]) -> None:
    _add(
        factory,
        _connection(id="conn-oura", provider="oura"),
        _connection(id="conn-polar", provider="polar"),
        _connection(id="conn-gh", provider="google_health"),
    )
    with factory() as session:
        assert session.query(Connection).count() == 3


def test_unknown_provider_rejected(factory: sessionmaker[Session]) -> None:
    with pytest.raises(ConstraintError):
        _add(factory, _connection(provider="fitbit"))


def test_unknown_status_rejected(factory: sessionmaker[Session]) -> None:
    with pytest.raises(ConstraintError):
        _add(factory, _connection(status="connected"))


def test_all_designed_statuses_accepted(factory: sessionmaker[Session]) -> None:
    for i, status in enumerate(("pending", "active", "needs_reauth", "revoked", "error")):
        with factory() as session, session.begin():
            row = _connection(id=f"conn-{i}", status=status)
            # Distinct provider slots are exhausted; use separate tenants.
            session.add(
                Tenant(
                    id=f"tenant-s{i}",
                    created_at=NOW_S,
                    status="active",
                    primary_timezone="UTC",
                    display_name=None,
                )
            )
            row.tenant_id = f"tenant-s{i}"
            session.add(row)


def test_invalid_scopes_json_rejected(factory: sessionmaker[Session]) -> None:
    with pytest.raises(ConstraintError):
        _add(factory, _connection(scopes_granted_json="not json"))


def test_empty_ciphertext_rejected(factory: sessionmaker[Session]) -> None:
    _add(factory, _connection())
    with pytest.raises(ConstraintError):
        _add(
            factory,
            ConnectionSecret(
                connection_id="conn-1",
                tenant_id="tenant-1",
                ciphertext=b"",
                key_version="v1",
                rotated_at=NOW_S,
            ),
        )


def test_empty_key_version_rejected(factory: sessionmaker[Session]) -> None:
    _add(factory, _connection())
    with pytest.raises(ConstraintError):
        _add(
            factory,
            ConnectionSecret(
                connection_id="conn-1",
                tenant_id="tenant-1",
                ciphertext=b"\x01\x02",
                key_version="",
                rotated_at=NOW_S,
            ),
        )


def test_secret_roundtrips_as_bytes(factory: sessionmaker[Session]) -> None:
    blob = bytes(range(256))
    _add(factory, _connection())
    _add(
        factory,
        ConnectionSecret(
            connection_id="conn-1",
            tenant_id="tenant-1",
            ciphertext=blob,
            key_version="kek-v1",
            rotated_at=NOW_S,
        ),
    )
    with factory() as session:
        stored = session.get(ConnectionSecret, "conn-1")
        assert stored is not None
        # Full byte range survives; no text coercion or truncation.
        assert stored.ciphertext == blob


def test_libsql_driver_still_lacks_dbapi_binary() -> None:
    """Pin the driver limitation that forces the local ``Blob`` type.

    ``libsql_experimental`` stores BLOBs correctly but exposes no DBAPI
    ``Binary`` constructor, so SQLAlchemy's stock ``LargeBinary`` bind
    processor raises before executing. If a future driver release adds it,
    this test fails on purpose so ``Blob`` can be reconsidered.
    """
    import libsql_experimental

    assert not hasattr(libsql_experimental, "Binary")


def test_stock_large_binary_still_fails_on_this_driver(factory: sessionmaker[Session]) -> None:
    """The workaround is load-bearing, not cargo-culted.

    Binding through stock ``LargeBinary`` must still raise on this driver;
    if it starts working, ``Blob`` is no longer required.
    """
    from sqlalchemy import LargeBinary, bindparam
    from sqlalchemy import text as sa_text

    _add(factory, _connection())
    with factory() as session, pytest.raises(Exception, match=r"(?i)binary"), session.begin():
        session.execute(
            sa_text(
                "INSERT INTO connection_secrets "
                "(connection_id, tenant_id, ciphertext, key_version, rotated_at) "
                "VALUES (:cid, :tid, :blob, :kv, :ts)"
            ).bindparams(bindparam("blob", type_=LargeBinary())),
            {
                "cid": "conn-1",
                "tid": "tenant-1",
                "blob": b"\x01\x02",
                "kv": "v1",
                "ts": NOW_S,
            },
        )


def test_negative_consecutive_failures_rejected(factory: sessionmaker[Session]) -> None:
    _add(factory, _connection())
    with pytest.raises(ConstraintError):
        _add(
            factory,
            ConnectionHealth(
                connection_id="conn-1",
                tenant_id="tenant-1",
                consecutive_failures=-1,
            ),
        )


def test_health_defaults_to_zero_failures(factory: sessionmaker[Session]) -> None:
    _add(factory, _connection())
    _add(factory, ConnectionHealth(connection_id="conn-1", tenant_id="tenant-1"))
    with factory() as session:
        health = session.get(ConnectionHealth, "conn-1")
        assert health is not None
        assert health.consecutive_failures == 0
        assert health.last_success_at is None


def test_secret_and_health_cascade_with_connection(factory: sessionmaker[Session]) -> None:
    _add(factory, _connection())
    _add(
        factory,
        ConnectionSecret(
            connection_id="conn-1",
            tenant_id="tenant-1",
            ciphertext=b"\x01",
            key_version="v1",
            rotated_at=NOW_S,
        ),
        ConnectionHealth(connection_id="conn-1", tenant_id="tenant-1"),
    )

    with factory() as session, session.begin():
        connection = session.get(Connection, "conn-1")
        assert connection is not None
        session.delete(connection)

    # Deleting a connection must not orphan its secret material.
    with factory() as session:
        assert session.get(ConnectionSecret, "conn-1") is None
        assert session.get(ConnectionHealth, "conn-1") is None


def test_connection_requires_existing_tenant(factory: sessionmaker[Session]) -> None:
    with pytest.raises(ConstraintError):
        _add(factory, _connection(id="conn-x", tenant_id="tenant-missing"))
