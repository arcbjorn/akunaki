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
from akunaki.adapters.connectors.oura_fetch import OuraFetchClient
from akunaki.adapters.connectors.polar_fetch import PolarFetchClient
from akunaki.adapters.crypto.config import build_sealer
from akunaki.adapters.crypto.sessions import generate_provenance_token
from akunaki.adapters.db.anomaly_repository import AnomalyRepository
from akunaki.adapters.db.checkin_repository import CheckInRepository
from akunaki.adapters.db.connection_repository import ConnectionRepository
from akunaki.adapters.db.derivation_repository import DerivationRepository
from akunaki.adapters.db.fact_repository import FactRepository
from akunaki.adapters.db.ingestion_repository import IngestionRepository, RevisionReader
from akunaki.adapters.db.job_repository import JobRepository
from akunaki.adapters.db.score_repository import ScoreRepository
from akunaki.adapters.db.source_selection_repository import SourceSelectionRepository
from akunaki.adapters.db.unit_of_work import FencedUnitOfWork
from akunaki.application.anomaly_tracker import AnomalyTracker
from akunaki.application.handlers import HandlerRegistry
from akunaki.application.recovery_inputs import RecoveryInputService
from akunaki.application.recovery_surface import RecoverySurfaceService
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


def build_registry(settings: Settings, session_factory: sessionmaker[Session]) -> HandlerRegistry:
    """Construct the full product handler registry for a worker process."""
    sealer = build_sealer(settings)
    connections = ConnectionRepository(session_factory)
    ingestion = IngestionRepository(session_factory)
    facts = FactRepository(session_factory)
    jobs = JobRepository(session_factory)

    # One initial + one incremental handler per provider, each fixed to that
    # provider's fetch client and stream/schema config.
    initial: dict[str, Callable[..., None]] = {}
    incremental: dict[str, Callable[..., None]] = {}
    for provider, make_client in _FETCH_CLIENTS.items():
        config = sync_config_for_provider(provider)
        backfill = InitialSyncHandler(
            fetch_client=make_client(),
            ingestion=ingestion,
            connections=connections,
            sealer=sealer,
            new_id=_new_id,
            config=config,
        )
        initial[provider] = backfill
        incremental[provider] = IncrementalSyncHandler(
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
        }
    )
