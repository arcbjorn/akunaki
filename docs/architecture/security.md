# Security and privacy

**Status:** Proposed

**Last reviewed:** 2026-07-13

Authoritative for **security** (coverage matrix item 14). This is a threat-model-oriented design proposal, not a compliance certification.

**No unsupported regulatory compliance claim** is made (HIPAA, GDPR certification, SOC2, etc.). Legal review is required before any compliance marketing.

---

## Assets

| Asset | Sensitivity |
|-------|-------------|
| Wearable raw payloads and facts | High (health) |
| Scores, factors, recommendations | High (health-derived) |
| Conversations, journal, egress manifests | High |
| Optional vector embeddings of summaries/journal/knowledge | High (derived; never default raw measurements) |
| Provider OAuth tokens | High (credential) |
| Model API keys | High (credential) |
| Session tokens | High |
| Display names and email | High (sensitive PII; not “non-PHI”) |
| Private export objects and restore keys | High |
| Restoration-suppression ledger + dedicated deletion key | High (access-separated) |
| Turso DB, local DB, backups at rest | High |
| Audit metadata / deletion completion proofs | Medium (metadata-only; no health values) |
| Public marketing site (future) | Low |

---

## Threat model (STRIDE-style summary)

| Threat | Example | Mitigations |
|--------|---------|-------------|
| Spoofing | Stolen session | httpOnly cookies, rotation, hashed session tokens, OIDC state/nonce/PKCE, logout revoke |
| Tampering | Modified score client-side | Scores only from API; no trusted client formulas |
| Repudiation | "I didn't export" | Tamper-evident audit events (**no** health values) |
| Information disclosure | Logs with HR values | PHI-free logs; **pseudonymized** tenant labels; redaction |
| Denial of service | Sync storms | Rate limits, job backoff, per-tenant caps |
| Elevation of privilege | Cross-tenant read | Composite tenant auth on every query; no libSQL RLS; integration tests |
| Confused deputy / prompt injection | Model tricks tool into export | Tool authz re-check; confirmation binding; untrusted tool results |
| SSRF | Local model URL to metadata IP | Outbound allowlist for local model endpoints |

---

## Authentication and session

| Topic | Proposal |
|-------|----------|
| User login | OIDC **authorization code + PKCE** with **`state`** and **`nonce`** |
| Session | **Backend-issued** opaque session cookie; store **`token_hash` only** server-side—**never** the raw cookie token |
| CSRF | Required for cookie-authenticated state-changing requests; store **`csrf_secret_hash`** or a **derived** CSRF secret from server-only key material—never plaintext CSRF secret in DB |
| Session rotation | Rotate session identifier on privilege change and at configured intervals |
| Cookie | `Secure`, `HttpOnly`, `SameSite=Lax` or `Strict` as fit for OAuth returns |
| CORS | Explicit allowlist of web origins |
| CSP | Strict default-src; no inline unless nonces |
| Logout | Server revoke + cookie clear |

### Provider OAuth state rows

| Topic | Proposal |
|-------|----------|
| State | Store **`state_hash`** only (never raw `state`) |
| PKCE | Store **envelope-encrypted** `code_verifier` (`code_verifier_ciphertext` + key version) |
| Redirect | Store **exact** `redirect_uri`; callback must match exactly |
| Use | **Single-use** (`consumed_at`); enforce `expires_at`; reject reuse |

---

## Authorization and tenancy

- Every application service method takes `AuthContext { user_id, tenant_id, scopes }`.
- Repositories **always** filter by `tenant_id` (**composite tenant auth**).
- **libSQL / Turso has no RLS**; application-layer isolation is mandatory and tested.
- No "superuser" path in MVP application code; operator access is a future break-glass design with separate audit.
- SSE connect/reconnect re-authorizes tenant and conversation ownership.

---

## Provider OAuth least privilege

| Provider | Principle |
|----------|-----------|
| Oura | Request only scopes needed for sleep/overnight streams used |
| Google Health | Google OAuth; restricted scopes and security review; google-wearables for Fitbit-origin daytime policy |
| Polar | AccessLink scopes for exercises/load only as needed |

Additional controls:

- **state_hash** and envelope-encrypted **PKCE** verifier where the provider supports them
- **Exact redirect URI** checks
- **Serialized refresh** (single-flight) to avoid token races
- **Revocation** on disconnect
- Tokens in `connection_secrets` with **envelope encryption**; refresh server-side only

Display names and email are **sensitive PII** (never treat as non-PHI free labels). Legacy Fitbit Web API is **not** an MVP connector.

---

## Webhooks

- **Provider-specific** signed webhooks—not a single generic HMAC scheme for all vendors.
- Google Health: rotating public-key signatures + endpoint authorization as documented.
- Oura / Polar: their documented signature schemes.
- Replay: timestamp skew window + durable `webhook_inbox` dedupe by delivery id/hash.
- Verify then ack quickly; heavy work is job-based refetch.

---

## Encryption at rest and key management

| Class | Protection |
|-------|------------|
| **Turso production DB** | Platform encryption at rest + app-level envelope for secrets columns |
| **Local DB** (dev/CI) | Disk/volume encryption where feasible; never commit DB files with real PHI |
| **Backups** | Encrypted backups; keys separated from backup blobs |
| **Raw data / private export objects** | Encrypted object storage; time-limited download URLs; no public buckets |
| **Restore keys** | Separated KEK/DEK hierarchy; restore keys not colocated with ciphertext alone |
| **Dedicated deletion key** | Separate from general DB encryption; HMAC selectors for restoration-suppression ledger only |
| Provider OAuth tokens | Envelope-encrypted in DB (`ciphertext` + `key_version`) |
| OAuth PKCE verifiers | Envelope-encrypted in `oauth_states` |
| Model provider API keys | Envelope-encrypted user keys; secret manager preferred for platform keys |
| Optional vector rows | Tenant-scoped; deleted with source; not a substitute for encrypting parent data |

**Key separation and rotation:** KEKs in external KMS/secret manager; re-encrypt on read or batch; bump `key_version`; document rotation runbooks.

---

## Transport and browser security

| Control | Proposal |
|---------|----------|
| TLS | Required on all external endpoints |
| Authenticated API | `Cache-Control: private, no-store`; CDN bypass for health |
| Browser storage | No persistent health JSON in `localStorage`/`IndexedDB` by default |
| PWA SW | **NetworkOnly** for authenticated `/v1` data—not network-first durable health cache |

---

## Logging and audit

| Allowed in logs | Forbidden in logs |
|-----------------|-------------------|
| request_id, **pseudonymized** tenant label, route, status, latency | Raw **tenant UUIDs** as correlatable PHI handles when avoidable; never HR, HRV, sleep minutes, raw payloads |
| job_type, error_class | Token values, Authorization headers |
| tool name, non-health resource ids | Model prompt full health dumps |

Audit events: action + resource metadata **without health values**; prefer **tamper-evident** append (hash chain or signed batches as implementation matures).

General logs are **pseudonymized**; do not treat raw tenant ids as free log labels.

---

## Model egress and agent isolation

1. User grants purpose- and data-scope-specific consents; persisted **context manifest** is provider/model/purpose/scope specific.
2. Review provider **no-training / data-use** policy before enable.
3. Only structured summaries leave the trust boundary by default.
4. **Prompt injection** and confused-deputy: tools re-check authz; confirmation required for mutations; model **cannot** confirm.
5. Local model endpoints: **outbound allowlist**, SSRF blocks for link-local/metadata ranges.
6. No silent model fallback.
7. Missing/failed **agent-worker** cannot affect ingestion, engine, recommendations, notifications, dashboard, or export.
8. Intentional disable → **409 `agent_disabled`**; outage → **503**.
9. Optional embeddings never become a required path for scores/export; delete with source data.

---

## Export, disconnect, deletion

| Operation | Behavior |
|-----------|----------|
| **Export** | Job builds archive into **private encrypted object storage**; time-limited download; audited; expiry enforced |
| **Disconnect** | Revoke vendor token best-effort; delete secrets; historical facts retained until privacy delete or product policy says otherwise |
| **Privacy delete** | Cancel/drain related work; **hard-delete** health, conversation, egress, **vector**, and export data; revoke credentials; write **two artifacts** (below) |

Deletion pipeline states are tracked in `deletion_requests` ([data-model.md](data-model.md)).

### Two deletion artifacts (no single contradictory “forever non-linkable ledger”)

| Artifact | Contents | Retention | Access |
|----------|----------|-----------|--------|
| **Minimal completion / audit proof** | Non-identifying completion id, timestamp, status, scrub class counts—**no** health values, **no** email/display name | Audit retention policy | Ordinary audit access |
| **Restoration-suppression ledger** | **Only** HMAC-derived deleted-tenant/object selectors under a **dedicated deletion key**—**no** identity or health values | Until **all relevant backups expire + 30 days**, then **destroy** ledger entries and retire key material for those selectors | **Access-separated** from primary app credentials |

**Restore rule:** load and apply the restoration-suppression ledger **before** restored data is served. Do **not** claim permanent tenant-lifetime non-linkability: operators who hold the deletion key can recompute selectors; after key destruction, residual risk is bounded by cryptographic erasure assumptions, not absolute permanent unlinkability.

---

## Jobs security

- Job payloads avoid embedding raw health blobs when a FK suffices.
- Lease fencing prevents stale workers from writing after reassignment.
- Dead-letter payloads redacted for operator UIs.
- Agent jobs isolated by worker role; core worker never requires model credentials.

---

## Explicit non-claims

- This design does **not** assert HIPAA covered-entity readiness.
- This design does **not** assert GDPR certification or DPIA completion.
- Wellness guidance is **not** a medical device claim in this documentation.
- No compliance badge or certification is implied by encryption or audit design alone.

---

## Related

- [operations.md](operations.md)
- [api-tools-and-agent.md](api-tools-and-agent.md)
- [../product-principles.md](../product-principles.md)
