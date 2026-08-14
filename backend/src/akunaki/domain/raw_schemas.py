"""Which raw schema versions are retained without being normalized.

Several provider streams are fetched and kept as immutable raw payloads with
full lineage, but have no normalizer: they carry a vendor score v0.1.0 does not
surface (the product ships exactly one score, and a second would imply a formula
nobody accepted), or they have no canonical detail table yet. Retaining them now
means a normalizer added later can re-run over real history rather than starting
from the day it ships.

Naming the convention here keeps "no normalizer yet" a **declared** decision
rather than something inferred from a missing dispatch branch — and gives the
ingestion adapter and the normalize handler one shared authority, so the two can
never disagree about whether a stream is retained-only.
"""

from __future__ import annotations

# Schema-version prefixes that are deliberately never normalized.
RETAINED_ONLY_PREFIXES = ("oura_raw.", "polar_raw.", "google_health_raw.")


def is_retained_only_schema(schema_version: str) -> bool:
    """Whether a schema version is retained raw with no normalizer.

    Two consequences follow, both handled by the caller:

    - the page is **not** split per record, because per-record revisioning buys
      nothing where no fact hangs off a record, and costs one raw object plus
      one revision per sample on a sampled series;
    - no normalize job is enqueued, because it would only read the revision and
      return.
    """
    return schema_version.startswith(RETAINED_ONLY_PREFIXES)
