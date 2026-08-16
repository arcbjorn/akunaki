# Connector setup

**Status:** Describes shipped code (`backend/src/akunaki/adapters/connectors/`, `backend/src/akunaki/api/routes/connections.py`, `backend/src/akunaki/api/routes/webhooks.py`)

**Last reviewed:** 2026-08-15

Three connectors are implemented. Each needs OAuth credentials to be linkable
and, separately, webhook configuration to receive push notifications. The two
are independent: a connector works without webhooks (the worker's reconcile
sweep polls every 30 minutes), but not without OAuth.

---

## The path key is not the brand name

The provider string appears in environment variables, URL paths, and webhook
paths, and **all three use the same key**:

| Provider | Path key | Env var prefix |
|----------|----------|----------------|
| Oura | `oura` | `AKUNAKI_OURA_` |
| Polar | `polar` | `AKUNAKI_POLAR_` |
| Google Health | **`google_health`** | `AKUNAKI_GOOGLE_HEALTH_` |

`google_health` — with the underscore, not `google`, not `googlehealth`, not
`google-health`. The key is compared against a fixed set
(`oauth_client_factory.py`), so a near-miss is an unknown provider, which is an
indistinguishable `404`. This matters in three places at once: the env var
name, the callback URL you register with Google, and the webhook path.

---

## Partial credentials are invisible

`Settings.connector_oauth(provider)` returns a config **only when client id,
client secret, and redirect URI are all non-empty**. Any one missing and it
returns `None`, which the route treats identically to a provider that does not
exist:

```
GET /v1/connections/oura/authorize
→ 404 {"code": "provider_not_configured"}      ← two of three set

GET /v1/connections/nonsense/authorize
→ 404 {"code": "unknown_provider"}
```

Both are `404`. The distinction in the body exists, but the deployment-level
symptom is the same, and `GET /v1/providers` omits partially-configured
providers entirely — it deliberately does not become a back door around the
`404`.

This is a design choice (no half-built connect surface), not a bug. The
practical rule: **set all three or none.** If a provider you configured does not
appear in `/v1/providers`, check for an empty or whitespace-only value among its
three variables before looking anywhere else.

---

## Callback URLs

The connector callback path is under `/v1` — unlike the OIDC login callback:

```
https://<your host>/v1/connections/oura/callback
https://<your host>/v1/connections/polar/callback
https://<your host>/v1/connections/google_health/callback
```

Register that URL at the provider's developer portal **and** set the identical
value as `AKUNAKI_<PROVIDER>_REDIRECT_URI`. The value is sent on the authorize
leg and again on the token exchange; a mismatch fails at the vendor.

Note the callback is **session-authenticated**: the browser must still hold the
akunaki session cookie when the provider redirects back. The session cookie is
`SameSite=Lax` precisely so it survives that top-level cross-site navigation.

---

## Per-provider notes

### Oura

| Item | Value |
|------|-------|
| Path key | `oura` |
| Credentials from | An OAuth application registered at the [Oura developer portal](https://developer.ouraring.com/applications) (sign-in required); see the [authentication docs](https://cloud.ouraring.com/docs/authentication) for the flow |
| Authorize endpoint | `https://cloud.ouraring.com/oauth/authorize` |
| Token endpoint | `https://api.ouraring.com/oauth/token` |
| API base | `https://api.ouraring.com/v2/usercollection` |
| Callback to register | `https://<host>/v1/connections/oura/callback` |
| Flow | Authorization code **with PKCE** |
| Scopes requested | `daily` |
| Webhook | HMAC-SHA256, header `X-Oura-Signature` |

**The scope caveat.** Oura's OpenAPI spec declares an empty scope list on every
endpoint, so the vendor publishes no per-endpoint scope mapping. `daily` is the
only real scope whose description mentions sleep, so it is what the code
requests. The failure mode is quiet: **an under-scoped Oura token returns an
empty array, not an error**, so a scope shortfall looks like "this account has
no data". The initial-sync handler warns when a full-lookback backfill yields
zero records — treat that warning as a likely scope problem, not an empty
account.

### Polar

| Item | Value |
|------|-------|
| Path key | `polar` |
| Credentials from | A registered Polar AccessLink client ([AccessLink docs](https://www.polar.com/accesslink-api/)) |
| Authorize endpoint | `https://flow.polar.com/oauth2/authorization` |
| Token endpoint | `https://polarremote.com/v2/oauth2/token` |
| API base | `https://www.polaraccesslink.com/v3` |
| Callback to register | `https://<host>/v1/connections/polar/callback` |
| Flow | Authorization code, **no PKCE**, token request uses HTTP Basic auth |
| Scopes requested | `accesslink.read_all` |
| Webhook | HMAC-SHA256, header `Polar-Webhook-Signature` |

**Polar requires user registration (`POST /v3/users`).** AccessLink serves **no
exercise data at all** until the authorized user is registered to the calling
client. The connector does this automatically after the token exchange
(`PolarOAuthClient.enroll_user`), sending only the vendor's own user id. A `409
Conflict` is treated as success — the user is already registered to this client,
which is the desired end state, and re-consenting to an existing connection
could otherwise never complete.

If registration fails, every subsequent fetch returns `403` and the connection
looks mis-credentialed rather than un-enrolled. Look for
`polar user registration rejected` in the logs.

Two more Polar-specific facts worth knowing before you diagnose "missing
workouts":

- **No refresh token.** AccessLink access tokens are long-lived and a refresh is
  never possible; a dead grant goes straight to `needs_reauth` and requires
  re-linking.
- **A 30-day retention horizon.** Only exercises uploaded to Flow in the last 30
  days are returned, and only those uploaded after the user linked this client.
  The connector re-reads Polar's entire retention set on every sync (no date
  filter), so losing a workout requires no successful sync for 30 consecutive
  days — and `/v1/data-quality` raises `connection_stale_sync` after one day.

### Google Health

| Item | Value |
|------|-------|
| Path key | **`google_health`** |
| Credentials from | An OAuth 2.0 client in a Google Cloud project with the Health API enabled ([setup docs](https://developers.google.com/health/setup)) |
| Authorize endpoint | `https://accounts.google.com/o/oauth2/v2/auth` |
| Token endpoint | `https://oauth2.googleapis.com/token` |
| API base | `https://health.googleapis.com/v4` |
| Callback to register | `https://<host>/v1/connections/google_health/callback` |
| Flow | Authorization code **with PKCE**, `access_type=offline`, `prompt=consent` |
| Webhook | Pub/Sub push OIDC token (not HMAC) |

Scopes requested:

```
https://www.googleapis.com/auth/googlehealth.sleep.readonly
https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly
```

**These are Restricted scopes.** Google requires a security review (its
Restricted-scope verification process) before an app using them may be used in
production by users outside your test group. Plan for that lead time — it is
typically the longest pole in standing up this connector. Until verification
completes, the app works only for accounts you have added as test users.

**The `googlehealth.` prefix is load-bearing.** Google's data-types reference
lists these scopes by *suffix* only (`.sleep.readonly`,
`.activity_and_fitness.readonly`), which invites writing the bare
`.../auth/activity_and_fitness.readonly`. That form is rejected by Google's
authorize endpoint with "Some requested scopes were invalid". Both full strings
above were verified against the live authorize endpoint on 2026-08-13.

Unlike Oura, an under-scoped Google token fails **loudly** with `403
PERMISSION_DENIED` rather than returning an empty page, so a missing scope is
not mistaken for an account with no data.

---

## Webhooks

The path is the same for all three providers:

```
POST /webhooks/{provider}/{connection_id}
```

It is **unauthenticated** in the session sense — the delivery comes from the
vendor, not a browser — so all trust comes from per-provider verification. A
verified delivery is deduplicated, recorded, and used to enqueue an ordinary
incremental-sync job. **The delivered body is never trusted as data**; the
webhook only triggers a pull.

Register the URL at each vendor's portal with the correct path key and the
connection id of the link (from `GET /v1/connections`).

### Verification differs per provider

| Provider | Scheme | Required configuration |
|----------|--------|------------------------|
| Oura | HMAC-SHA256 over the exact request body, constant-time compare | `AKUNAKI_OURA_WEBHOOK_SECRET` |
| Polar | Same | `AKUNAKI_POLAR_WEBHOOK_SECRET` |
| Google Health | Google-signed **Pub/Sub push OIDC token** (Bearer JWT) | `AKUNAKI_GOOGLE_HEALTH_PUSH_AUDIENCE` **and** `AKUNAKI_GOOGLE_HEALTH_PUSH_SERVICE_ACCOUNT` |

For the HMAC providers, the signature is read from `X-Oura-Signature` and
`Polar-Webhook-Signature` respectively. An optional `sha256=` prefix on the hex
digest is tolerated.

For Google Health, the JWT is verified against Google's rotating JWKS
(`https://www.googleapis.com/oauth2/v3/certs`), accepting **asymmetric
algorithms only** (RS256/384/512, ES256/384) — HS256 is refused, closing the
alg-confusion downgrade. The claims must then satisfy all of: issuer is Google,
`aud` equals your configured push audience, `exp` is in the future (60s skew
tolerance), `email` equals your configured push service account, and
`email_verified` is true. Both env vars are therefore mandatory — the verifier
raises on construction if either is blank, which is why the route disables the
path rather than attempting verification.

### What "disabled" looks like

**A `404`, for every provider.**

```
POST /webhooks/oura/<connection_id>          → 404 {"code": "no_webhook"}
POST /webhooks/google_health/<connection_id> → 404 {"code": "no_webhook"}
```

A provider is "enabled" only when its verification configuration is complete:
the shared secret for Oura and Polar, **both** push variables for Google
Health. One of the two Google variables set is the same as neither — a `404`.

The `404` is deliberate: webhook ingress must reveal nothing about which
providers *could* be enabled. It is also indistinguishable from a wrong path
key, so check the spelling of `google_health` at the same time you check the
variables.

### Other webhook responses

| Status | Body | Meaning |
|--------|------|---------|
| `200` | `{"status": "accepted"}` | Verified, recorded, refetch enqueued |
| `200` | `{"status": "duplicate"}` | Vendor redelivery, already recorded; idempotent no-op |
| `401` | `{"code": "invalid_signature"}` | Verification failed — **or** the connection id is unknown or belongs to a different provider |
| `404` | `{"code": "no_webhook"}` | That provider's webhook is not configured |

The `401` deliberately conflates a bad signature with an unknown connection, so
an unverified caller cannot probe which connections exist. Deduplication uses
the vendor's `X-Delivery-Id` header when present, otherwise a SHA-256 of the
body.

---

## Verifying a connector end to end

1. `GET /v1/providers` (session-authenticated) — the provider must appear. If
   it does not, its OAuth credentials are incomplete.
2. `GET /v1/connections/{provider}/authorize` — returns `{"authorize_url": ...,
   "provider": ...}`. Like the login route, this is **JSON, not a redirect**.
3. Navigate to the URL, consent, get redirected back to the callback. A `200`
   with a `connection_id` means linked.
4. `GET /v1/connections` — check `status` is `active` and watch
   `last_success_at` and `raw_revisions` after the first sync.
5. `POST /v1/connections/{connection_id}/sync` with an `Idempotency-Key` header
   to force a sync rather than waiting for the 30-minute reconcile sweep.
6. `GET /v1/data-quality` — `connection_stale_sync` after a day of no successful
   sync is the earliest signal something is wrong.

`last_error_class` on a connection carries an error **class** only, never a
vendor body or message — vendor responses are never logged, because a token
endpoint response can contain credentials. Diagnose from the class plus the
server logs.
