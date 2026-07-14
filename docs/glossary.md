# Glossary

**Status:** Proposed

**Last reviewed:** 2026-07-13

Terms used consistently across the proposed architecture. Prefer these spellings in docs and future code.

| Term | Definition |
|------|------------|
| **Tenant** | Isolation boundary for a single user account (and future household extensions). All operational rows carry `tenant_id`. |
| **Connection** | A tenant's authorized link to one provider (`oura`, `google_health`, `polar`), including encrypted credentials and status. |
| **Provider** | External wearable or health API vendor. MVP provider ids: `oura`, `google_health`, `polar`. |
| **Connector** | Code port that implements OAuth, fetch, provider-specific webhook verification, and cursor handling for one provider. |
| **Google Health** | Cloud API (Google Health API v4) that is the successor to the legacy Fitbit Web API. MVP connector id `google_health`. Uses Google OAuth; not Google Fit. Current Fitbit-origin connector path. |
| **google-wearables** | Google Health data-source family selected for Fitbit-origin daytime policy. UI may say "Fitbit via Google Health"; storage preserves DataSource origin/device. |
| **Legacy Fitbit Web API** | Deprecated Fitbit server API that stops syncing in September 2026. **Not** an MVP connector. |
| **Health Connect** | Android on-device health platform; future companion bridge only, not an MVP server connector. Distinct from Apple Health/HealthKit. |
| **Apple Health / HealthKit** | Apple on-device health store; fine-grained user-authorized; future **native iOS bridge** that syncs typed, provenance-preserving records to the backend. **Not** a server connector; **no** native mobile app in MVP. |
| **RawEnvelope** | Typed container for one fetch or webhook-driven page: exact payload body, redacted request metadata, schema version, received_at. |
| **raw_payload** | Durable storage of exact vendor transport bodies; **every response retained**; `content_hash` indexed, not a uniqueness constraint that erases repeats. Optional `transport_kind` source marker (e.g. `webhook_capture`). No FK to `webhook_inbox`. |
| **Raw revision** | Immutable logical version of a raw object linked to a payload page and optional sync run; **no** `normalizer_version` on the revision. |
| **Vendor tombstone** | Explicit vendor-side deletion marker (`tombstone_reason=vendor_deleted`) retained so history does not silently drop. |
| **Privacy tombstone** | Transient marker during privacy deletion only (`tombstone_reason=privacy_delete`); hard-scrub follows. Not a long-lived substitute for scrub. Not `superseded`. |
| **Normalizer** | Pure transform from RawEnvelope / raw revision content to candidate measurement records with units, quality, and lineage; version recorded on **facts**. |
| **fact_records** | Common metadata header for every typed health fact; one-to-one typed detail tables hold metric columns (not core EAV). |
| **Canonical fact** | Policy-selected `fact_records` row referenced by a current `source_selection.selected_fact_record_id` for a metric family and `grain_key`. |
| **source_grains** | Stable provider-independent **episode identity header** for a session/workout grain (`id`, tenant, metric_family, granularity, created_at); selection `grain_key` is this id, never a vendor session/workout id and never a version-row id. |
| **source_grain_versions** | Versioned membership/interval container for a `source_grains` identity (canonical day/interval, `match_algorithm_version`, `version_n`, `is_current`); late arrivals add a new version under the same grain id. |
| **source_grain_members** | Real-FK membership of fact records in a **`source_grain_versions`** row (`source_grain_version_id` + `fact_record_id`); unique per version. |
| **match_algorithm_version** | Version of the episode matcher pinned on a grain version; same complete input set + version yields identical membership; late arrivals open a new grain version without silently rewriting history. Not a claim that UUID allocation is content-deterministic. |
| **grain_key** | Non-null **provider-independent** identity of a selection grain (daily metric+date, interval pair, or stable `source_grains.id` for session/workout). Session/workout keys are stable allocated ids; daily/interval keys are content-derived. |
| **source_selection** | Exactly one current versioned decision per `(tenant, metric_family, granularity, grain_key)`; nullable real FK to `fact_records`; null only for `missing_authoritative` with `missing_reason`. Session/workout also pin `source_grain_version_id` (exact membership snapshot under stable `source_grain_id`). |
| **source_selection_candidate** | Alternative fact for a selection decision (real FK, rank, eligibility/reason); never averaged; never auto-fallback. Eligible candidates are members of the selection’s pinned grain version unless explicitly ineligible near-match with reason. |
| **candidate rank** | Display/eligibility order on `source_selection_candidates.rank` only—not silent fallback; not a policy multi-authoritative ranking. |
| **Current row** | Latest non-superseded version of a fact, selection, or derived artifact for a natural key; historical versions remain queryable until privacy scrub. |
| **Local health day** | Calendar date (`YYYY-MM-DD` TEXT) in the tenant's IANA timezone used to bucket sleep, readiness, and daily aggregates. |
| **Wake-date sleep assignment** | Rule that assigns a sleep bout to the local calendar date of wake time (not sleep onset). |
| **Source policy** | Effective-dated rules mapping metric families to authoritative providers/data-source families, with inspectable alternatives. |
| **Authoritative source** | Provider (and optional data-source family) selected by source policy for a metric family; alternatives are retained as candidates, not averaged. |
| **daily_health_features** | Derived scalar or structured field for one local health day, produced by a derivation run. |
| **Baseline** | Rolling personal reference (median / `robust_scale` = 1.4826×MAD with IQR/1.349 then metric floor; EWMA α=0.25 when used) for a feature over a defined window and stratification context. |
| **robust_scale** | σ-equivalent dispersion stored for z: prefer `1.4826 * MAD`; else IQR/1.349; else metric floor. |
| **daily_health_scores** | Deterministic 0–100 (or `insufficient` / `partial`) daily health composite identified by `score_code`, with factors and confidence. |
| **general_recovery_v0.1.0** | Executable, explicitly unvalidated recovery formula version with exact weights and mappings. |
| **Factor** | Signed contributor to a score with magnitude, direction, input provenance, and coverage disclosure. |
| **Anomaly** | Persistent deviation from baseline beyond rule thresholds, with start/end and severity; non-diagnostic. |
| **Recommendation** | Rule-produced wellness/performance guidance; one global primary plus supporting detail after conflict resolution. |
| **Training recommendation** | Deterministic hard / moderate / light / rest / insufficient label with exact v0 thresholds, downshifts, and ruleset version. Missing data → insufficient / reconnect, not rest. |
| **Formula version** | Immutable identifier for scoring/baseline/load equations; every derivation stores the version that produced it. |
| **Source-policy generation** | Version generation of source policy used as baseline stratification/reset key when selection rules change materially. |
| **Dependency hash** | Hash of exact input record IDs, baseline refs, formula version, and source-policy version used for recompute invalidation. |
| **derivation_run** | Reproducible record of one engine derivation (features, baselines, scores, factors, anomalies, recommendations). |
| **derivation_input** | Typed nullable FK input row for a derivation_run (exactly one of selection/fact/feature/baseline/score/anomaly/recommendation); retains `role`; no polymorphic table/id pointer. |
| **as_of_at** | Explicit UTC evaluation instant for freshness; freshness is relative to each input's freshness timestamp vs `as_of_at`. |
| **webhook_inbox** | Durable, deduplicated store of verified webhook deliveries; ack quickly, then refetch; body capture may precede sync run. Sole FK to body is `body_payload_id` → `raw_payload`; reverse lookup is through inbox. |
| **sync_run** | One scheduled, manual, or webhook-triggered fetch attempt with status and error class (no payload bodies in health fields). |
| **Job** | Durable work unit in the database-leased queue (sync, normalize, recompute, export, delete). |
| **Lease / fencing token** | Worker claim on a job with expiry; fencing prevents stale workers from committing after lease loss. Job claim uses conditional CAS UPDATE (not `FOR UPDATE`/`SKIP LOCKED`). Passive standby requires a leader lease/fence before scheduling or reaping. |
| **Idempotency key** | Client- or system-supplied key ensuring at-most-once side effects for mutating operations. |
| **Tool** | Typed application capability (Pydantic in/out, version, scopes, sensitivity, side effect, idempotency, timeout, audit, model exposure, confirmation) invoked by REST, reports, agent, or future MCP. Built phase two independent of AI. |
| **Tool registry** | Typed capability facade over selected application services; not a second business layer. |
| **MCP** | Model Context Protocol; optional phase-four adapter **process** over the same tools, not a second business layer. |
| **Model provider** | Optional LLM backend (OpenAI, Anthropic, Gemini, xAI, OpenRouter, local). Fully disableable; no silent fallback. |
| **Agent-worker** | Optional separately deployable process from the modular monolith that claims agent jobs only; missing/failed worker cannot affect core product. |
| **SSE conversation event** | Durable server-sent event with monotonic event_id and run_id (token, tool call, confirmation required, heartbeat, terminal states). |
| **PWA** | Progressive web app: Next.js TypeScript frontend with offline shell, not offline health JSON cache; NetworkOnly for authenticated `/v1`. |
| **libSQL / Turso** | Operational SQLite-compatible store; **Turso selected for production**; local libSQL/SQLite for dev and many relational tests; vector integration uses libSQL/Turso. |
| **UUIDv7** | Time-ordered UUID stored as TEXT primary keys in SQLite/libSQL. |
| **Envelope encryption** | Data key encrypts secrets at rest; key encryption keys managed outside the DB row. |
| **token_hash** | Server-side hash of opaque session cookie token; raw token never stored. |
| **state_hash** | Server-side hash of OAuth `state`; raw state never stored. |
| **PHI-free logs** | Logs, traces, and metrics must not contain health measurement values or free-text health content; tenant labels pseudonymized; email/display names are sensitive PII. |
| **Deletion completion proof** | Minimal non-identifying audit record that a privacy deletion completed; not a restore map. |
| **Restoration-suppression ledger** | Access-separated store of HMAC-derived deleted-tenant/object selectors under a dedicated deletion key; replayed before restored data is served; retained until backups expire + 30 days, then destroyed. |
| **health_experiments** | First-class observational, non-causal personal experiments (hypothesis, protocol, dates/status, outcome feature codes, confounder notes). Not product feature flags. |
| **feature_flags** | Product rollout flag assignments; distinct from health_experiments. |
| **oxygen_saturation_samples** | Typed detail table for SpO2 samples. |
| **subjective_check_ins** | Typed completed check-in rows (normalized energy/stress/symptom scales); incomplete rows are not engine inputs. |
| **laboratory_results** | Typed lab result detail table. |
| **Vertical slice** | End-to-end path through one user-visible capability (Oura sleep → score → How am I). |
| **Phase zero** | Risk-retirement spikes before feature build: exact Turso Python/SQLAlchemy/Alembic path, concurrency, migrations, encryption, volume, later vector options—not a generic store go/no-go. |
| **ACWR** | Acute:chronic workload ratio = 7-day acute load sum divided by chronic weekly equivalent (28-day sum / 4); v0 requires 7/7 and 28/28 known days; descriptive only, never injury prediction. |
| **Canonical load** | Internally calculated training load from Polar HR-zone durations under versioned individualized zone boundaries; vendor load is comparison only. |
| **Intraday** | Sub-daily samples (minute HR). Google Health default list data includes elevated-resolution streams subject to restricted scopes and security review. |
| **AccessLink** | Polar's developer API family (v3/v4 capability matrix to be validated in phase zero). |
| **Sleep target** | Explicit user preference for target sleep minutes; provisional default 480 minutes until set. Never the chronically short personal median. |
| **Sleep debt** | Rolling 14-calendar-day shortfall versus sleep target with daily surplus credit cap 60 min and total cap 14×target; lower bound when partial; ≥12/14 known before debt recommendation. |
| **Sleep consistency** | Mean resultant length over principal-sleep midpoints for current+13 days; score = 100R; minimum 7 valid nights. |
| **retrieval_documents** | Future vector parent rows with typed source links; child `vector_embeddings` holds embeddings via real FK. |
| **Registry** | Key/value definitions table acceptable only for derived feature codes and long nutrient/lab vocabularies with enforced definitions—not core vitals storage. |

Related: [product-principles.md](product-principles.md), [architecture/overview.md](architecture/overview.md).
