"""Split a vendor collection page into per-record slices.

Pure: no I/O, no clock. A provider returns a *page* containing many logical
records, but the raw layer's identity is per **record** — the design keys
``raw_objects`` on ``(tenant, provider, stream, vendor_record_id)``, where that
id is "a stable vendor id or hash of natural key".

Splitting here rather than in the normalizer keeps the raw layer faithful:
each logical record gets its own object and its own append-only revision
history, so a vendor correcting one night supersedes only that night, and a
vendor deleting one record tombstones only that record.

Each slice carries the **exact** sub-body for its record, so the transport
layer still stores what actually arrived.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

# Streams whose records carry a stable vendor id, keyed by the field holding it.
# A stream absent from this map falls back to hashing the record body, which is
# still per-record — just not stable across cosmetic vendor changes.
VENDOR_ID_FIELDS = {
    "sleep": "id",
    "daily_sleep": "id",
    "daily_readiness": "id",
    "daily_activity": "id",
    "workout": "id",
}


@dataclass(frozen=True, slots=True)
class RecordSlice:
    """One logical record extracted from a collection page."""

    vendor_record_id: str
    payload_text: str
    content_hash: str
    has_stable_id: bool
    """False when identity is a body hash rather than a vendor-assigned id."""


def split_page(stream: str, payload_text: str) -> list[RecordSlice]:
    """Split a vendor page into per-record slices.

    A page whose shape is not a recognized collection yields a single slice
    covering the whole body, so an unfamiliar stream degrades to the previous
    page-level behavior rather than dropping data.
    """
    try:
        parsed = json.loads(payload_text)
    except ValueError:
        return [_whole_page_slice(stream, payload_text)]

    if not isinstance(parsed, dict):
        return [_whole_page_slice(stream, payload_text)]

    records = parsed.get("data")
    if not isinstance(records, list):
        return [_whole_page_slice(stream, payload_text)]

    slices: list[RecordSlice] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        record_slice = _slice_for(stream, record)
        if record_slice.vendor_record_id in seen:
            # A page repeating one vendor id would otherwise collapse two
            # revisions onto one object; keep the first and skip the rest.
            continue
        seen.add(record_slice.vendor_record_id)
        slices.append(record_slice)
    return slices


def _slice_for(stream: str, record: dict[str, Any]) -> RecordSlice:
    """Build one slice, preferring a vendor-assigned id."""
    # Serialize deterministically so an unchanged record hashes identically
    # regardless of key order in the vendor body.
    body = json.dumps(record, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()

    id_field = VENDOR_ID_FIELDS.get(stream)
    raw_id = record.get(id_field) if id_field else None
    if isinstance(raw_id, str) and raw_id:
        return RecordSlice(
            vendor_record_id=f"{stream}:{raw_id}",
            payload_text=body,
            content_hash=digest,
            has_stable_id=True,
        )
    if isinstance(raw_id, int) and not isinstance(raw_id, bool):
        return RecordSlice(
            vendor_record_id=f"{stream}:{raw_id}",
            payload_text=body,
            content_hash=digest,
            has_stable_id=True,
        )

    # No vendor id: hash of the natural key (the record body itself). Position
    # in the page is deliberately not part of identity — a record must not get
    # a new identity because the vendor reordered a page.
    return RecordSlice(
        vendor_record_id=f"{stream}:hash:{digest}",
        payload_text=body,
        content_hash=digest,
        has_stable_id=False,
    )


def _whole_page_slice(stream: str, payload_text: str) -> RecordSlice:
    """Fallback identity for an unrecognized page shape."""
    digest = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    return RecordSlice(
        vendor_record_id=f"{stream}:page:{digest}",
        payload_text=payload_text,
        content_hash=digest,
        has_stable_id=False,
    )
