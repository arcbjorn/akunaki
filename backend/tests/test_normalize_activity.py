"""The normalize handler dispatches a Google activity revision to activity facts.

Uses fake ports so the ``google_activity.`` schema-version dispatch is tested in
isolation. Activity is its own stream — sleep/vitals/workout writes must not run.
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

_ACTIVITY_PAGE = json.dumps(
    {
        "dataPoints": [
            {
                "startTime": "2026-07-22T00:00:00+02:00",
                "endTime": "2026-07-23T00:00:00+02:00",
                "steps": 8500,
                "activeMinutes": 42.5,
            }
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
        self.activity: list[Any] = []

    def write_activity_fact(self, *, fact: Any, **kwargs: object) -> _FakeOutcome:
        self.activity.append(fact)
        return _FakeOutcome(is_new_version=True)

    def write_sleep_fact(self, **kwargs: object) -> _FakeOutcome:  # pragma: no cover
        raise AssertionError("sleep write must not run for an activity revision")

    def write_vitals_fact(self, **kwargs: object) -> _FakeOutcome:  # pragma: no cover
        raise AssertionError("vitals write must not run for an activity revision")

    def write_workout_fact(self, **kwargs: object) -> _FakeOutcome:  # pragma: no cover
        raise AssertionError("workout write must not run for an activity revision")


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


def test_activity_revision_writes_activity_facts() -> None:
    revision = RevisionBody(
        revision_id="rev-1",
        connection_id="conn-1",
        raw_payload_id="pay-1",
        schema_version="google_activity.v1",
        payload_text=_ACTIVITY_PAGE,
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

    assert len(facts.activity) == 1
    fact = facts.activity[0]
    assert fact.local_health_day == "2026-07-22"
    assert fact.steps == 8500
    # A recompute was chained for the affected day.
    assert json.loads(jobs.enqueued[0])["local_health_day"] == "2026-07-22"
