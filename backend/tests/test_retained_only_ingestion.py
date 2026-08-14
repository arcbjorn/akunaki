"""Retained-only streams are ingested whole, and queue no normalize job.

Both properties exist for one reason: nothing normalizes these streams, so the
work per-record splitting and normalize jobs would do is guaranteed to be
useless — and on a sampled series it is not cheap. Oura's heart rate is one
reading every ~15 seconds; splitting it produced a raw object, a revision, and a
no-op normalize job **per sample**: 4,406 revisions and 4,049 permanently
pending jobs from a single month, inflating the database roughly eightfold over
the whole rest of the data combined.
"""

from __future__ import annotations

import json

from akunaki.domain.raw_schemas import is_retained_only_schema
from akunaki.domain.record_split import split_page, whole_page_slice

_SAMPLES = json.dumps(
    {
        "data": [
            {"bpm": 74, "timestamp": "2026-07-15T04:29:00.000Z", "source": "awake"},
            {"bpm": 71, "timestamp": "2026-07-15T04:29:15.000Z", "source": "awake"},
            {"bpm": 69, "timestamp": "2026-07-15T04:29:30.000Z", "source": "awake"},
        ]
    }
)


def test_retained_prefixes_are_recognized() -> None:
    assert is_retained_only_schema("oura_raw.heartrate.v2") is True
    assert is_retained_only_schema("polar_raw.sleep.v1") is True
    assert is_retained_only_schema("google_health_raw.anything.v4") is True


def test_normalized_schemas_are_not_retained_only() -> None:
    """The carve-out must stay narrow: a normalized stream still splits."""
    for schema in (
        "oura.v2",
        "oura_activity.v2",
        "polar.v1",
        "polar_activity.v1",
        "google_health.v4",
        "google_health_activity.v4",
    ):
        assert is_retained_only_schema(schema) is False


def test_a_sampled_page_would_split_per_sample() -> None:
    """This is the cost being avoided, pinned so the scale stays visible."""
    assert len(split_page("heartrate", _SAMPLES)) == 3


def test_whole_page_slice_keeps_one_record_per_page() -> None:
    """A retained page is one slice, whatever it contains."""
    slices = [whole_page_slice("heartrate", _SAMPLES)]

    assert len(slices) == 1
    assert slices[0].payload_text == _SAMPLES
    # Keyed by the page's own hash: no vendor id is claimed for it.
    assert slices[0].vendor_record_id.startswith("heartrate:page:")
    assert slices[0].has_stable_id is False


def test_whole_page_identity_is_stable_across_identical_pages() -> None:
    """Re-fetching an unchanged page must dedupe, not append a revision."""
    first = whole_page_slice("heartrate", _SAMPLES)
    second = whole_page_slice("heartrate", _SAMPLES)

    assert first.content_hash == second.content_hash
    assert first.vendor_record_id == second.vendor_record_id


def test_a_changed_page_gets_a_new_identity() -> None:
    """A page with a new sample really is new data, and must revision."""
    changed = json.dumps({"data": [{"bpm": 99, "timestamp": "2026-07-15T04:30:00.000Z"}]})

    assert (
        whole_page_slice("heartrate", _SAMPLES).content_hash
        != whole_page_slice("heartrate", changed).content_hash
    )
