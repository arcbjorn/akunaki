"""OIDC login state: single-use consumption against real libSQL.

Same callback-security rules as the connector flow, plus a ``nonce`` that
binds the returned ``id_token`` to this specific login attempt.
"""

from __future__ import annotations

import threading
from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.crypto.envelope import KEY_BYTES, EnvelopeSealer
from akunaki.adapters.crypto.oauth import (
    generate_code_verifier,
    generate_nonce,
    generate_state,
    hash_state,
)
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.login_state_repository import LoginStateRepository
from akunaki.adapters.db.models import LoginState
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.oauth import OAuthStateRejection

T0 = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
TTL = timedelta(minutes=10)
REDIRECT = "https://app.example.com/auth/callback"
KEK = b"\x99" * KEY_BYTES
ConstraintError = (IntegrityError, ValueError)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def login_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    db_path = tmp_path / "login.db"
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
def factory(login_db: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(Settings(database_url=login_db))
    try:
        yield create_session_factory(engine)
    finally:
        engine.dispose()


@pytest.fixture
def repository(factory: sessionmaker[Session]) -> LoginStateRepository:
    return LoginStateRepository(factory)


def _sealer() -> EnvelopeSealer:
    return EnvelopeSealer(keys={"v1": KEK}, active_key_version="v1")


def _create(
    repository: LoginStateRepository,
    *,
    state: str,
    nonce: str,
    state_id: str = "login-1",
    redirect_uri: str = REDIRECT,
    now: datetime = T0,
    ttl: timedelta = TTL,
) -> str:
    verifier = generate_code_verifier()
    repository.create(
        state_id=state_id,
        state=state,
        nonce=nonce,
        sealed_verifier=_sealer().seal(verifier.encode(), aad=state_id.encode()),
        redirect_uri=redirect_uri,
        now=now,
        ttl=ttl,
    )
    return verifier


# ---------------------------------------------------------------------------
# Storage secrecy
# ---------------------------------------------------------------------------


def test_raw_state_and_nonce_are_never_stored(
    repository: LoginStateRepository, login_db: str
) -> None:
    state, nonce = generate_state(), generate_nonce()
    verifier = _create(repository, state=state, nonce=nonce)

    engine = create_db_engine(Settings(database_url=login_db))
    try:
        with engine.connect() as conn:
            stored_state, stored_nonce, ciphertext = conn.execute(
                text("SELECT state_hash, nonce_hash, code_verifier_ciphertext FROM login_states")
            ).one()
    finally:
        engine.dispose()

    assert stored_state == hash_state(state)
    assert stored_nonce == hash_state(nonce)
    assert state not in stored_state
    assert nonce not in stored_nonce
    # The PKCE verifier is sealed, not readable from the row.
    assert verifier.encode() not in ciphertext


def test_duplicate_state_is_rejected(repository: LoginStateRepository) -> None:
    state, nonce = generate_state(), generate_nonce()
    _create(repository, state=state, nonce=nonce, state_id="login-1")

    with pytest.raises(ConstraintError):
        _create(repository, state=state, nonce=generate_nonce(), state_id="login-2")


# ---------------------------------------------------------------------------
# Consumption
# ---------------------------------------------------------------------------


def test_valid_consume_returns_verifier_and_nonce_hash(
    repository: LoginStateRepository,
) -> None:
    state, nonce = generate_state(), generate_nonce()
    verifier = _create(repository, state=state, nonce=nonce)

    result = repository.consume(state=state, redirect_uri=REDIRECT, now=T0 + timedelta(minutes=1))

    assert result.ok
    assert result.sealed_verifier is not None
    opened = _sealer().open(result.sealed_verifier, aad=b"login-1").decode()
    assert opened == verifier
    # The raw nonce is never stored, so the caller compares by hash.
    assert result.nonce_hash == hash_state(nonce)


def test_state_is_single_use(repository: LoginStateRepository) -> None:
    """A replayed callback must not obtain the verifier again."""
    state, nonce = generate_state(), generate_nonce()
    _create(repository, state=state, nonce=nonce)

    first = repository.consume(state=state, redirect_uri=REDIRECT, now=T0)
    second = repository.consume(state=state, redirect_uri=REDIRECT, now=T0)

    assert first.ok
    assert second.rejection is OAuthStateRejection.ALREADY_CONSUMED
    assert second.sealed_verifier is None


def test_expired_state_is_rejected(repository: LoginStateRepository) -> None:
    state, nonce = generate_state(), generate_nonce()
    _create(repository, state=state, nonce=nonce, ttl=timedelta(minutes=5))

    result = repository.consume(state=state, redirect_uri=REDIRECT, now=T0 + timedelta(minutes=6))
    assert result.rejection is OAuthStateRejection.EXPIRED


def test_expiry_boundary_is_exclusive(repository: LoginStateRepository) -> None:
    state, nonce = generate_state(), generate_nonce()
    _create(repository, state=state, nonce=nonce, ttl=timedelta(minutes=5))

    result = repository.consume(state=state, redirect_uri=REDIRECT, now=T0 + timedelta(minutes=5))
    assert result.rejection is OAuthStateRejection.EXPIRED


def test_redirect_must_match_exactly(repository: LoginStateRepository) -> None:
    state, nonce = generate_state(), generate_nonce()
    _create(repository, state=state, nonce=nonce)

    for candidate in (
        REDIRECT + "/",
        REDIRECT + "?x=1",
        REDIRECT.replace("https", "http"),
        "https://evil.test/callback",
    ):
        result = repository.consume(state=state, redirect_uri=candidate, now=T0)
        assert result.rejection is OAuthStateRejection.REDIRECT_MISMATCH, candidate


def test_failed_attempt_does_not_burn_the_state(
    repository: LoginStateRepository,
) -> None:
    """Otherwise a wrong redirect becomes a denial-of-service vector."""
    state, nonce = generate_state(), generate_nonce()
    _create(repository, state=state, nonce=nonce)

    bad = repository.consume(state=state, redirect_uri="https://evil.test/cb", now=T0)
    good = repository.consume(state=state, redirect_uri=REDIRECT, now=T0)

    assert bad.rejection is OAuthStateRejection.REDIRECT_MISMATCH
    assert good.ok


def test_unknown_state_is_rejected(repository: LoginStateRepository) -> None:
    _create(repository, state=generate_state(), nonce=generate_nonce())
    result = repository.consume(state=generate_state(), redirect_uri=REDIRECT, now=T0)

    assert result.rejection is OAuthStateRejection.NOT_FOUND


def test_empty_inputs_are_rejected(repository: LoginStateRepository) -> None:
    assert repository.consume(state="", redirect_uri=REDIRECT, now=T0).rejection is (
        OAuthStateRejection.NOT_FOUND
    )
    assert repository.consume(state="x", redirect_uri="", now=T0).rejection is (
        OAuthStateRejection.NOT_FOUND
    )


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_consume_yields_exactly_one_winner(login_db: str) -> None:
    """Two simultaneous callbacks: only one may obtain the verifier."""
    n_threads = 4
    state, nonce = generate_state(), generate_nonce()

    engine = create_db_engine(Settings(database_url=login_db))
    try:
        _create(LoginStateRepository(create_session_factory(engine)), state=state, nonce=nonce)
    finally:
        engine.dispose()

    barrier = threading.Barrier(n_threads)
    successes: list[bool] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def attempt() -> None:
        worker_engine = create_db_engine(Settings(database_url=login_db))
        try:
            repo = LoginStateRepository(create_session_factory(worker_engine))
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
    assert successes.count(True) == 1, f"expected one winner, got {successes}"


# ---------------------------------------------------------------------------
# Purge and validation
# ---------------------------------------------------------------------------


def test_purge_removes_expired_and_keeps_live(
    repository: LoginStateRepository, factory: sessionmaker[Session]
) -> None:
    _create(
        repository,
        state=generate_state(),
        nonce=generate_nonce(),
        state_id="old",
        ttl=timedelta(minutes=1),
    )
    _create(
        repository,
        state=generate_state(),
        nonce=generate_nonce(),
        state_id="live",
        ttl=timedelta(hours=2),
    )

    removed = repository.purge_expired(now=T0 + timedelta(minutes=30))

    assert removed == 1
    with factory() as session:
        assert list(session.scalars(select(LoginState.id)).all()) == ["live"]


def test_create_validates_arguments(repository: LoginStateRepository) -> None:
    sealed = _sealer().seal(b"verifier")
    base = {
        "state_id": "s1",
        "state": "raw-state",
        "nonce": "raw-nonce",
        "sealed_verifier": sealed,
        "redirect_uri": REDIRECT,
        "now": T0,
        "ttl": TTL,
    }
    for field in ("state_id", "state", "nonce", "redirect_uri"):
        with pytest.raises(ValueError, match=f"{field} must be non-empty"):
            repository.create(**{**base, field: ""})  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="ttl must be at least one second"):
        repository.create(**{**base, "ttl": timedelta(0)})  # type: ignore[arg-type]


def test_login_state_has_no_tenant_column(login_db: str) -> None:
    """Login happens before a tenant is known; requiring one would be wrong."""
    from sqlalchemy import inspect

    engine = create_db_engine(Settings(database_url=login_db))
    try:
        columns = {c["name"] for c in inspect(engine).get_columns("login_states")}
    finally:
        engine.dispose()

    assert "tenant_id" not in columns
    assert {"state_hash", "nonce_hash"} <= columns
