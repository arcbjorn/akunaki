# Testing

**Status:** Proposed

**Last reviewed:** 2026-07-13

Authoritative for **tests** (coverage matrix item 16). Describes the proposed test pyramid and mandatory gates for a future implementation. No test suite exists in this repository yet.

---

## Goals

1. Prove the **deterministic core** with **all model providers disabled** and **core-only install** (no model SDK).
2. Protect provenance, idempotence, revision semantics, and source policy behavior.
3. Use **sanitized vendor fixtures**, never live production PHI.
4. Catch timezone/DST, authz, accessibility, restore, agent isolation, and vector tenancy regressions before release.
5. Assert API contracts under **`/v1`** (not `/api/v1`).

---

## Test pyramid

```text
        /  e2e + visual + a11y  \
       /  load + restore drills  \
      /   contract + integration  \
     /    property + golden formulas \
    /_____ unit (domain pure) _______\
```

| Layer | Owns | Models |
|-------|------|--------|
| Unit | domain stages, normalizers, policy selection | off |
| Property / golden | formula invariants, fixtures → scores | off |
| Integration | DB, jobs, migrations, connectors with HTTP mocks | off |
| Contract | OpenAPI/schema vs web client types (`/v1`) | off |
| Security | authz, CSRF, webhook auth/replay, OAuth fixation, SSRF | off |
| E2E | critical user journeys in browser | off by default |
| Agent (optional job) | confirmation, no score invent, isolation | on in isolated job only |
| Vector (optional) | tenant isolation, version rebuild, deletion | libSQL/Turso only |
| a11y / visual | Today hierarchy and score states | off |
| Load | sync/recompute fan-out | off |
| Restore | backup → restoration-suppression ledger replay before serve | off |

**CI default pipeline must pass with models fully disabled and no model SDK in the core install path.**

---

## Dependency and boot boundary

| Test | Assertion |
|------|-----------|
| Core-only install | Environment without `[agent]` extra has **no** model SDK packages |
| Core-only boot | API and core worker start with **no** model config |
| Denied model network | Core process suite cannot open outbound model provider hosts |
| Dependency boundary | `domain` / core `api` import graph excludes model SDKs |

---

## Unit tests

- Pure functions: features, baselines, scores, anomalies, recommendations
- Source policy selection: one current selection per provider-independent `grain_key`; one authoritative rule per `(policy_id, metric_family[, granularity])`; nullable `selected_fact_record_id` only with `missing_authoritative` + `missing_reason`; candidates never averaged; no silent fallback; candidate `rank` display-only
- Episode matching: same complete input set + `match_algorithm_version` → identical `source_grain_members` under a `source_grain_versions` row; late arrivals create a new `source_grain_versions` + membership set and selection version under the same stable `source_grains.id` / `grain_key` without rewriting history; newly discovered distinct episode → new `source_grains` identity
- **Pinned membership version:** session/workout selections require `source_grain_version_id` (and `source_grain_id`); version row belongs to that grain; selected fact is a member of the pinned version; eligible candidates are members unless explicitly ineligible near-match with reason; daily/interval leave both grain FKs null
- **Cross-provider same-episode candidates:** fixture with Oura + Google Health (or Polar + Google Health) facts for one real-world bout → one `source_grains` identity, both facts as members of the pinned version and candidates under one `grain_key`, policy selects authoritative only
- Grain keys never embed vendor session/workout ids; session/workout keys are stable provider-independent `source_grains.id` (not content-deterministic UUIDs; not version-row ids); daily/interval keys are content-derived; matching determinism is membership identity, not UUID minting
- Normalizers: field preservation (UTC, offset, IANA, wake-date, units, lineage); `steps_count` INTEGER; energy as kcal
- Tool input validation (Pydantic)
- Score range: 0–100 or null iff insufficient (recovery only for v0.1.0)
- Known **zero load** vs **unknown load** distinction
- Steady-load **ACWR near 1** for constant daily load fixtures with 7/7 and 28/28 known
- **HRV method-specific baselines** (RMSSD vs SDNN / window) do not mix
- Multiple **naps** and multiple **workouts** per local day (separate canonical grains)
- Swim session requires parent workout link

---

## Property and golden formula tests

| Kind | Example |
|------|---------|
| Golden | Fixed fixture pack → exact score JSON for `general_recovery_v0.1.0` |
| Property | Score in 0–100 or null iff insufficient |
| Property | Same inputs + version ⇒ identical outputs (hash equality) |
| Property | Missing critical ⇒ never fabricated 50 |
| Property | `robust_scale` path: 1.4826×MAD, else IQR/1.349, else metric floor + flag |
| Property | Directed mappings: HRV +z; RHR −z; temp −\|z\|; resp −max(z,0) |
| Property | Freshness piecewise vs `as_of_at` (1@≤24h, 0.5@72h, 0@&gt;168h); min across critical |
| Property | Sleep debt rolling 14d, credit cap 60, total cap 14×target; partial lower bound; debt rec needs ≥12/14 known |
| Property | Consistency: 100R, min 7 midpoints |
| Property | Monotony: all-zero→0; equal positive→10 + flag; else min(mean/stdev,10); needs 7/7 |
| Property | Training labels exact bands; missing data → insufficient not rest; downshift rules |
| Property | High-severity anomaly alone → downshift to `light` (not rest); rest only score&lt;40 or explicit severe symptom flag |
| Property | `sleep_extend_window` only when `known_days >= 12` and `debt >= 120` min and adherence &lt; 90 |
| Property | `load_ease` only when ACWR defined, \(a &gt; 1.3\), and \(c_{\mathrm{hrv}} &lt; 40\) |
| Property | High symptom burden = `symptom_burden_n >= 0.75` or explicit severe flag |
| Property | Weights of present components renormalize with disclosed `available_weight`; full set sums to 1.00 |
| Property | Subjective: completed check-in with explicit no symptoms ⇒ `symptom_burden_n = 0`; blank symptom fields ⇒ omit subjective component; missing check-in ⇒ omit |
| Property | Baseline-insufficient component omitted from \(W\) |
| Property | Non-recovery score codes not writable without accepted formula fixtures |
| Property | `GET /v1/today` contract: only recovery is 0–100 score in v0.1.0; sleep/strain/activity/readiness are summaries/labels not fabricated scores |
| Window | Changing day D only invalidates expected baseline set |

Unvalidated formulas still need **bit-stable** tests so refactors do not silently change semantics.

---

## Schema and integrity invariants

| Invariant | Assertion |
|-----------|-----------|
| Selections | Partial unique one current per `(tenant_id, metric_family, granularity, grain_key)` |
| Policy rules | SQLite-safe partial uniques: `(policy_id, metric_family)` WHERE `granularity IS NULL`; `(policy_id, metric_family, granularity)` WHERE `granularity IS NOT NULL`; no `priority_rank` |
| source_grains | Stable identity header; session/workout grain_key = `source_grains.id` |
| source_grain_versions | `UNIQUE (source_grain_id, version_n)`; partial unique one current per `source_grain_id`; holds interval/day + `match_algorithm_version` |
| source_grain_members | Real FKs on `source_grain_version_id` + `fact_record_id`; unique membership per version |
| Selection grain pin | Session/workout: `source_grain_id` + `source_grain_version_id` required; version.`source_grain_id` matches selection; daily/interval both null |
| Selection FK | `selected_fact_record_id` null iff `missing_authoritative` with non-null `missing_reason`; when non-null session/workout, fact is member of pinned version |
| Candidates | Real `fact_record_id` FK; never auto-applied as selection; `rank` display-only; eligible members of pinned version unless ineligible near-match with reason |
| Derivation inputs | Exactly one typed FK non-null (CHECK); no `input_kind`/`input_id` |
| Raw revisions | Immutable; no `normalizer_version`; tombstone_reason ∈ {vendor_deleted, privacy_delete} |
| raw_payload | Repeat `content_hash` allowed (indexed, not unique-erasing); webhook capture with null `sync_run_id` + `transport_kind`; **no** `raw_payload.webhook_inbox_id`; only `webhook_inbox.body_payload_id` → payload |
| Sessions | `token_hash` only; CSRF hashed or derived |
| OAuth states | `state_hash`; encrypted verifier; exact redirect; single-use expiry |
| Vectors (when present) | `vector_embeddings.retrieval_document_id` real FK; tenant predicate; no generic source pointer |
| Naming | `daily_health_features`, `daily_health_scores`, `oxygen_saturation_samples`, `subjective_check_ins`, `laboratory_results`, `health_experiments` |

---

## Sanitized vendor fixtures

- Store under proposed `backend/tests/fixtures/{oura,google_health,polar}/`
- **Do not** treat legacy Fitbit Web API as an MVP fixture source; Fitbit-origin daytime data is captured via **Google Health** fixtures
- Scrub tokens, emails, precise GPS if any
- Document capture date and schema version
- Contract tests fail when normalizer cannot parse fixture after vendor schema drift

---

## Integration tests

| Area | Assertions |
|------|------------|
| Migrations | alembic upgrade/downgrade; **N / N−1** rolling expand/contract |
| SQLite + Turso path | Relational suite on SQLite; selected integration on Turso/libSQL |
| Raw transport | same logical content_hash skips new revision; **new** transport row still retained |
| Crash-safe cursor | atomic cursor + raw + outbox commit; crash replay safe |
| Webhook capture | inbox (null body FK) → raw_payload (null sync_run, transport marker) → set `body_payload_id`; reverse lookup via inbox only; later refetch with sync_run |
| Jobs | CAS claim (conditional UPDATE on ready/due/expected fence; RETURNING or affected-row check); concurrent dual claim → one winner; lease expiry; fence reject; loser retry; dead letter; no `FOR UPDATE`/`SKIP LOCKED` requirement |
| Leader lease | Passive standby cannot schedule/reap without leader lease CAS; fence mismatch stops leadership |
| Turso concurrency (phase zero) | Explicit multi-client claim race + leader lease stress on Turso/libSQL; validate CAS protocol under concurrent API+worker |
| Idempotency | double POST export one job |
| Recompute | affected dates only |
| Privacy delete | scrub + job cancel + vector/export/conversation hard-delete |
| Deletion restore | restore backup → **restoration-suppression ledger** applied **before serve**; deleted data absent; completion proof alone insufficient |
| Ledger lifecycle | suppression entries destroyed after backup expiry + 30 days (test clock) |
| Tenant isolation | cross-tenant 404 |
| Export | expiry, private object access, cache headers |

---

## Timezones and DST

- Fixtures spanning US and EU DST transitions
- Wake-date sleep assignment across local midnight
- Workout spanning midnight local
- Tenant timezone change triggers re-bucket policy (document expected behavior in tests)

---

## Contract tests

- Generated OpenAPI from FastAPI vs web TypeScript client types
- Base path **`/v1`** (assert no stale `/api/v1` client assumptions)
- Representative today response schema matches [architecture/api-tools-and-agent.md](architecture/api-tools-and-agent.md)
- Problem Details shape; **412** on `If-Match` failure; **422** validation; **409 `agent_disabled`** intentional disable; **503** agent outage—not disable

---

## Security tests

| Area | Assertions |
|------|------------|
| Unauthenticated | access denied |
| CSRF | rejection on cookie mutations; secret not stored plaintext |
| Session | raw cookie token never in DB; lookup by `token_hash` |
| OAuth | **state_hash** / session fixation defenses; encrypted PKCE; exact redirect; single-use |
| Webhooks | **Google / Oura / Polar** provider-specific auth; **replay** rejected |
| Tool scopes | enforcement |
| Confirmation | replay fails; **arg substitution** fails; model cannot confirm |
| Model egress | manifests present; redaction; consent scope |
| Local model SSRF | blocked destinations |
| Logs | no health values; **no raw tenant ids** as free labels (pseudonymized); no email/display_name dumps |
| Browser | non-persistence of health JSON by default |
| Deletion artifacts | completion proof has no health/identity payload; suppression ledger only HMAC selectors |
| Vector (when present) | **tenant isolation**; version rebuild; deletion with source; typed document links |

---

## Agent-specific tests (optional CI job)

- With mock model provider: tool call → confirmation required for mutations
- Model cannot write `daily_health_scores`
- Agent intentionally disabled → **409 `agent_disabled`**; triad/Today e2e still green
- Agent-worker absent/crash → agent routes **503**; ingestion/engine/export integration still green
- No silent model fallback

**Do not** assert `503` + `models_disabled` for intentional disable.

---

## Accessibility, visual, load, restore

| Suite | Cadence |
|-------|---------|
| a11y (axe or equivalent) on Today hierarchy | CI |
| Visual snapshots for score states (ok/partial/insufficient) | CI or nightly |
| Load: concurrent sync jobs per tenant caps | pre-release |
| Restore + restoration-suppression ledger before serve | quarterly staging |

---

## Definition of done for scoring changes

1. New `formula_version` string
2. Golden fixtures updated or added
3. Property tests still pass
4. Recompute job documented
5. UI copy keys updated if factors change
6. Docs note validation status remains unvalidated unless research process says otherwise
7. Non-recovery scores: accepted formula spec + golden fixtures before any write path ships

---

## Related

- [architecture/health-engine.md](architecture/health-engine.md)
- [architecture/operations.md](architecture/operations.md)
- [architecture/api-tools-and-agent.md](architecture/api-tools-and-agent.md)
- [roadmap.md](roadmap.md)
