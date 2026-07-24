"""The normalize handler dispatches a Google Health revision to sleep facts.

Uses fake ports so the ``google_health.`` schema-version dispatch is tested in
isolation, without a Google Health fetch client. Google Health sleep segments
carry no HRV/RHR, so the vitals write must never run for these revisions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from akunaki.application.sync_handlers import NORMALIZE_JOB_TYPE, NormalizeHandler
from akunaki.domain.jobs import JobClaim, JobRole
from akunaki.ports.facts import RevisionBody

T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)

_SLEEP_PAGE = json.dumps(
    {
        "dataPoints": [
            {
                "startTime": "2026-07-22T00:00:00+02:00",
                "endTime": "2026-07-22T04:00:00+02:00",
                "sleepType": "SLEEP_STAGE_LIGHT",
            },
            {
                "startTime": "2026-07-22T04:00:00+02:00",
                "endTime": "2026-07-22T07:00:00+02:00",
                "sleepType": "SLEEP_STAGE_DEEP",
            },
        ]
    }
)


@dataclass
class _FakeOutcome:
    is_new_version: bool = True


class _FakeRevisions:
    def __init__(self, body: RevisionBody) -> None:
        self._body = body

    def get_revision(self, *, revision_id: str) -> RevisionBody | None:
        return self._body


class _FakeFacts:
    def __init__(self) -> None:
        self.sleep: list[Any] = []

    def write_sleep_fact(self, *, fact: Any, **kwargs: object) -> _FakeOutcome:
        self.sleep.append(fact)
        return _FakeOutcome(is_new_version=True)

    def write_vitals_fact(self, **kwargs: object) -> _FakeOutcome:  # pragma: no cover
        raise AssertionError("vitals write must not run for a google_health revision")

    def write_workout_fact(self, **kwargs: object) -> _FakeOutcome:  # pragma: no cover
        raise AssertionError("workout write must not run for a google_health revision")


class _FakeJobs:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    def enqueue_job(self, *, payload_json: str, **kwargs: object) -> object:
        self.enqueued.append(payload_json)
        return object()


def _claim() -> JobClaim:
    return JobClaim(
        job_id="norm-1",
        tenant_id="tenant-1",
        role=JobRole.CORE,
        job_type=NORMALIZE_JOB_TYPE,
        owner="worker-1",
        fence_token=1,
        leased_until="2026-07-22T13:00:00Z",
        attempts=1,
        max_attempts=5,
        payload_json='{"raw_revision_id":"rev-1"}',
    )


def test_google_health_revision_writes_sleep_facts() -> None:
    revision = RevisionBody(
        revision_id="rev-1",
        connection_id="conn-1",
        raw_payload_id="pay-1",
        schema_version="google_health.v4",
        payload_text=_SLEEP_PAGE,
        is_tombstone=False,
    )
    facts = _FakeFacts()
    jobs = _FakeJobs()
    handler = NormalizeHandler(
        revisions=_FakeRevisions(revision),
        facts=facts,  # type: ignore[arg-type]
        jobs=jobs,  # type: ignore[arg-type]
        new_id=lambda: "id-1",
        clock=lambda: T0,
    )

    handler(_claim())

    assert len(facts.sleep) == 1
    fact = facts.sleep[0]
    assert fact.local_health_day == "2026-07-22"
    # 240 light + 180 deep = 420 sleep minutes for the aggregated night.
    assert fact.duration_min == 420.0
    assert fact.vendor_record_id == "google_sleep:2026-07-22"
    # A recompute was chained for the affected day.
    assert json.loads(jobs.enqueued[0])["local_health_day"] == "2026-07-22"
