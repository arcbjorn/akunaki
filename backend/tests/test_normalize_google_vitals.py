"""The normalize handler dispatches a Google Health heart-summary revision to vitals facts.

Fake ports, like the sleep dispatch test: the ``google_health_vitals.`` prefix
must be matched before the bare ``google_health.`` branch, which would read a
heart summary as a sleep page and write nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from akunaki.application.sync_handlers import NORMALIZE_JOB_TYPE, NormalizeHandler
from akunaki.domain.jobs import JobClaim, JobRole
from akunaki.ports.facts import RevisionBody

T0 = datetime(2026, 11, 9, 5, 0, 0, tzinfo=UTC)

_RHR_PAGE = json.dumps(
    {
        "dataPoints": [
            {
                "dailyRestingHeartRate": {
                    "date": {"year": 2026, "month": 11, "day": 7},
                    "dailyRestingHeartRateMetadata": {"calculationMethod": "WITH_SLEEP"},
                    "beatsPerMinute": "52",
                }
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
        self.vitals: list[Any] = []
        self.schema_versions: list[str] = []

    def write_vitals_fact(
        self, *, fact: Any, schema_version: str, **kwargs: object
    ) -> _FakeOutcome:
        self.vitals.append(fact)
        self.schema_versions.append(schema_version)
        return _FakeOutcome(is_new_version=True)

    def write_sleep_fact(self, **kwargs: object) -> _FakeOutcome:  # pragma: no cover
        raise AssertionError("sleep write must not run for a heart-summary revision")

    def write_workout_fact(self, **kwargs: object) -> _FakeOutcome:  # pragma: no cover
        raise AssertionError("workout write must not run for a heart-summary revision")


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
        leased_until="2026-11-09T06:00:00Z",
        attempts=1,
        max_attempts=5,
        payload_json='{"raw_revision_id":"rev-1"}',
    )


def test_heart_summary_revision_writes_vitals_and_chains_a_recompute() -> None:
    revision = RevisionBody(
        revision_id="rev-1",
        connection_id="conn-1",
        raw_payload_id="pay-1",
        schema_version="google_health_vitals.v4",
        payload_text=_RHR_PAGE,
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

    [fact] = facts.vitals
    assert fact.local_health_day == "2026-11-07"
    assert fact.resting_hr_bpm == 52.0
    # The writer derives the provider from this, so it must travel unchanged.
    assert facts.schema_versions == ["google_health_vitals.v4"]
    assert json.loads(jobs.enqueued[0])["local_health_day"] == "2026-11-07"
