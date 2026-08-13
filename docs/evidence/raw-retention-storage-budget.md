# Raw retention storage budget (measured 2026-08-13)

Whether to retain every provider stream at full resolution — or to sample the
high-frequency ones — was treated as an open risk. It is not. This page records
the measurement so the question is settled with numbers rather than re-argued
from intuition.

**Conclusion: retain everything at full resolution.** Total cost for all three
providers, every stream, including per-sample heart rate, is **~16.5 MB/year on
disk**. A decade of complete history is under 200 MB.

## Method

Bytes of vendor JSON were measured against a **real linked account** over a
7-day window (28 days for Polar/Google, which use different windowing), not
estimated from record counts. Storage amplification was measured from the live
database rather than assumed.

## Vendor bytes per day

| Provider | Stream | B/day | Note |
|----------|--------|------:|------|
| Oura | `heartrate` | 12,399 | ~857 samples/week; the largest single stream |
| Oura | `daily_activity` | 6,929 | larger than sleep: `class_5_min` string + `met` array |
| Oura | `sleep` | 4,152 | the only stream ingested before this work |
| Oura | `daily_readiness` | 400 | vendor score + contributors |
| Oura | `daily_sleep` | 246 | vendor score |
| Oura | `daily_stress` | 141 | |
| Oura | `daily_cardiovascular_age` | 127 | |
| Oura | `vO2_max` | 21 | sparse |
| Oura | `sleep_time` | 4 | sparse |
| Google Health | `active-zone-minutes` | 136 | per-minute points, but few of them |
| Polar | `users/activities` | 18 | |
| Polar | `exercises` | 0 | empty within the 30-day horizon |

## Storage amplification

The raw layer deliberately stores each payload **twice**: once whole as a
transport row (`raw_payload.payload_json`, retained for audit) and once split
per record (`raw_revisions.slice_json`, which carries logical identity). That
duplication is a design property, not waste — it is what makes crash replay safe
and per-record versioning possible.

Measured on the live database:

| Quantity | Bytes |
|----------|------:|
| `raw_payload.payload_json` | 870,722 |
| `raw_revisions.slice_json` | 224,999 |
| **Database file** | **2,113,536** |

→ **1.93× on disk per raw vendor byte**, covering both copies plus indexes.

## Projection

| Scope | KB/day | MB/yr raw | **MB/yr on disk** |
|-------|-------:|----------:|------------------:|
| Core (sleep + activity, all providers) | 11.0 | 3.9 | **7.5** |
| \+ vendor scores retained raw | 11.9 | 4.2 | **8.2** |
| \+ heart-rate series | 24.0 | 8.6 | **16.5** |

Heart rate alone costs 8.3 MB/yr — roughly equal to everything else combined,
**not** dominant. Sampling it would halve an already trivial number while
permanently discarding the highest-resolution signal the system receives.

## Why this matters beyond the number

Raw retention is not a cache. Facts are versioned and re-derivable, so a
normalizer fixed later can be re-run over retained payloads to produce corrected
facts — but only for data that was kept. Two connector bugs found on 2026-08-13
(a rejected Google filter member; an empty page dead-lettering the chain) are
exactly the case this protects: had the raw layer been sampled, the days lost to
those bugs would be unrecoverable. Discarding raw data trades a few megabytes
for permanent, silent gaps.

The honest limit: this budget is one user. It scales linearly per tenant, so a
multi-tenant deployment multiplies it — still small, but the storage question
would need re-asking at a very different order of magnitude, and against a
server-grade store rather than a local libSQL file.
