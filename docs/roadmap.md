# Roadmap

**Status:** Proposed

**Last reviewed:** 2026-07-14

Authoritative for **phased plan** (coverage matrix item 20). Phases describe the implementation sequence. Application code has **started** under `backend/` for a model-free platform foundation; Phase Zero is **in progress**, not complete. See [implementation-status.md](implementation-status.md).

---

## Phase zero — risk retirement (mandatory)

**Status: in progress.** Local libSQL / Turso-compatible + SQLAlchemy + Alembic foundation exists and is tested. **Turso Cloud / remote is intentionally deferred** by product decision (not wired; not blocked on credentials). Remaining spikes (concurrency, encryption, volume, connectors, …) are open. Long-term production Turso remains proposed architecture (ADR 0003).

Retire platform and vendor uncertainties **before** feature velocity.

| Spike | Question | Exit criteria | Progress |
|-------|----------|---------------|----------|
| **Turso/libSQL Python + SQLAlchemy + Alembic** | Exact driver path for local libSQL now; production Turso later (selected long-term store) | Documented working **local** connection; known limitations listed; remote Turso deferred until product reopens it | **Partial (local complete for foundation):** local `sqlite+libsql` path proven on Python **3.13.14**; local-only URL validation; Python **3.14.5** + `sqlalchemy-libsql==0.2.0` segfault on macOS ARM documented; **Turso Cloud intentionally deferred** (not wired). Evidence: [evidence/phase-zero-turso-foundation.md](evidence/phase-zero-turso-foundation.md) |
| **Concurrency** | Write contention, nested transactions, job claim CAS (conditional UPDATE + affected-row/`RETURNING` check; no `FOR UPDATE`/`SKIP LOCKED`), fence reject, multi-worker race, leader lease for passive standby under concurrent API+worker on Turso/libSQL | Pass stress harness; exactly one claim winner; stale fence rejected; no silent corruption | **Partial (local repository lifecycle + single-process worker runtime complete):** CAS claims create durable attempts; fenced completion, retry scheduling, explicit and expiry dead letters, expiry history, dual-client races, multi-worker distribution, and nested savepoints are proven on **local** file-backed libSQL. The worker runtime (claim → execute → heartbeat → settle, retry classification and capped backoff, leader-gated reaping, signal shutdown) is implemented and tested end-to-end against the real repository, including **concurrent runtimes** on independent engines: exactly-once handler execution across a drained queue, a single reaper leader among contenders, and no false success when a lease is stolen mid-flight (runtime heartbeat guard and durable fence backstop covered separately). **Not** claimed: sustained multi-process fleet under production load, product job handlers, atomic domain side-effect UoW, Turso Cloud multi-client. Evidence: [evidence/phase-zero-job-concurrency.md](evidence/phase-zero-job-concurrency.md) |
| **Migrations** | Alembic expand/contract / N−1 rolling on libSQL/Turso | Upgrade/downgrade CI green | **Partial:** revisions through OAuth state `0005` upgrade/downgrade/upgrade on local libSQL; head → `0002` → head preserves a legacy job and verifies the `system.noop` backfill; migration tests derive head from the scripts rather than pinning a literal id; expand/contract / N−1 rolling not proven |
| **Encryption** | DB/backup/export encryption-at-rest assumptions on Turso path | Documented key separation and backup approach | **Partial (application-level envelope complete):** AES-256-GCM envelope encryption with a KEK/DEK hierarchy is implemented and tested — fresh DEK and nonces per seal, AAD binding an envelope to its owning row, versioned KEK registry with rotation that keeps old ciphertext readable, and fail-fast boot when no KEK is configured. KEKs load from `AKUNAKI_SECRET_KEKS`; **external KMS/secret-manager integration, backup/export encryption, and rotation runbooks are still open.** Evidence: [evidence/phase-zero-envelope-encryption.md](evidence/phase-zero-envelope-encryption.md) |
| **Volume** | Minute-level HR sample cardinality estimates | Storage and write budget; decide sampling/downsampling policy | **Not started** |
| **Vector (later-ready)** | Filtered ANN + tenant predicates; optional F32_BLOB path | Spike note; not MVP schema | **Not started** |
| **Google Health / Fitbit-origin capability** | Device marketing name mapping and Google Health field coverage | Written validation note; open gaps listed | **Not started** |
| **Polar v3 vs v4 / Verity Sense swim** | Required swim fields availability | Choose API version; document missing fields | **Not started** |
| **Google Health intraday / restricted scopes** | Approval path realistic for MVP? | Go/no-go for daytime HR resolution | **Not started** |

**Exit phase zero:** written spike reports linked from this doc; local libSQL path documented with known limitations; remaining spikes complete; remote Turso remains future work under ADR 0003 unless a proven blocker reopens the ADR; connector capability matrix updated.

Dependencies: backend foundation scaffold is present (`backend/`); remaining spikes have no product-feature dependencies.

---

## Phase one — foundation

### Scope

- Repository scaffold: `backend/` **started** (core package only); still need `web/`, `infra/` per [architecture/repository-and-services.md](architecture/repository-and-services.md)
- Auth: OIDC + sessions
- Schema: tenants, users, sessions, connections, secrets, jobs, raw tables
- Job claim loop with one **core** worker
- Oura connector: OAuth, webhook verify, initial sleep sync
- Normalize sleep → canonical facts
- Manual sync + connection health
- Core install: **no** model SDK

### Vertical slice

**Connect Oura → see raw sync success and latest sleep fact in API (internal/debug or minimal UI).**

### Exit criteria

- CI: unit + migration + job lease tests, models disabled, core-only boot
- Envelope encryption for tokens
- Privacy delete stub cancels jobs and scrubs demo tenant
- No DuckDB, no Redis, no required model config

### Deferred

Google Health + Polar connectors, scoring UI polish, agent-worker, MCP, vectors

---

## Phase two — deterministic engine

### Scope

- Google Health + Polar connectors (poll paths; scopes as approved)
- Source policy tables + selection
- Features, baselines, scores, factors, anomalies, recommendations (`general_recovery_v0.1.0` recovery only until other score formulas accepted)
- Product `/v1` day surfaces (`/v1/today`, recovery, sleep, …) and opaque provenance
- **Tool registry** (typed facade; independent of AI)
- Golden formula tests for exact v0.1.0 mappings, freshness, sleep debt, load, training labels

### Vertical slice

**Multi-provider day → authoritative selection → score with factors and insufficient paths.**

### Exit criteria

- Golden fixtures for ok/partial/insufficient
- Overlap exclusion for Google/Fitbit-origin vs Polar workouts
- Formula version pinned on score rows
- ACWR copy review: no injury language
- Tools usable by REST without model packages

### Dependencies

Phase one; phase-zero connector gates for fields claimed in UI

---

## Phase three — dashboard PWA

### Scope

- Next.js PWA Today hierarchy: How / Why / What
- Recovery, sleep, trends/metrics, workouts, swimming, data quality
- Connections, source settings, privacy
- Dark/light tokens, custom primitives, interpretable charts, a11y CI
- Export job + private encrypted object download
- Generated OpenAPI client; NetworkOnly authenticated `/v1`

### Vertical slice

**Mobile user understands today without assistant.**

### Exit criteria

- Models still off in CI e2e; complete models-off UX
- No persistent health JSON in browser storage by default
- Lighthouse/a11y budgets met for core routes

### Dependencies

Phase two API contracts stable enough for client generation

---

## Phase four — optional agent + MCP prep

### Scope

- Optional dependency extra + **separately deployable agent-worker**
- Model provider port + adapters; default/per-task selection; disable
- SSE conversations (persistent event_id/run_id, Last-Event-ID, heartbeat) + confirmation for mutations
- Egress consent + context manifest UX
- Optional vector boundary design/spike only if needed (never required for scores)
- MCP **read-only** stdio adapter process (local); remote Streamable HTTP design only or limited staging

### Vertical slice

**User asks why score changed; agent cites tool results; cannot invent score.**

### Exit criteria

- Core e2e green with models disabled and agent-worker absent
- Isolated CI job with mock model
- Intentional disable → 409 `agent_disabled`; outage → 503
- MCP origin validation and pinned protocol version documented in code

### Dependencies

Phase three for UX entry; phase two tools surface

---

## Phase five — nutrition and companion hooks

### Scope

- Nutrition log entities and descriptive correlations (no causation)
- Android Health Connect **companion bridge design/spike** (not server connector fiction)
- Apple Health / HealthKit **future native iOS bridge design** (device-local; not server connector; no native app in MVP)
- Deeper load analytics if Polar swim validation passed
- Multi-worker / external-broker evaluation when **any** sustained scale trigger fires

### Vertical slice

**Log meal → see association panel; companion paths documented honestly.**

### Exit criteria

- Nutrition never presented as causal injury/weight medical advice
- Health Connect and Apple Health/HealthKit remain out of server connector matrix until companions exist

---

## Dependency graph

```text
phase0 ──► phase1 ──► phase2 ──► phase3 ──► phase4
                          │                    │
                          └────────► phase5 ◄──┘
                     (nutrition can start after phase2
                      data model; companion after phase3)
```

---

## Deferred work (explicit)

| Item | Why deferred |
|------|--------------|
| Redis / SQS / external broker | Evaluate when **any** sustained scale trigger holds |
| DuckDB warehouse | No MVP analytics warehouse need |
| Google Fit | Deprecated foundation |
| Server-side Health Connect | Impossible; on-device Android companion later |
| **Apple Health / HealthKit server connector** | Impossible; device-local; future **native iOS bridge** only; no native mobile app in MVP; distinct from Health Connect |
| Optional vector retrieval | Phase four/future agent; SQL remains source of truth without embeddings |
| Multi-region active-active | Complexity |
| Clinical validation study | Out of eng roadmap |
| Household multi-user tenancy | MVP single user per tenant |
| Write-capable MCP remote | Safety |

---

## Top risks

| Risk | Mitigation |
|------|------------|
| Turso Python/SQLAlchemy path issues | Phase zero validates exact path; only proven blocker reopens ADR 0003; SQLite local for relational tests |
| Google Health scope / security review delays | Degrade daytime metrics; keep Oura sleep path |
| Polar swim fields missing | Narrow workout claims; open decision |
| Formula trust / misuse | Insufficient honesty; wellness copy; no diagnosis |
| Model leakage of PHI | Consent, minimization, agent isolation, disable switch |
| Job queue bottleneck | Metrics + evaluate external broker on **any** sustained trigger |
| Vendor schema churn | Fixtures + contract tests |
| Apple Health / Health Connect expectations | Explicitly not server connectors; companion bridges only |

---

## Open decisions

1. ~~Final IdP choice~~ — **decided 2026-07-19: self-hosted [Authelia](https://www.authelia.com/)**. Chosen for a minimal footprint (single Go binary, no external database) with password + TOTP/WebAuthn. Known trade-off: Authelia's OIDC provider is secondary to its reverse-proxy role. Mitigated by building the handshake to **standard OIDC** (discovery, PKCE, `state`, `nonce`, JWKS), so switching providers is a config change, not a rewrite. Rejected: Pocket ID (passkey-only), Zitadel/Keycloak/Authentik (heavier; require Postgres and, for Authentik, Redis + workers).
2. Turso phase-zero findings (path details; reopen ADR only on proven blocker)
3. Google Health intraday / restricted-scope approval strategy
4. Polar API major version for MVP
5. Exact Fitbit-origin device SKU naming and capability note (via Google Health)
6. ~~Default backfill lookback days~~ — **decided 2026-07-19: 30 days.** Lower first-sync cost and vendor load; the value stays configurable via `SyncConfig`.
7. ~~Whether disconnect retains historical facts by default~~ — **decided 2026-07-19: always preserve full data.** Disconnecting revokes credentials but never destroys history; only an explicit privacy delete removes facts.
8. First model provider to productionize (if any)
9. Whether tenant source overrides ship in phase three or later
10. Legal review timeline before any compliance language
11. Timing of Apple Health / HealthKit iOS bridge vs Android Health Connect companion

---

## Related

- [architecture/overview.md](architecture/overview.md)
- [testing.md](testing.md)
- [adr/README.md](adr/README.md)
