"""Production worker wiring: build the full product handler registry.

This is the assembly point the worker entrypoint uses. It constructs every
product job handler from concrete adapters (per-provider fetch clients, the
envelope sealer, and the DB repositories) and returns a ``HandlerRegistry``
plus the periodic schedules the leader should fire.

The sync handlers are fixed per provider (one fetch client + stream config
each), so a single ``ProviderDispatchSyncHandler`` per job type routes a job to
the handler for its connection's provider. That keeps one worker able to sync
every provider without a provider-specific job type.

Lives in ``adapters`` because it imports concrete adapters and application
handlers; the domain and ports stay free of this composition.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.connectors.google_health_fetch import GoogleHealthFetchClient
from akunaki.adapters.connectors.oauth_client_factory import build_oauth_client
from akunaki.adapters.connectors.oura_fetch import OuraFetchClient
from akunaki.adapters.connectors.polar_fetch import PolarFetchClient
from akunaki.adapters.crypto.config import build_sealer
from akunaki.adapters.crypto.sessions import generate_provenance_token
from akunaki.adapters.db.anomaly_repository import AnomalyRepository
from akunaki.adapters.db.audit_repository import AuditRepository
from akunaki.adapters.db.checkin_repository import CheckInRepository
from akunaki.adapters.db.confirmation_repository import ConfirmationRepository
from akunaki.adapters.db.connection_repository import ConnectionRepository
from akunaki.adapters.db.derivation_repository import DerivationRepository
from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.adapters.db.ingestion_repository import IngestionRepository, RevisionReader
from akunaki.adapters.db.job_repository import JobRepository
from akunaki.adapters.db.login_state_repository import LoginStateRepository
from akunaki.adapters.db.oauth_state_repository import OAuthStateRepository
from akunaki.adapters.db.score_repository import ScoreRepository
from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.adapters.db.source_selection_repository import SourceSelectionRepository
from akunaki.adapters.db.status_repository import SystemCheckRepository
from akunaki.adapters.db.sync_run_repository import SyncRunRepository
from akunaki.adapters.db.unit_of_work import FencedUnitOfWork
from akunaki.application.anomaly_tracker import AnomalyTracker
from akunaki.application.audit_handlers import AUDIT_VERIFY_JOB_TYPE, AuditVerifyHandler
from akunaki.application.handlers import HandlerRegistry
from akunaki.application.recovery_inputs import RecoveryInputService
from akunaki.application.recovery_surface import RecoverySurfaceService
from akunaki.application.retention_handlers import (
    RETENTION_SWEEP_JOB_TYPE,
    RetentionSweepHandler,
)
from akunaki.application.score_handlers import (
    SCORE_RECOMPUTE_JOB_TYPE,
    ScoreRecomputeHandler,
)
from akunaki.application.sync_handlers import (
    INCREMENTAL_SYNC_JOB_TYPE,
    INITIAL_SYNC_JOB_TYPE,
    NORMALIZE_JOB_TYPE,
    RECONCILE_SWEEP_JOB_TYPE,
    IncrementalSyncHandler,
    InitialSyncHandler,
    NormalizeHandler,
    ProviderDispatchSyncHandler,
    ReconcileSweepHandler,
    SyncConfig,
    streams_for_provider,
    sync_config_for_provider,
)
from akunaki.config import Settings
from akunaki.ports.fetch import ConnectorFetchPort

# Providers with a fetch client, mapped to a constructor. A provider absent here
# has no sync handler and its sync jobs fail permanently (a wiring error).
_FETCH_CLIENTS: dict[str, Callable[[], ConnectorFetchPort]] = {
    "oura": OuraFetchClient,
    "polar": PolarFetchClient,
    "google_health": GoogleHealthFetchClient,
}


def _new_id() -> str:
    return str(uuid.uuid4())


def sync_configs(settings: Settings) -> dict[tuple[str, str], SyncConfig]:
    """Every ``(provider, stream)`` the worker syncs, with its backfill config.

    Named and public because this is where the deployment's history window
    stops being configuration and becomes a sync's behaviour: every handler the
    registry builds takes its ``lookback_days`` from here, so a setting that
    reached some streams and not others would be worse than none at all.
    """
    return {
        (provider, stream): sync_config_for_provider(
            provider, stream=stream, lookback_days=settings.lookback_days
        )
        for provider in _FETCH_CLIENTS
        for stream, _schema in streams_for_provider(provider)
    }


def build_registry(settings: Settings, session_factory: sessionmaker[Session]) -> HandlerRegistry:
    """Construct the full product handler registry for a worker process."""
    sealer = build_sealer(settings)
    connections = ConnectionRepository(session_factory)
    ingestion = IngestionRepository(session_factory)
    facts = FactRepository(session_factory)
    jobs = JobRepository(session_factory)
    sync_runs = SyncRunRepository(session_factory)

    # One initial + one incremental handler per (provider, stream), each fixed
    # to that provider's fetch client and that stream's schema config. A
    # provider with several streams therefore gets several handlers, and the
    # dispatcher routes on both keys — so one stream failing cannot stop the
    # others, and each keeps its own cursor and retry budget.
    initial: dict[tuple[str, str], Callable[..., None]] = {}
    incremental: dict[tuple[str, str], Callable[..., None]] = {}
    # The deployment's window reaches the handlers here and nowhere else, so
    # there is one source of truth for how far back a backfill reaches.
    configs = sync_configs(settings)
    for provider, make_client in _FETCH_CLIENTS.items():
        # The OAuth client lets a sync renew an expiring access token before
        # spending a request on it. A provider whose credentials are not
        # configured gets None, and simply never refreshes — the connection then
        # behaves exactly as it did before refresh existed.
        oauth_config = settings.connector_oauth(provider)
        oauth_client = build_oauth_client(provider, oauth_config) if oauth_config else None
        for stream, _schema in streams_for_provider(provider):
            config = configs[provider, stream]
            backfill = InitialSyncHandler(
                fetch_client=make_client(),
                ingestion=ingestion,
                connections=connections,
                sealer=sealer,
                new_id=_new_id,
                config=config,
                sync_runs=sync_runs,
                oauth_client=oauth_client,
            )
            initial[provider, stream] = backfill
            incremental[provider, stream] = IncrementalSyncHandler(
                backfill=backfill,
                cursors=ingestion,
                config=config,
            )

    normalize = NormalizeHandler(
        revisions=RevisionReader(session_factory),
        facts=facts,
        jobs=jobs,
        new_id=_new_id,
        sleep_providers=facts,
        selections=SourceSelectionRepository(session_factory),
    )
    recompute = ScoreRecomputeHandler(
        recovery=RecoverySurfaceService(
            inputs=RecoveryInputService(
                features=facts, subjective=CheckInRepository(session_factory)
            )
        ),
        scores=ScoreRepository(session_factory),
        new_id=_new_id,
        inputs=RecoveryInputService(features=facts, subjective=CheckInRepository(session_factory)),
        tracker=AnomalyTracker(store=AnomalyRepository(session_factory), new_id=_new_id),
        derivations=DerivationRepository(session_factory),
        generate_token=generate_provenance_token,
        unit_of_work=FencedUnitOfWork(session_factory),
    )
    reconcile = ReconcileSweepHandler(
        connections=connections,
        jobs=jobs,
        new_id=_new_id,
    )

    return HandlerRegistry(
        {
            INITIAL_SYNC_JOB_TYPE: ProviderDispatchSyncHandler(
                connections=connections, handlers=initial
            ),
            INCREMENTAL_SYNC_JOB_TYPE: ProviderDispatchSyncHandler(
                connections=connections, handlers=incremental
            ),
            NORMALIZE_JOB_TYPE: normalize,
            SCORE_RECOMPUTE_JOB_TYPE: recompute,
            RECONCILE_SWEEP_JOB_TYPE: reconcile,
            AUDIT_VERIFY_JOB_TYPE: AuditVerifyHandler(
                audit=AuditRepository(session_factory),
                checks=SystemCheckRepository(session_factory),
            ),
            RETENTION_SWEEP_JOB_TYPE: RetentionSweepHandler(
                stores={
                    "sessions": SessionRepository(session_factory),
                    "login_states": LoginStateRepository(session_factory),
                    "oauth_states": OAuthStateRepository(session_factory),
                    "tool_confirmations": ConfirmationRepository(session_factory),
                },
            ),
        }
    )
