"""The normalize handler dispatches a Polar workout revision to workout facts.

Uses fake ports so the schema-version dispatch is tested in isolation, without a
Polar fetch client.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from akunaki.application.sync_handlers import NORMALIZE_JOB_TYPE, NormalizeHandler
from akunaki.domain.jobs import JobClaim, JobRole
from akunaki.domain.workout_normalizer import WorkoutFact
from akunaki.ports.facts import RevisionBody

T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)

_WORKOUT_PAGE = json.dumps(
    {
        "data": [
            {
                "id": "ex-1",
                "start_time": "2026-07-22T06:00:00+02:00",
                "duration": "PT1H",
                "heart_rate_zones": [
                    {"index": 1, "in_zone": "PT10M"},
                    {"index": 2, "in_zone": "PT20M"},
                    {"index": 3, "in_zone": "PT30M"},
                    {"index": 4, "in_zone": "PT5M"},
                    {"index": 5, "in_zone": "PT2M"},
                ],
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
        self.workouts: list[WorkoutFact] = []

    def write_sleep_fact(self, **kwargs: object) -> _FakeOutcome:  # pragma: no cover
        raise AssertionError("sleep write must not run for a polar revision")

    def write_vitals_fact(self, **kwargs: object) -> _FakeOutcome:  # pragma: no cover
        raise AssertionError("vitals write must not run for a polar revision")

    def write_workout_fact(self, *, fact: WorkoutFact, **kwargs: object) -> _FakeOutcome:
        self.workouts.append(fact)
        return _FakeOutcome(is_new_version=True)


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


def test_polar_revision_writes_workout_facts() -> None:
    revision = RevisionBody(
        revision_id="rev-1",
        connection_id="conn-1",
        raw_payload_id="pay-1",
        schema_version="polar.v1",
        payload_text=_WORKOUT_PAGE,
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

    assert len(facts.workouts) == 1
    # 10*1 + 20*2 + 30*3 + 5*4 + 2*5 = 170.
    assert facts.workouts[0].session_load == 170.0
    assert facts.workouts[0].local_health_day == "2026-07-22"
    # A recompute was chained for the affected day.
    assert json.loads(jobs.enqueued[0])["local_health_day"] == "2026-07-22"
