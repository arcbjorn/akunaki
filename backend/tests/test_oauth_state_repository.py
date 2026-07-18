"""OAuth state persistence: single-use consumption against real libSQL.

These are the callback-security invariants: a state works exactly once, only
before it expires, only with the exact redirect URI it was issued for, and the
raw state never appears in the database.
"""

from __future__ import annotations

import threading
from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.crypto.envelope import KEY_BYTES, EnvelopeSealer
from akunaki.adapters.crypto.oauth import generate_code_verifier, generate_state, hash_state
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import OAuthState, Tenant
from akunaki.adapters.db.oauth_state_repository import OAuthStateRepository
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339
from akunaki.domain.oauth import OAuthStateRejection

T0 = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)
TTL = timedelta(minutes=10)
REDIRECT = "https://app.example.com/oauth/oura/callback"
KEK = b"\x33" * KEY_BYTES

ConstraintError = (IntegrityError, ValueError)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def oauth_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "oauth.db"
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
def factory(oauth_db: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=oauth_db))
    session_factory = create_session_factory(engine)
    with session_factory() as session, session.begin():
        session.add(
            Tenant(
                id="tenant-1",
                created_at=to_utc_rfc3339(T0),
                status="active",
                primary_timezone="UTC",
                display_name="Test",
            )
        )
    try:
        yield session_factory
    finally:
        engine.dispose()


@pytest.fixture
def repository(factory: sessionmaker[Session]) -> OAuthStateRepository:
    return OAuthStateRepository(factory)


def _sealer() -> EnvelopeSealer:
    return EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1")


def _create(
    repository: OAuthStateRepository,
    *,
    state: str,
    state_id: str = "state-1",
    verifier: str | None = None,
    redirect_uri: str = REDIRECT,
    now: datetime = T0,
    ttl: timedelta = TTL,
) -> str:
    """Create one authorize row; returns the PKCE verifier that was sealed."""
    code_verifier = verifier or generate_code_verifier()
    repository.create(
        state_id=state_id,
        tenant_id="tenant-1",
        provider="oura",
        state=state,
        sealed_verifier=_sealer().seal(code_verifier.encode(), aad=state_id.encode()),
        redirect_uri=redirect_uri,
        now=now,
        ttl=ttl,
    )
    return code_verifier


# ---------------------------------------------------------------------------
# Storage secrecy
# ---------------------------------------------------------------------------


def test_raw_state_is_never_stored(repository: OAuthStateRepository, oauth_db: str) -> None:
    state = generate_state()
    verifier = _create(repository, state=state)

    engine = create_db_engine(Settings(database_url=oauth_db))
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT state_hash, code_verifier_ciphertext FROM oauth_states")
            ).one()
    finally:
        engine.dispose()

    stored_hash, ciphertext = row
    assert stored_hash == hash_state(state)
    assert state not in stored_hash
    # The PKCE verifier is sealed, not readable from the row.
    assert verifier.encode() not in ciphertext


def test_duplicate_state_hash_is_rejected(
    repository: OAuthStateRepository, factory: sessionmaker[Session]
) -> None:
    state = generate_state()
    _create(repository, state=state, state_id="state-1")

    # Same state must never map to two rows.
    with pytest.raises(ConstraintError):
        _create(repository, state=state, state_id="state-2")


# ---------------------------------------------------------------------------
# Consumption rules
# ---------------------------------------------------------------------------


def test_valid_consume_returns_the_sealed_verifier(repository: OAuthStateRepository) -> None:
    state = generate_state()
    verifier = _create(repository, state=state)

    result = repository.consume(state=state, redirect_uri=REDIRECT, now=T0 + timedelta(minutes=1))

    assert result.ok
    assert result.rejection is None
    assert result.tenant_id == "tenant-1"
    assert result.provider == "oura"
    assert result.sealed_verifier is not None
    opened = _sealer().open(result.sealed_verifier, aad=b"state-1")
    assert opened.decode() == verifier


def test_state_is_single_use(repository: OAuthStateRepository) -> None:
    state = generate_state()
    _create(repository, state=state)

    first = repository.consume(state=state, redirect_uri=REDIRECT, now=T0)
    second = repository.consume(state=state, redirect_uri=REDIRECT, now=T0)

    assert first.ok
    # A replayed callback must not obtain the verifier again.
    assert not second.ok
    assert second.rejection is OAuthStateRejection.ALREADY_CONSUMED
    assert second.sealed_verifier is None


def test_expired_state_is_rejected(repository: OAuthStateRepository) -> None:
    state = generate_state()
    _create(repository, state=state, ttl=timedelta(minutes=5))

    result = repository.consume(
        state=state,
        redirect_uri=REDIRECT,
        now=T0 + timedelta(minutes=6),
    )

    assert not result.ok
    assert result.rejection is OAuthStateRejection.EXPIRED
    assert result.sealed_verifier is None


def test_expiry_boundary_is_exclusive(repository: OAuthStateRepository) -> None:
    state = generate_state()
    _create(repository, state=state, ttl=timedelta(minutes=5))

    # Exactly at expires_at the state is already dead.
    at_expiry = repository.consume(
        state=state,
        redirect_uri=REDIRECT,
        now=T0 + timedelta(minutes=5),
    )
    assert at_expiry.rejection is OAuthStateRejection.EXPIRED


def test_redirect_uri_must_match_exactly(repository: OAuthStateRepository) -> None:
    state = generate_state()
    _create(repository, state=state)

    for candidate in (
        REDIRECT + "/",
        REDIRECT + "?code=x",
        REDIRECT.replace("https", "http"),
        "https://evil.test/callback",
    ):
        result = repository.consume(state=state, redirect_uri=candidate, now=T0)
        assert result.rejection is OAuthStateRejection.REDIRECT_MISMATCH, candidate
        assert result.sealed_verifier is None


def test_redirect_mismatch_does_not_consume_the_state(
    repository: OAuthStateRepository,
) -> None:
    """A failed attempt must not burn the state, or it becomes a DoS vector."""
    state = generate_state()
    _create(repository, state=state)

    bad = repository.consume(state=state, redirect_uri="https://evil.test/cb", now=T0)
    good = repository.consume(state=state, redirect_uri=REDIRECT, now=T0)

    assert bad.rejection is OAuthStateRejection.REDIRECT_MISMATCH
    assert good.ok


def test_unknown_state_is_rejected(repository: OAuthStateRepository) -> None:
    _create(repository, state=generate_state())

    result = repository.consume(state=generate_state(), redirect_uri=REDIRECT, now=T0)

    assert result.rejection is OAuthStateRejection.NOT_FOUND
    assert result.sealed_verifier is None


def test_empty_inputs_are_rejected(repository: OAuthStateRepository) -> None:
    assert repository.consume(state="", redirect_uri=REDIRECT, now=T0).rejection is (
        OAuthStateRejection.NOT_FOUND
    )
    assert repository.consume(state="x", redirect_uri="", now=T0).rejection is (
        OAuthStateRejection.NOT_FOUND
    )


def test_consumed_at_is_recorded(
    repository: OAuthStateRepository, factory: sessionmaker[Session]
) -> None:
    state = generate_state()
    _create(repository, state=state)
    consume_time = T0 + timedelta(minutes=2)

    repository.consume(state=state, redirect_uri=REDIRECT, now=consume_time)

    with factory() as session:
        row = session.get(OAuthState, "state-1")
        assert row is not None
        assert row.consumed_at == to_utc_rfc3339(consume_time)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_consume_yields_exactly_one_winner(oauth_db: str) -> None:
    """Simultaneous callbacks: only one may obtain the verifier.

    All threads do read the row as unconsumed (verified by tracing), so the
    race window is real. **Timing caveat:** libSQL serializes the write
    transactions, so at natural timing the read-side ``consumed_at`` check
    alone is enough to break the tie — this test still passes with the atomic
    CAS guard removed. Widening the read->write window makes it fail without
    the CAS and pass with it, confirming the guard is load-bearing on a store
    that does not serialize writes. Kept as a regression net for both layers.
    """
    n_threads = 4
    engine = create_db_engine(Settings(database_url=oauth_db))
    try:
        factory = create_session_factory(engine)
        with factory() as session, session.begin():
            session.add(
                Tenant(
                    id="tenant-1",
                    created_at=to_utc_rfc3339(T0),
                    status="active",
                    primary_timezone="UTC",
                    display_name="Test",
                )
            )
        state = generate_state()
        _create(OAuthStateRepository(factory), state=state)
    finally:
        engine.dispose()

    barrier = threading.Barrier(n_threads)
    successes: list[bool] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def attempt() -> None:
        worker_engine = create_db_engine(Settings(database_url=oauth_db))
        try:
            repo = OAuthStateRepository(create_session_factory(worker_engine))
            barrier.wait(timeout=10)
            result = repo.consume(state=state, redirect_uri=REDIRECT, now=T0)
            with lock:
                successes.append(result.ok)
        except BaseException as exc:
            errors.append(exc)
        finally:
            worker_engine.dispose()

    threads = [threading.Thread(target=attempt, name=f"cb-{i}") for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), f"thread {t.name} still alive"

    assert not errors, f"consume errors: {errors}"
    assert successes.count(True) == 1, f"expected exactly one winner, got {successes}"


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------


def test_purge_removes_expired_and_keeps_live_states(
    repository: OAuthStateRepository, factory: sessionmaker[Session]
) -> None:
    _create(repository, state=generate_state(), state_id="old", ttl=timedelta(minutes=1))
    _create(repository, state=generate_state(), state_id="live", ttl=timedelta(hours=2))

    removed = repository.purge_expired(now=T0 + timedelta(minutes=30))

    assert removed == 1
    with factory() as session:
        assert session.get(OAuthState, "old") is None
        assert session.get(OAuthState, "live") is not None


def test_purge_clears_spent_verifier_ciphertext(
    repository: OAuthStateRepository, factory: sessionmaker[Session]
) -> None:
    """Consumed rows must not retain sealed verifiers forever."""
    state = generate_state()
    _create(repository, state=state, ttl=timedelta(minutes=5))
    repository.consume(state=state, redirect_uri=REDIRECT, now=T0)

    repository.purge_expired(now=T0 + timedelta(minutes=10))

    with factory() as session:
        assert session.get(OAuthState, "state-1") is None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_create_rejects_invalid_arguments(repository: OAuthStateRepository) -> None:
    sealed = _sealer().seal(b"verifier")
    base = {
        "state_id": "s1",
        "tenant_id": "tenant-1",
        "provider": "oura",
        "state": "raw-state",
        "sealed_verifier": sealed,
        "redirect_uri": REDIRECT,
        "now": T0,
        "ttl": TTL,
    }
    for field in ("state_id", "tenant_id", "provider", "state", "redirect_uri"):
        with pytest.raises(ValueError, match=f"{field} must be non-empty"):
            repository.create(**{**base, field: ""})  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="ttl must be at least one second"):
        repository.create(**{**base, "ttl": timedelta(0)})  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="must be timezone-aware"):
        repository.create(**{**base, "now": datetime(2026, 7, 18, 12, 0, 0)})  # type: ignore[arg-type]


def test_states_cascade_with_tenant(
    repository: OAuthStateRepository, factory: sessionmaker[Session]
) -> None:
    _create(repository, state=generate_state())

    with factory() as session, session.begin():
        tenant = session.get(Tenant, "tenant-1")
        assert tenant is not None
        session.delete(tenant)

    with factory() as session:
        assert session.get(OAuthState, "state-1") is None


def test_oauth_state_model_matches_migration(oauth_db: str) -> None:
    from sqlalchemy import inspect

    engine = create_db_engine(Settings(database_url=oauth_db))
    try:
        insp = inspect(engine)
        assert "oauth_states" in insp.get_table_names()
        migration_cols = {c["name"] for c in insp.get_columns("oauth_states")}
        assert migration_cols == {c.name for c in OAuthState.__table__.columns}

        # Verifier column must be binary, never text.
        cols = {c["name"]: c for c in insp.get_columns("oauth_states")}
        assert "BLOB" in str(cols["code_verifier_ciphertext"]["type"]).upper()

        # No column stores a raw state value.
        assert "state" not in migration_cols
        assert "state_hash" in migration_cols

        index_names = {ix["name"] for ix in insp.get_indexes("oauth_states")}
        assert "ix_oauth_states_expires_at" in index_names
    finally:
        engine.dispose()
