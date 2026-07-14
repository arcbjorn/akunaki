# ADR 0005: Authoritative source policy

**Status:** Proposed

**Last reviewed:** 2026-07-13

## Context

Users may connect Oura, Google Health, and Polar simultaneously. Metrics overlap (sleep, steps, workouts). Averaging conflicts creates non-physical values and hides disagreement. Silent fallback to a secondary provider makes scores jump without explanation when the primary reconnects. As of July 2026, the cloud path for Fitbit-origin daytime data is **Google Health API v4** (`google_health`), not the legacy Fitbit Web API (which stops syncing in September 2026). Google Fit is not a foundation. Android Health Connect remains a separate on-device future bridge.

## Decision

Use **inspectable, effective-dated, versioned** source policies with **required `source_selections`**: exactly **one current versioned decision** per non-null **provider-independent `grain_key`** (partial unique on `tenant_id + metric_family + granularity + grain_key`). Granularity ∈ {`daily_metric`, `interval`, `session`, `workout`}.

**Grain-key semantics (do not conflate):**

- **Session/workout** grain keys are **stable, provider-independent UUIDs** (`source_grains.id`)—**not** content-deterministic. A newly discovered distinct episode allocates a new identity; once known, the id is application-stable across membership versions.
- **Daily/interval** grain keys are **content-derived** (local health day; UTC interval pair).
- **Episode-matching membership** is deterministic for a complete input set + `match_algorithm_version` (identical who-belongs-together), which is separate from UUID allocation.

- **One authoritative source rule** per `(policy_id, metric_family[, granularity if needed])`. Do **not** encode multiple authoritative providers as ranked rows for the same key.
- `selected_fact_record_id` is a real **nullable FK** to `fact_records`: non-null when a fact is selected; **null only** for `selection_reason = missing_authoritative` with required **`missing_reason`**.
- Alternatives live only in **`source_selection_candidates`** (real `fact_record_id` FK, **`rank`**, eligibility/reason)—**never averaged**, **never auto-fallback**. Candidate **display order** is only `source_selection_candidates.rank`.
- Session/workout grains use stable **`source_grains`** episode identity headers; membership/interval snapshots live in **`source_grain_versions`** (with pinned **`match_algorithm_version`**, `version_n`, `is_current`); **`source_grain_members`** real FKs attach to **`source_grain_version_id`**. Selection `grain_key` / `source_grain_id` reference the **stable** `source_grains.id`. Session/workout selections also require **`source_grain_version_id`** (real FK) pinning the exact membership snapshot; the version row **must** belong to that stable `source_grain_id`; the selected fact (when non-null) **must** be a member of that version; eligible candidates are members of that version unless explicitly ineligible near-matches with reason. Daily/interval leave grain FKs null. **Never** use vendor session/workout ids as grain keys (that blocks cross-provider same-episode candidates). Daily grains remain metric + date.
- Multiple sleeps and workouts are first-class separate grains. No drifting `is_authoritative` column on fact rows.

| Metric family | Authoritative provider |
|---------------|------------------------|
| Sleep sessions and sleep stages | Oura |
| Overnight HRV, overnight RHR, overnight temperature, overnight respiration | Oura |
| Daytime heart rate, steps, activity, daytime SpO2, daytime temperature, other daytime signals | `google_health` with **google-wearables** Fitbit-origin DataSource family (UI may say Fitbit via Google Health; preserve origin/device) |
| Workout HR, workout sessions, swimming, intensity, inputs to **internally calculated** load | Polar |

Hard rules:

1. **Keep alternatives** in `source_selection_candidates` and visible in Why / data-quality UI.
2. **Never average** conflicts for the same metric family and `grain_key`.
3. **Do not silently fall back** when the authoritative source is missing; write `missing_authoritative` + `missing_reason`; engine may return `insufficient` or partial with explicit reasons.
4. **`source_selection_candidates.rank` is not fallback**—display/eligibility order only.
5. **Exclude overlapping Google/Fitbit-origin workout samples** from workout load calculations when Polar covers the interval (`exclude_from_load`).
6. Policies are versioned with a **generation** used for baseline reset; scores and derivation runs pin `source_policy_version_id`.
7. System default policy ships first; tenant overrides are a later, still-versioned feature.
8. Canonical load is always **internal from Polar HR zones**; vendor load is comparison only.
9. Episode matching is deterministic for a complete input set + `match_algorithm_version` (identical membership), not content-deterministic UUID minting. Late arrivals create a new `source_grain_versions` row and selection version under the same stable `source_grains.id` / `grain_key` without silently rewriting history; a newly discovered distinct episode gets a new `source_grains` identity.

MVP connectors: **`oura`**, **`google_health`**, **`polar`**. Legacy Fitbit Web API is **not** an MVP connector. Health Connect is not a server-side authoritative source in MVP. Apple Health/HealthKit remains deferred to a native iOS bridge. Fitbit Air exact device/SKU capability remains open validation.

## Consequences

### Positive

- Predictable selection for users and tests
- Honest data-quality UX
- Clear connector ownership per domain
- Aligns with Google Health migration timeline (pre–September 2026)
- Cross-provider same-episode facts can compete under one grain

### Negative

- If Oura is disconnected, sleep/overnight path may be insufficient even if Google Health sleep exists (by design until override)
- Requires product copy that explains gaps and "Fitbit via Google Health" labeling
- Google Health restricted scopes / security review gate daytime HR quality
- Polar dependence for internal load
- Episode matcher must be versioned and golden-tested

### Neutral

- Alternatives enable future user overrides without re-ingestion
- DataSource provenance preserved even when UI simplifies labels

## Reversal conditions

Change default mapping when evidence shows systematically better accuracy for another provider on a metric family—via a **new policy version**, not silent code special cases.

Allow automatic fallback only if a future ADR defines **user-visible** fallback rules with explicit status flags (still no averaging).

Do not reverse to legacy Fitbit Web API as the MVP daytime connector after the September 2026 sync stop without a new ADR and proven continuity path.

## Related

- [../architecture/ingestion-and-sync.md](../architecture/ingestion-and-sync.md)
- [../architecture/data-model.md](../architecture/data-model.md)
- [../architecture/health-engine.md](../architecture/health-engine.md)
- [../glossary.md](../glossary.md)
