# Implementation status

**Last updated:** 2026-07-19

Honest truth table for what exists in this repository. Prefer this page over roadmap marketing language when asking “is it built?”

Legend:

- **Implemented** — code present in tree
- **Tested** — automated tests green on the documented platform
- **Pending** — designed in docs; not built or not proven

---

## Platform foundation (Phase Zero partial)

| Capability | Implemented | Tested | Pending / notes |
|------------|:-----------:|:------:|-----------------|
| Backend package layout (`src/akunaki/{domain,application,ports,adapters/db,api,worker}`) | yes | yes (import/boot) | layers mostly empty by design |
| Python 3.13.14 pin + `requires-python >=3.13.14,<3.14` | yes | yes | 3.14 rejected after segfault observation |
| Exact dependency pins + `uv.lock` | yes | yes (`uv lock --check`) | stable-only policy |
| pydantic-settings config (`AKUNAKI_` prefix) | yes | yes | no model config surface |
| Safe local default `sqlite+libsql` URL | yes | yes | parent dir created on engine build |
| Local-only `database_url` validation (official `sqlite+libsql://` memory / path memory / relative / absolute file) | yes | yes | rejects hostname, credentials, port, query, fragment, non-dialect |
| Remote Turso auth token / connect_args | **no** | n/a | **intentionally deferred** (not wired; not credential-blocked) |
| SQLAlchemy 2 engine/session + FK pragma + busy_timeout(50ms) + file WAL once | yes | yes | StaticPool in-memory; QueuePool file-backed (pool_size=5, max_overflow=5, pool_timeout=5) |
| Declarative base + naming conventions | yes | yes | |
| Database readiness probe | yes | yes | |
| Alembic env + migrations (through OIDC login states) | yes | yes (up/down/up through `0012`; legacy job + `system.noop` backfill; head derived, not hardcoded) | full product schema pending |
| Connection lifecycle schema (`connections`, `connection_secrets`, `connection_health`) | yes | yes | migration `0004`; one connection per provider per tenant; provider/status vocabularies; ciphertext-only token storage; cascade delete |
| libSQL-compatible `Blob` column type | yes | yes | driver exposes no DBAPI `Binary`, so stock `LargeBinary` cannot bind — see Turso evidence note 4 |
| ORM models agree with migration (columns/FKs/indexes) | yes | yes | IDs caller-supplied TEXT; no UUIDv7 helper |
| Domain job lifecycle/failure types + ports Protocol (model-free) | yes | yes | immutable failure results; no SQLAlchemy in domain/ports; second-precision times; min 1s lease TTL |
| JobRepository atomic execution lifecycle + leader fencing | yes | yes | claim creates one attempt; complete/fail/retry/dead-letter/expiry history are fenced local short transactions |
| Idempotent enqueue (`enqueue_job`) deduped on `(tenant_id, idempotency_key)` | yes | yes | atomic `INSERT ... ON CONFLICT DO NOTHING`; NULL key always inserts; tenant-scoped; duplicate job id without a key raises |
| Durable attempt history + one-to-one dead-letter records | yes | yes | retry scheduling uses exact caller-provided delay; redacted failure messages capped at 500 chars |
| Leader lease owner/expiry pair + nonempty name checks | yes | yes | migration `0002` + model agreement |
| FastAPI app factory + `python -m akunaki.api` | yes | yes | |
| `GET /healthz` (service, db ready, `models_required=false`) | yes | yes | does not fabricate product health |
| Internal debug surface (`/internal/debug/sync-status`, `/latest-sleep`) | yes | yes | **unauthenticated**; router not registered unless `AKUNAKI_DEBUG_ROUTES_ENABLED` is set (default off); `private, no-store`; cross-tenant reads are 404 |
| Phase-one vertical slice (connect → sync → normalize → read in API) | yes | yes | verified end-to-end in a real process against a live HTTP provider |
| Authenticated `/v1` product surface | partial | partial | login, `/v1/session`, and `/v1/sleep` reachable; `/v1/today` and `/v1/recovery` await the scoring engine. The debug router remains a stand-in to be replaced |
| `GET /v1/sleep` deterministic sleep summary | yes | yes | authenticated, tenant from session; exact `sleep_summary_v0.1.0` adherence + 14-day debt; a **summary not a score** — no score field is exposed; unknown days disclosed as a lower bound; golden + end-to-end tests |
| Worker entry `python -m akunaki.worker` runtime | yes | yes | claim loop + SIGINT/SIGTERM cooperative shutdown; JSON logs |
| Worker runtime (claim → execute → heartbeat → settle) | yes | yes | port-typed in `application`; fake-repository policy tests + file-backed end-to-end tests |
| Retry classification + exponential backoff policy | yes | yes | transient/permanent/cancelled; capped jitter; min 1s (second-precision lifecycle) |
| Handler registry (`system.noop` built in) | yes | yes | unregistered `job_type` dead-letters instead of burning attempts |
| Background lease heartbeat | yes | yes | daemon thread; lease loss suppresses completion (no false success) |
| Leader-gated reaper tick (requeue expired / dead-letter exhausted) | yes | yes | standby never reaps without the `core-reaper` leader lease |
| Core-only / no model SDKs | yes | yes | import-linter + tests |
| Ruff / mypy / import-linter / pytest / pip-audit gates | yes | yes | see evidence docs |
| CI workflow (GitHub Actions) | yes | yes | four jobs: quality, migrations up/down/up on an ephemeral DB, core-only boot with models disabled, advisory `pip-audit`; every step verified locally before commit |
| Core-only boot proven without dev deps | yes | yes | CI installs with `--no-dev` and asserts no model SDK is importable, then boots API + worker with no `MODEL_*` config |
| Frontend / web | no | no | deferred |
| Session layer (`users`, `sessions`: issue, validate, rotate, revoke, purge) | yes | yes | migration `0011`; **hash-only** storage of cookie token and CSRF secret; expiry + revocation enforced; rotation revokes the predecessor |
| OIDC login state (`login_states`: hashed `state` + `nonce`, sealed PKCE verifier) | yes | yes | migration `0012`; separate from `oauth_states` because login has **no tenant yet** and needs a nonce; single-use, expiring, exact redirect match |
| `id_token` claim validation (iss, aud, nonce, exp/nbf/iat, sub) | yes | yes | pure and clock-injected; 60s skew tolerance; rejected tokens never yield an identity; email redacted in `repr` |
| OIDC client (discovery, authorize URL, token exchange, JWKS signature verification) | yes | yes | PyJWT against the issuer's JWKS; **asymmetric algs only** (HS256 refused — alg-confusion closed); real-HTTP verified; secrets never logged |
| OIDC login flow (`/auth/login`, `/auth/callback`, user upsert, session issue) | yes | yes | port-typed service; state sealed before redirect, single-use consume, verify-before-session; first login provisions tenant+user; end-to-end verified against a real HTTP IdP through to an authenticated `/v1` request |
| User provisioning (one user per tenant, MVP) | yes | yes | keyed by `(oidc_issuer, oidc_subject)`; first login provisions tenant + user atomically; email refreshed but never used as identity |
| Session cookie wiring + CSRF enforcement | yes | yes | `Secure`/`HttpOnly`/`SameSite=Lax`; CSRF required on POST/PUT/PATCH/DELETE; one generic 401 so unknown/expired/revoked are indistinguishable |
| `GET /v1/session`, `POST /v1/session/logout` | yes | yes | tenant comes from the validated session, never a request parameter; logout revokes server-side **and** clears the cookie |
| Connectors (Oura, Google Health, Polar) | partial | partial | Oura OAuth, fetch, initial sync, and sleep normalization ship; no webhooks or incremental sync; Google Health and Polar not started |
| Agent / model packages | no | no | forbidden in core |
| Full data-model schema | no | no | tenants, durable-job lifecycle, connection lifecycle, OAuth state, sync transport, and the **sleep** fact slice exist; `webhook_inbox`, other detail tables, source selection, and scores pending |
| Envelope encryption (AES-256-GCM, KEK/DEK, rotation, AAD binding) | yes | yes | fresh DEK+nonces per seal; versioned KEK registry; fail-fast boot without keys; mutation-checked randomness |
| Sealed tokens persisted to `connection_secrets` | yes | yes | raw column holds no readable token; envelope bound to its connection; cascade delete |
| KEK sourcing from external KMS / secret manager | no | no | keys load from `AKUNAKI_SECRET_KEKS` only; no KMS client, rotation runbook, or key-use audit |
| OAuth state / PKCE handshake primitives (`oauth_states`) | yes | yes | migration `0005`; hashed state only, sealed PKCE verifier, exact redirect match, single-use + expiry, purge sweep |
| Oura OAuth client (authorize URL, PKCE code exchange, refresh) | yes | yes | S256 only; typed failure vocabulary (`invalid_grant` → reauth vs retryable); secrets never logged; mock-transport + real-HTTP verified |
| OAuth linking service (start link → callback → sealed tokens) | yes | yes | port-typed in `application`; `tenant_id` is a parameter; connection row + sealed secret written in **one** transaction |
| Connection repository (link/relink, status transitions) | yes | yes | relink reuses the existing `(tenant_id, provider)` row; failed exchange leaves no half-written connection |
| Sync transport schema (`sync_runs`, `raw_payload`, `sync_cursors`, `raw_objects`, `raw_revisions`) | yes | yes | migration `0006`; every response retained (hash indexed, **not** unique); append-only revisions; `superseded` rejected as a tombstone reason |
| `webhook_inbox` table + webhook verification | no | no | deferred with webhook handling; one-way FK to `raw_payload` documented but not created |
| Oura V2 fetch client (windowed pages, pagination, typed failures) | yes | yes | exact body returned for faithful transport persistence; 401/403 → `unauthorized`, 429 → `rate_limit` with `Retry-After`; token never logged |
| Atomic fetch commit (`IngestionRepository`) | yes | yes | transport row + logical revision + cursor in **one** transaction; revision appended only on new `content_hash` |
| `connection.initial_sync` handler | yes | yes | opens sealed tokens, paginates, translates fetch outcomes into the worker's retry vocabulary; auth failure → `needs_reauth` + dead letter, 429/5xx → retry |
| Vendor record identity in the **raw** layer | yes | yes | per-record: one page yields one `raw_object`/`raw_revision` **per record**, keyed `stream:<vendor_id>` (or a body hash when the vendor supplies none); each revision stores its own slice body (`slice_json`) |
| Stable ids for streams without a vendor id | partial | yes | falls back to hashing the record body — per-record, but a cosmetic vendor change re-identifies the record; only `sleep`/`daily_*`/`workout` have mapped id fields |
| Sleep fact schema (`fact_records`, `sleep_sessions`) | yes | yes | migration `0007`; versioned headers with a partial unique index on current; typed one-to-one detail (not EAV) |
| Oura sleep normalizer (pure, deterministic) | yes | yes | wake-date assignment, canonical minutes, honest quality grading; no clock, so re-runs are byte-identical |
| Versioned fact writes (supersede, never update in place) | yes | yes | identical content is a no-op; changed content appends a version and retains the prior row with its detail |
| Recovery composite engine (`general_recovery_v0.1.0`) | partial | partial | pure domain: exact component weights, baseline z-score→c curve, bounded adherence, piecewise freshness, the three-part sufficiency gate, weighted mean renormalized over **present** weights with disclosed coverage, and `confidence = coverage*freshness*quality*baseline_maturity`; golden + mutation tested. **Takes already-mapped component scores** — baseline/ACWR/check-in mapping upstream, persistence, and the `/v1` surface are the next units |
| Other detail tables (HR, HRV, activity, workouts, labs, …) | no | no | **deliberately deferred**: each arrives with the normalizer that populates it, not as empty tables |
| `raw.normalize` job handler | yes | yes | keyed by `raw_revision_id`; enqueued in the **same transaction** as the revision, so a revision can never exist without its normalize job |
| Closed ingestion loop (sync → normalize → facts) | yes | yes | drained through the real worker; verified end-to-end against a live HTTP server with lineage intact |
| OAuth HTTP routes (authorize/callback endpoints) | no | no | **deliberately deferred**: `/v1` requires an authenticated session and auth/OIDC is not built; the linking service takes `tenant_id` as a parameter so routes are a thin layer later |
| Google Health / Polar OAuth clients | no | no | only Oura implemented; both gated on unstarted phase-zero spikes |
| Concurrent worker runtimes (exactly-once execution, single leader, stolen-lease safety) | yes | yes | bounded local stress: 3 workers/24 jobs, 4 contending reapers, independent engines |
| Sustained multi-process fleet under production load | no | no | in-process threads with independent engines only; no long-running or cross-host soak |
| Product job handlers | partial | partial | `connection.initial_sync` and `raw.normalize` ship; scoring/recompute handlers pending |
| Atomic domain side-effect unit of work | no | no | lease validity primitive exists; fenced side-effect UoW still pending |
| Remote production Turso (Turso Cloud) | no | no | **product deferred**; proposed in ADR 0003 only |
| Privacy delete stub (cancel jobs → scrub rows → completion proof) | yes | yes | phase-one exit criterion; ordering enforced (scrub before cancel is rejected); tenant-scoped; proof carries counts only |
| Restoration-suppression ledger | no | no | **deliberately not built**: needs a dedicated deletion key with access separation; an empty table would imply a guarantee the system cannot make |
| Backup expiry on deletion | no | no | pipeline records the stage; no backup provider is wired, so nothing is actually expired |
| Multi-tenant fact isolation | yes | yes | fact identity is tenant-scoped (migration `0010`); regression-tested after a collision bug where one tenant's fact silently overwrote another's |
| Encryption-at-rest / backup evidence | partial | partial | application-level envelope for secret columns done (see [evidence](evidence/phase-zero-envelope-encryption.md)); platform at-rest, backup/export encryption, and key separation runbooks open |
| Volume / vector spikes | no | no | Phase Zero open |

---

## Process entrypoints

| Entrypoint | Behavior today |
|------------|----------------|
| `python -m akunaki.api` | Serves core API; `/healthz` only |
| `python -m akunaki.worker` | Boots config/DB, readiness, then runs the durable claim loop until SIGINT/SIGTERM |
| `python -m akunaki.agent_worker` | **not present** |
| `python -m akunaki.mcp_adapter` | **not present** |

---

## Documentation vs code

| Area | State |
|------|-------|
| `docs/` architecture set | Proposed design (mostly still forward-looking; Turso Cloud remains **future** context) |
| `backend/` | First real application code (this foundation) |
| Phase Zero overall | **In progress** — local libSQL foundation, atomic durable-job repository lifecycle, and single-process worker runtime tested; remote Turso intentionally deferred; encryption / volume / connector spikes open |

Evidence: [evidence/phase-zero-turso-foundation.md](evidence/phase-zero-turso-foundation.md), [evidence/phase-zero-job-concurrency.md](evidence/phase-zero-job-concurrency.md), [evidence/phase-zero-envelope-encryption.md](evidence/phase-zero-envelope-encryption.md).
