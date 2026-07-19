"""Sync transport schema: model/migration agreement and enforced invariants.

The transport layer carries the crash-replay guarantees of the ingestion
design: every vendor response is retained, logical revisions are append-only,
and a tombstone can only ever mean a vendor or privacy deletion.
"""

from __future__ import annotations

import json
from collections.abc import Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import (
    Connection,
    RawObject,
    RawPayload,
    RawRevision,
    SyncCursor,
    SyncRun,
    Tenant,
)
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339

T0 = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
NOW_S = to_utc_rfc3339(T0)

ConstraintError = (IntegrityError, ValueError)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def sync_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "sync.db"
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
def factory(sync_db: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=sync_db))
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


def _add(factory: sessionmaker[Session], *rows: object) -> None:
    with factory() as session, session.begin():
        for row in rows:
            session.add(row)


def _payload(**overrides: object) -> RawPayload:
    values: dict[str, object] = {
        "id": "pay-1",
        "tenant_id": "tenant-1",
        "connection_id": "conn-1",
        "sync_run_id": None,
        "transport_kind": "sync_fetch",
        "provider": "oura",
        "stream": "sleep",
        "page_token": None,
        "fetched_at": NOW_S,
        "received_at": NOW_S,
        "http_status": 200,
        "content_type": "application/json",
        "content_hash": "abc123",
        "payload_json": '{"data":[]}',
        "payload_blob": None,
        "request_meta_json": json.dumps({"url_template": "v2/sleep"}),
    }
    values.update(overrides)
    return RawPayload(**values)  # type: ignore[arg-type]


def _run(**overrides: object) -> SyncRun:
    values: dict[str, object] = {
        "id": "run-1",
        "tenant_id": "tenant-1",
        "connection_id": "conn-1",
        "trigger": "initial",
        "stream": "sleep",
        "status": "running",
        "started_at": NOW_S,
    }
    values.update(overrides)
    return SyncRun(**values)  # type: ignore[arg-type]


def _revision(**overrides: object) -> RawRevision:
    values: dict[str, object] = {
        "id": "rev-1",
        "tenant_id": "tenant-1",
        "raw_object_id": "obj-1",
        "raw_payload_id": "pay-1",
        "sync_run_id": None,
        "revision_n": 1,
        "vendor_record_id": "vendor-1",
        "observed_at": NOW_S,
        "effective_at": NOW_S,
        "received_at": NOW_S,
        "content_hash": "hash-1",
        "schema_version": "oura.v2",
        "deletion_state": "active",
        "is_tombstone": 0,
        "tombstone_reason": None,
    }
    values.update(overrides)
    return RawRevision(**values)  # type: ignore[arg-type]


def _object(**overrides: object) -> RawObject:
    values: dict[str, object] = {
        "id": "obj-1",
        "tenant_id": "tenant-1",
        "connection_id": "conn-1",
        "provider": "oura",
        "stream": "sleep",
        "vendor_record_id": "vendor-1",
        "current_revision_id": None,
        "created_at": NOW_S,
    }
    values.update(overrides)
    return RawObject(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Model / migration agreement
# ---------------------------------------------------------------------------


def test_transport_tables_match_models(sync_db: str) -> None:
    engine = create_db_engine(Settings(database_url=sync_db))
    try:
        insp = inspect(engine)
        assert {
            "sync_runs",
            "raw_payload",
            "sync_cursors",
            "raw_objects",
            "raw_revisions",
        } <= set(insp.get_table_names())
        for table, model in (
            ("sync_runs", SyncRun),
            ("raw_payload", RawPayload),
            ("sync_cursors", SyncCursor),
            ("raw_objects", RawObject),
            ("raw_revisions", RawRevision),
        ):
            migration_cols = {c["name"] for c in insp.get_columns(table)}
            assert migration_cols == {c.name for c in model.__table__.columns}, table
    finally:
        engine.dispose()


def test_raw_payload_content_hash_index_is_not_unique(sync_db: str) -> None:
    """Uniqueness here would break the 'retain every response' guarantee."""
    engine = create_db_engine(Settings(database_url=sync_db))
    try:
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("raw_payload")}
        assert "ix_raw_payload_connection_content_hash" in indexes
        # The libSQL dialect reports this flag as 0/1, not True/False.
        assert not indexes["ix_raw_payload_connection_content_hash"]["unique"]
    finally:
        engine.dispose()


def test_raw_revisions_have_no_normalizer_version(sync_db: str) -> None:
    # Raw rows are immutable snapshots; normalizer_version belongs on facts.
    engine = create_db_engine(Settings(database_url=sync_db))
    try:
        cols = {c["name"] for c in inspect(engine).get_columns("raw_revisions")}
        assert "normalizer_version" not in cols
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Retain-every-response
# ---------------------------------------------------------------------------


def test_identical_bodies_create_separate_transport_rows(
    factory: sessionmaker[Session],
) -> None:
    """A retried fetch must never overwrite or collapse a transport row."""
    _add(factory, _payload(id="pay-1", content_hash="same"))
    _add(factory, _payload(id="pay-2", content_hash="same"))

    with factory() as session:
        rows = session.scalars(select(RawPayload)).all()
    assert len(rows) == 2


def test_payload_may_predate_a_sync_run(factory: sessionmaker[Session]) -> None:
    # Webhook capture lands before any run exists, so sync_run_id is nullable.
    _add(factory, _payload(sync_run_id=None, transport_kind="webhook_capture", fetched_at=None))

    with factory() as session:
        row = session.get(RawPayload, "pay-1")
        assert row is not None
        assert row.sync_run_id is None


def test_payload_body_representations_are_exclusive(
    factory: sessionmaker[Session],
) -> None:
    with pytest.raises(ConstraintError):
        _add(factory, _payload(payload_json='{"a":1}', payload_blob=b"\x01"))


def test_payload_rejects_invalid_json(factory: sessionmaker[Session]) -> None:
    with pytest.raises(ConstraintError):
        _add(factory, _payload(payload_json="not json"))
    with pytest.raises(ConstraintError):
        _add(factory, _payload(id="pay-2", request_meta_json="not json"))


def test_payload_rejects_unknown_transport_kind(factory: sessionmaker[Session]) -> None:
    with pytest.raises(ConstraintError):
        _add(factory, _payload(transport_kind="carrier_pigeon"))


def test_blob_body_roundtrips(factory: sessionmaker[Session]) -> None:
    blob = bytes(range(256))
    _add(factory, _payload(payload_json=None, payload_blob=blob, content_type="application/cbor"))

    with factory() as session:
        row = session.get(RawPayload, "pay-1")
        assert row is not None
        assert row.payload_blob == blob


# ---------------------------------------------------------------------------
# Logical revisions
# ---------------------------------------------------------------------------


def test_revision_numbers_are_unique_per_object(factory: sessionmaker[Session]) -> None:
    _add(factory, _payload(), _object())
    _add(factory, _revision(id="rev-1", revision_n=1))

    with pytest.raises(ConstraintError):
        _add(factory, _revision(id="rev-2", revision_n=1))


def test_revisions_append_monotonically(factory: sessionmaker[Session]) -> None:
    _add(factory, _payload(), _object())
    _add(factory, _revision(id="rev-1", revision_n=1, content_hash="h1"))
    _add(factory, _revision(id="rev-2", revision_n=2, content_hash="h2"))

    with factory() as session:
        revisions = session.scalars(select(RawRevision).order_by(RawRevision.revision_n)).all()
    assert [r.revision_n for r in revisions] == [1, 2]


def test_same_content_hash_is_allowed_but_findable(factory: sessionmaker[Session]) -> None:
    """The hash index supports 'skip append when unchanged' without enforcing it."""
    _add(factory, _payload(), _object())
    _add(factory, _revision(id="rev-1", revision_n=1, content_hash="dup"))
    _add(factory, _revision(id="rev-2", revision_n=2, content_hash="dup"))

    with factory() as session:
        found = session.scalars(
            select(RawRevision).where(
                RawRevision.raw_object_id == "obj-1",
                RawRevision.content_hash == "dup",
            )
        ).all()
    assert len(found) == 2


def test_superseded_is_not_a_valid_tombstone_reason(
    factory: sessionmaker[Session],
) -> None:
    """Superseding is expressed by a later revision, never by a tombstone."""
    _add(factory, _payload(), _object())
    with pytest.raises(ConstraintError):
        _add(
            factory,
            _revision(is_tombstone=1, tombstone_reason="superseded", deletion_state="active"),
        )


@pytest.mark.parametrize("reason", ["vendor_deleted", "privacy_delete"])
def test_valid_tombstone_reasons_accepted(factory: sessionmaker[Session], reason: str) -> None:
    _add(factory, _payload(), _object())
    _add(
        factory,
        _revision(
            is_tombstone=1,
            tombstone_reason=reason,
            deletion_state="vendor_deleted" if reason == "vendor_deleted" else "privacy_scrubbed",
        ),
    )


def test_tombstone_flag_and_reason_must_agree(factory: sessionmaker[Session]) -> None:
    _add(factory, _payload(), _object())
    # Flag set without a reason.
    with pytest.raises(ConstraintError):
        _add(factory, _revision(id="rev-a", is_tombstone=1, tombstone_reason=None))
    # Reason set without the flag.
    with pytest.raises(ConstraintError):
        _add(factory, _revision(id="rev-b", is_tombstone=0, tombstone_reason="vendor_deleted"))


def test_revision_rejects_unknown_deletion_state(factory: sessionmaker[Session]) -> None:
    _add(factory, _payload(), _object())
    with pytest.raises(ConstraintError):
        _add(factory, _revision(deletion_state="archived"))


def test_revision_number_must_be_positive(factory: sessionmaker[Session]) -> None:
    _add(factory, _payload(), _object())
    with pytest.raises(ConstraintError):
        _add(factory, _revision(revision_n=0))


def test_object_identity_is_unique(factory: sessionmaker[Session]) -> None:
    _add(factory, _object(id="obj-1"))
    with pytest.raises(ConstraintError):
        _add(factory, _object(id="obj-2"))


def test_transport_row_cannot_be_deleted_while_referenced(
    factory: sessionmaker[Session],
) -> None:
    """RESTRICT protects the exact vendor body a revision points at."""
    _add(factory, _payload(), _object())
    _add(factory, _revision())

    with pytest.raises(ConstraintError), factory() as session, session.begin():
        payload = session.get(RawPayload, "pay-1")
        assert payload is not None
        session.delete(payload)


# ---------------------------------------------------------------------------
# Runs and cursors
# ---------------------------------------------------------------------------


def test_sync_run_status_and_trigger_vocabularies(factory: sessionmaker[Session]) -> None:
    for i, trigger in enumerate(("schedule", "webhook", "manual", "reconcile", "initial")):
        _add(factory, _run(id=f"run-t{i}", trigger=trigger))
    with pytest.raises(ConstraintError):
        _add(factory, _run(id="run-bad", trigger="cron"))
    with pytest.raises(ConstraintError):
        _add(factory, _run(id="run-bad2", status="pending"))


def test_sync_run_stats_must_be_valid_json(factory: sessionmaker[Session]) -> None:
    _add(factory, _run(stats_json=json.dumps({"pages": 3, "revisions": 2})))
    with pytest.raises(ConstraintError):
        _add(factory, _run(id="run-2", stats_json="not json"))


def test_one_cursor_per_connection_stream(factory: sessionmaker[Session]) -> None:
    _add(
        factory,
        SyncCursor(
            id="cur-1",
            tenant_id="tenant-1",
            connection_id="conn-1",
            stream="sleep",
            cursor_type="timestamp",
            cursor_value=NOW_S,
            updated_at=NOW_S,
        ),
    )
    with pytest.raises(ConstraintError):
        _add(
            factory,
            SyncCursor(
                id="cur-2",
                tenant_id="tenant-1",
                connection_id="conn-1",
                stream="sleep",
                cursor_type="timestamp",
                cursor_value=NOW_S,
                updated_at=NOW_S,
            ),
        )


def test_cursor_rejects_unknown_type(factory: sessionmaker[Session]) -> None:
    with pytest.raises(ConstraintError):
        _add(
            factory,
            SyncCursor(
                id="cur-x",
                tenant_id="tenant-1",
                connection_id="conn-1",
                stream="sleep",
                cursor_type="offset",
                cursor_value="0",
                updated_at=NOW_S,
            ),
        )


def test_transport_cascades_with_connection(factory: sessionmaker[Session]) -> None:
    # Insert the run first: raw_payload.sync_run_id is a real FK.
    _add(factory, _run())
    _add(factory, _payload(sync_run_id="run-1"), _object())

    with factory() as session, session.begin():
        connection = session.get(Connection, "conn-1")
        assert connection is not None
        session.delete(connection)

    with factory() as session:
        assert session.scalars(select(SyncRun)).all() == []
        assert session.scalars(select(RawPayload)).all() == []
        assert session.scalars(select(RawObject)).all() == []
