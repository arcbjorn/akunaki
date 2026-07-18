"""Durable job worker runtime: claim, execute, heartbeat, settle.

Depends only on domain types and the ``JobRepositoryPort`` protocol; no
SQLAlchemy and no adapter imports, so the whole loop is exercised in tests
against an in-memory fake repository.

The runtime owns *execution policy* (retry classification, backoff, heartbeat
cadence, poll idling, leader-gated reaping). The repository owns *durability*
(atomic CAS transitions, attempt history, dead letters).
"""

from __future__ import annotations

import logging
import random
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from akunaki.application.handlers import HandlerRegistry, default_registry
from akunaki.domain.jobs import JobClaim, JobRole
from akunaki.domain.retry import (
    FailureKind,
    RetryPolicy,
    classify_exception,
    error_class_of,
    redact_error_message,
)
from akunaki.ports.jobs import JobRepositoryPort

logger = logging.getLogger("akunaki.worker")

# Leader lease name guarding scheduler/reaper duties. Only the leader may
# requeue expired leases or dead-letter exhausted ones, so a passive standby
# never reaps behind an active worker's back.
REAPER_LEASE_NAME = "core-reaper"


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    """Tunable execution policy for one worker process."""

    role: JobRole = JobRole.CORE
    lease_ttl: timedelta = timedelta(seconds=30)
    heartbeat_interval: timedelta = timedelta(seconds=10)
    poll_interval: timedelta = timedelta(seconds=1)
    reaper_interval: timedelta = timedelta(seconds=15)
    leader_lease_ttl: timedelta = timedelta(seconds=30)
    claim_limit: int = 32
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)

    def __post_init__(self) -> None:
        if self.lease_ttl < timedelta(seconds=1):
            msg = "lease_ttl must be at least 1 second (second-precision lifecycle)"
            raise ValueError(msg)
        if self.leader_lease_ttl < timedelta(seconds=1):
            msg = "leader_lease_ttl must be at least 1 second"
            raise ValueError(msg)
        if self.heartbeat_interval <= timedelta(0):
            msg = "heartbeat_interval must be positive"
            raise ValueError(msg)
        # A heartbeat that fires no sooner than the lease expires cannot keep
        # the lease alive, which would let another worker steal a running job.
        if self.heartbeat_interval >= self.lease_ttl:
            msg = "heartbeat_interval must be shorter than lease_ttl"
            raise ValueError(msg)
        if self.poll_interval < timedelta(0):
            msg = "poll_interval must be non-negative"
            raise ValueError(msg)
        if self.claim_limit < 1:
            msg = "claim_limit must be >= 1"
            raise ValueError(msg)


@dataclass(slots=True)
class WorkerStats:
    """Counters describing what one run of the loop did."""

    claimed: int = 0
    succeeded: int = 0
    retried: int = 0
    dead_lettered: int = 0
    lease_lost: int = 0
    unhandled_type: int = 0
    requeued_expired: int = 0
    dead_lettered_expired: int = 0
    idle_polls: int = 0


class JobWorker:
    """Single-threaded durable job worker.

    One iteration claims at most one job and runs it to a settled outcome.
    ``run_forever`` repeats until the stop event is set, idling on the poll
    interval when the queue is empty.
    """

    def __init__(
        self,
        repository: JobRepositoryPort,
        *,
        owner: str,
        config: WorkerConfig | None = None,
        registry: HandlerRegistry | None = None,
        stop_event: threading.Event | None = None,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] | None = None,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if not owner.strip():
            msg = "owner must be a non-empty string"
            raise ValueError(msg)
        self._repository = repository
        self._owner = owner
        self._config = config or WorkerConfig()
        self._registry = registry or default_registry()
        self._stop_event = stop_event or threading.Event()
        self._clock = clock
        # Sleeping on the stop event makes shutdown immediate rather than
        # waiting out a full poll interval.
        self._sleep = sleep or self._interruptible_sleep
        self._jitter = jitter
        self.stats = WorkerStats()
        self._leader_fence: int | None = None
        self._next_reap_at: datetime | None = None

    @property
    def owner(self) -> str:
        """Stable identity used for lease ownership and fencing."""
        return self._owner

    def request_stop(self) -> None:
        """Signal cooperative shutdown; the loop exits after the current job settles."""
        self._stop_event.set()

    def _interruptible_sleep(self, seconds: float) -> None:
        self._stop_event.wait(seconds)

    def run_forever(self) -> WorkerStats:
        """Run until stop is requested. Returns accumulated stats."""
        logger.info(
            "worker starting",
            extra={"owner": self._owner, "role": str(self._config.role)},
        )
        while not self._stop_event.is_set():
            worked = self.run_once()
            if not worked and not self._stop_event.is_set():
                self.stats.idle_polls += 1
                self._sleep(self._config.poll_interval.total_seconds())
        logger.info("worker stopped", extra={"owner": self._owner})
        return self.stats

    def run_once(self) -> bool:
        """Perform reaper duties then claim and execute at most one job.

        Returns True when a job was claimed, so the caller can poll again
        immediately instead of idling while work remains.
        """
        self._maybe_reap()

        claim = self._repository.claim_next(
            role=self._config.role,
            owner=self._owner,
            lease_ttl=self._config.lease_ttl,
            now=self._clock(),
            limit=self._config.claim_limit,
        )
        if claim is None:
            return False

        self.stats.claimed += 1
        self._execute(claim)
        return True

    def _execute(self, claim: JobClaim) -> None:
        """Run the handler for ``claim`` and settle the durable lifecycle."""
        handler = self._registry.get(claim.job_type)
        if handler is None:
            # An unregistered type is a deployment/contract error, never a
            # transient one: dead-letter it rather than burning the attempt
            # budget on a handler that will never exist in this process.
            self.stats.unhandled_type += 1
            logger.error(
                "no handler registered for job type",
                extra={"job_id": claim.job_id, "job_type": claim.job_type},
            )
            self._settle_failure(
                claim,
                kind=FailureKind.PERMANENT,
                error_class="UnregisteredJobType",
                message=f"no handler registered for job_type {claim.job_type!r}",
            )
            return

        heartbeat = _Heartbeat(
            repository=self._repository,
            claim=claim,
            owner=self._owner,
            lease_ttl=self._config.lease_ttl,
            interval=self._config.heartbeat_interval,
            clock=self._clock,
        )
        heartbeat.start()
        try:
            handler(claim)
        except BaseException as exc:
            heartbeat.stop()
            kind = classify_exception(exc)
            logger.warning(
                "job attempt failed",
                extra={
                    "job_id": claim.job_id,
                    "job_type": claim.job_type,
                    "error_class": error_class_of(exc),
                    "failure_kind": str(kind),
                },
            )
            self._settle_failure(
                claim,
                kind=kind,
                error_class=error_class_of(exc),
                message=redact_error_message(exc),
            )
            # A cancelled attempt means shutdown is in progress; a genuine
            # KeyboardInterrupt/SystemExit must still terminate the process
            # after the durable record is written.
            if isinstance(exc, KeyboardInterrupt | SystemExit):
                self.request_stop()
                raise
            return
        else:
            heartbeat.stop()

        if heartbeat.lease_lost:
            # The lease expired mid-execution, so another worker may already
            # own this job. Completing under a stale fence would be rejected
            # anyway; record the loss rather than reporting a false success.
            self.stats.lease_lost += 1
            logger.warning(
                "lease lost during execution; not completing",
                extra={"job_id": claim.job_id, "fence_token": claim.fence_token},
            )
            return

        completed = self._repository.complete_job(
            job_id=claim.job_id,
            owner=self._owner,
            fence_token=claim.fence_token,
            now=self._clock(),
        )
        if completed:
            self.stats.succeeded += 1
        else:
            self.stats.lease_lost += 1
            logger.warning(
                "completion rejected by fence",
                extra={"job_id": claim.job_id, "fence_token": claim.fence_token},
            )

    def _settle_failure(
        self,
        claim: JobClaim,
        *,
        kind: FailureKind,
        error_class: str,
        message: str | None,
    ) -> None:
        """Record the failure durably, choosing retry delay by policy."""
        retryable = kind is not FailureKind.PERMANENT
        if kind is FailureKind.CANCELLED:
            # Shutdown is not the job's fault: retry promptly without backoff.
            delay = timedelta(seconds=1)
        else:
            delay = self._config.retry_policy.delay_for_attempt(
                claim.attempts,
                jitter=self._jitter(),
            )

        result = self._repository.fail_job(
            job_id=claim.job_id,
            owner=self._owner,
            fence_token=claim.fence_token,
            retryable=retryable,
            retry_delay=delay,
            error_class=error_class,
            redacted_error_message=message,
            now=self._clock(),
        )
        if result is None:
            # Fence rejected the failure record: the lease was already lost.
            self.stats.lease_lost += 1
            return

        if result.disposition.value == "retry_scheduled":
            self.stats.retried += 1
        else:
            self.stats.dead_lettered += 1

    def _maybe_reap(self) -> None:
        """Run leader-gated expiry reaping when the interval has elapsed."""
        now = self._clock()
        if self._next_reap_at is not None and now < self._next_reap_at:
            return
        self._next_reap_at = now + self._config.reaper_interval

        if not self._ensure_leadership(now):
            return

        self.stats.requeued_expired += self._repository.requeue_expired_leases(now=now)
        self.stats.dead_lettered_expired += self._repository.dead_letter_expired_jobs(now=now)

    def _ensure_leadership(self, now: datetime) -> bool:
        """Acquire or extend the reaper leader lease. False when another owner leads."""
        if self._leader_fence is not None:
            extended = self._repository.heartbeat_leader(
                lease_name=REAPER_LEASE_NAME,
                owner=self._owner,
                fence_token=self._leader_fence,
                lease_ttl=self._config.leader_lease_ttl,
                now=now,
            )
            if extended:
                return True
            # Lost leadership; fall through and try to reacquire cleanly.
            self._leader_fence = None

        leader = self._repository.try_acquire_leader(
            lease_name=REAPER_LEASE_NAME,
            owner=self._owner,
            lease_ttl=self._config.leader_lease_ttl,
            now=now,
        )
        if leader is None:
            return False
        self._leader_fence = leader.fence_token
        return True


class _Heartbeat:
    """Background lease extension for one in-flight job.

    Runs on a daemon thread so a wedged handler can never block interpreter
    exit. Records lease loss so the caller refuses to report false success.
    """

    def __init__(
        self,
        *,
        repository: JobRepositoryPort,
        claim: JobClaim,
        owner: str,
        lease_ttl: timedelta,
        interval: timedelta,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._claim = claim
        self._owner = owner
        self._lease_ttl = lease_ttl
        self._interval = interval.total_seconds()
        self._clock = clock
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self.lease_lost = False

    def start(self) -> None:
        """Begin extending the lease in the background."""
        thread = threading.Thread(
            target=self._run,
            name=f"heartbeat-{self._claim.job_id}",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        """Stop extending and join the background thread."""
        self._done.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5.0)
            self._thread = None

    def _run(self) -> None:
        while not self._done.wait(self._interval):
            try:
                alive = self._repository.heartbeat_job(
                    job_id=self._claim.job_id,
                    owner=self._owner,
                    fence_token=self._claim.fence_token,
                    lease_ttl=self._lease_ttl,
                    now=self._clock(),
                )
            except Exception:
                # A transient DB error must not kill the worker process; the
                # next tick retries, and a truly lost lease is caught by the
                # fenced completion check.
                logger.exception(
                    "heartbeat error",
                    extra={"job_id": self._claim.job_id},
                )
                continue
            if not alive:
                self.lease_lost = True
                logger.warning(
                    "heartbeat rejected; lease no longer held",
                    extra={"job_id": self._claim.job_id},
                )
                return
