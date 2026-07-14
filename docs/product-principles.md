# Product principles

**Status:** Proposed

**Last reviewed:** 2026-07-13

Akunaki is proposed as a personal health intelligence product. Users connect wearable providers, the system ingests and normalizes measurements, a deterministic engine produces inspectable daily health context, and an optional model layer may explain or discuss that context without inventing scores.

These principles govern every design choice in this documentation set. They are not marketing copy; they constrain architecture, data retention, API shape, and UI copy.

---

## 1. Deterministic core over model theater

Scores, baselines, anomalies, and rule recommendations are pure functions of selected canonical facts, formula versions, and effective source policies. Models never invent, average, or override those numbers. See [architecture/health-engine.md](architecture/health-engine.md) and [adr/0002-deterministic-core.md](adr/0002-deterministic-core.md).

## 2. Provenance is product

Every fact and score must answer: which raw revision and payload page, which normalizer version, which source policy, which formula version, which derivation inputs or dependency hash. Users and operators must be able to audit *why* a day looks the way it does. See [adr/0004-versioned-provenance.md](adr/0004-versioned-provenance.md).

## 3. Authoritative sources, never silent averages

Overlapping providers are resolved by inspectable, effective-dated source policies. Conflicts are not averaged. Silent fallback is forbidden. Exactly one authoritative source rule per policy metric family (and optional granularity). Exactly one current `source_selections` decision per provider-independent `grain_key`; alternatives live in `source_selection_candidates` for visibility only (`rank` is display order, never auto-fallback). Session/workout grains use canonical `source_grains` episodes (not vendor ids) and pin the exact membership snapshot via `source_grain_version_id`. See [adr/0005-authoritative-source-policy.md](adr/0005-authoritative-source-policy.md).

## 4. Insufficient is honest

When critical inputs are missing or quality is too low, the engine returns `insufficient` (or partial with explicit confidence and coverage), never a fabricated neutral score. UI copy must say what is missing, not invent reassurance. Renormalization of score weights must disclose available coverage.

## 5. Wellness and performance, not diagnosis

Recommendations are wellness and training-load guidance. ACWR and nutrition associations are descriptive, never injury predictions or medical causation. No design language claims diagnosis, treatment, or clinical decision support.

## 6. Privacy overrides immutability

Raw revisions and canonical facts are append-only for auditability until a user-initiated privacy deletion. Privacy deletion hard-scrubs user-linked rows (privacy tombstones are transient only during the pipeline). Deletion is complete across primary store, queued work, and retention of backups. Two artifacts: minimal non-identifying completion proof, and an access-separated restoration-suppression ledger (HMAC selectors under a dedicated deletion key; retain until backups expire + 30 days, then destroy). Do not claim permanent tenant-lifetime non-linkability. Vendor deletions retain durable vendor tombstones until privacy scrub. See [architecture/security.md](architecture/security.md).

## 7. Models are optional and minimized

The product must be complete with all model providers disabled. Models receive structured summaries, not full raw dumps by default. Mutations through tools require explicit user confirmation. CI must prove the core path with models off. See [testing.md](testing.md).

## 8. Least privilege and tenant isolation

OAuth scopes are minimized per connector. Google Health uses restricted scopes and security review. Every row is tenant-scoped with composite foreign keys and indexes. Sessions are backend-issued. Operator access is audited without logging health values. See [architecture/security.md](architecture/security.md).

## 9. One modular monolith first

MVP ships as a modular monolith: one Python package shared by API and worker, one Next.js PWA, one operational database, one durable job queue with one active worker. Split services only when measured scale triggers fire. See [adr/0001-modular-monolith.md](adr/0001-modular-monolith.md).

## 10. Explain How / Why / What

The primary UX triad is:

| Question | Meaning |
|----------|---------|
| How am I? | Status, score(s), confidence, freshness |
| Why? | Signed factors, sources, baselines, provenance, coverage |
| What should I do? | One global primary recommendation plus supporting detail |

Frontend architecture is built around this triad. See [architecture/frontend.md](architecture/frontend.md).

## 11. Connector honesty

MVP connectors are **Oura**, **Google Health** (`google_health`; Google Health API v4, cloud successor to the legacy Fitbit Web API; current Fitbit-origin path), and **Polar AccessLink**. The legacy Fitbit Web API is **not** an MVP connector (it stops syncing in September 2026). Android Health Connect is on-device and is a future companion bridge, not a server connector. **Apple Health / HealthKit** is deferred to a future **native iOS bridge** (not a server connector; no native app in MVP). Google Fit is not a foundation. Device-specific capability gaps (including Fitbit Air exact device/SKU) are open validation items until proven. Turso remains the selected production store. See [architecture/ingestion-and-sync.md](architecture/ingestion-and-sync.md).

## 12. Ship vertical slices with exit criteria

Roadmap phases retire risk before building features. Phase zero spikes for Turso/Python drivers, concurrency, migrations, and volume are mandatory. Each phase has dependencies and exit criteria. See [roadmap.md](roadmap.md).

---

## Non-goals (MVP)

- Clinical diagnosis, medical device claims, or regulatory compliance badges without legal review
- Multi-region active-active data plane
- Real-time competitive sports coaching with sub-second HR streaming
- DuckDB analytical warehouse in MVP
- Redis or external broker before the documented job-queue scale trigger
- Treating models as a required dependency for core product value
- Using legacy Fitbit Web API or Google Fit as connector foundations
