"""Per-record page splitting: identity and exact sub-bodies.

Raw-layer identity is per **record**, so a vendor correcting one night must
supersede only that night. These are pure tests; no database needed.
"""

from __future__ import annotations

import json

from akunaki.domain.record_split import split_page


def _page(*records: dict[str, object]) -> str:
    return json.dumps({"data": list(records)})


def test_page_splits_into_one_slice_per_record() -> None:
    slices = split_page("sleep", _page({"id": "s1"}, {"id": "s2"}, {"id": "s3"}))

    assert [s.vendor_record_id for s in slices] == ["sleep:s1", "sleep:s2", "sleep:s3"]
    assert all(s.has_stable_id for s in slices)


def test_each_slice_carries_only_its_own_record() -> None:
    """A revision's body must be its record, not the whole page."""
    slices = split_page("sleep", _page({"id": "s1", "score": 82}, {"id": "s2", "score": 70}))

    bodies = [json.loads(s.payload_text) for s in slices]
    assert bodies[0]["id"] == "s1"
    assert bodies[1]["id"] == "s2"
    # No page envelope leaks into a record slice.
    assert all("data" not in body for body in bodies)


def test_identity_is_stable_across_page_reordering() -> None:
    """A vendor reordering a page must not re-identify its records."""
    first = split_page("sleep", _page({"id": "s1"}, {"id": "s2"}))
    reordered = split_page("sleep", _page({"id": "s2"}, {"id": "s1"}))

    assert {s.vendor_record_id for s in first} == {s.vendor_record_id for s in reordered}


def test_content_hash_ignores_key_order() -> None:
    a = split_page("sleep", json.dumps({"data": [{"id": "s1", "a": 1, "b": 2}]}))
    b = split_page("sleep", json.dumps({"data": [{"b": 2, "id": "s1", "a": 1}]}))

    assert a[0].content_hash == b[0].content_hash


def test_changed_record_changes_only_its_own_hash() -> None:
    before = split_page("sleep", _page({"id": "s1", "score": 82}, {"id": "s2", "score": 70}))
    after = split_page("sleep", _page({"id": "s1", "score": 90}, {"id": "s2", "score": 70}))

    assert before[0].content_hash != after[0].content_hash
    # The untouched night must not be re-revisioned.
    assert before[1].content_hash == after[1].content_hash


def test_integer_vendor_ids_are_supported() -> None:
    [slice_] = split_page("workout", _page({"id": 12345}))
    assert slice_.vendor_record_id == "workout:12345"
    assert slice_.has_stable_id is True


def test_records_without_a_vendor_id_fall_back_to_body_hash() -> None:
    slices = split_page("sleep", _page({"score": 82}, {"score": 70}))

    assert all(s.vendor_record_id.startswith("sleep:hash:") for s in slices)
    assert all(not s.has_stable_id for s in slices)
    # Still per-record: two distinct records, two distinct identities.
    assert slices[0].vendor_record_id != slices[1].vendor_record_id


def test_boolean_id_is_not_treated_as_an_integer_id() -> None:
    [slice_] = split_page("sleep", _page({"id": True}))
    assert slice_.has_stable_id is False


def test_duplicate_vendor_ids_in_one_page_are_collapsed() -> None:
    """Two revisions must never land on one object from a single page."""
    slices = split_page("sleep", _page({"id": "s1", "v": 1}, {"id": "s1", "v": 2}))

    assert len(slices) == 1
    assert json.loads(slices[0].payload_text)["v"] == 1


def test_unknown_stream_still_splits_per_record() -> None:
    slices = split_page("mystery", _page({"foo": 1}, {"foo": 2}))
    assert len(slices) == 2
    assert all(not s.has_stable_id for s in slices)


def test_non_collection_shapes_degrade_to_a_whole_page_slice() -> None:
    """An unfamiliar shape must not silently drop data."""
    for payload in ("not json", json.dumps([1, 2]), json.dumps({"next_token": "x"})):
        slices = split_page("sleep", payload)
        assert len(slices) == 1
        assert slices[0].vendor_record_id.startswith("sleep:page:")
        assert slices[0].payload_text == payload


def test_empty_page_yields_no_slices() -> None:
    assert split_page("sleep", _page()) == []


def test_non_object_entries_are_skipped() -> None:
    slices = split_page("sleep", json.dumps({"data": ["nope", 5, {"id": "s1"}]}))
    assert len(slices) == 1
    assert slices[0].vendor_record_id == "sleep:s1"
