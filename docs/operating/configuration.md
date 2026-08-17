# Configuration reference

**Status:** Describes shipped code (`backend/src/akunaki/config.py`)

**Last reviewed:** 2026-08-16

Configuration is read by pydantic-settings with the **`AKUNAKI_` prefix**. Every
field below is one entry in `Settings`; the environment variable is the field
name upper-cased with that prefix. Unknown `AKUNAKI_*` variables are ignored
(`extra="ignore"`), so a typo is silent — check spelling against this table.

A `.env` file in the process working directory is also read. In a container,
prefer real environment variables and platform secrets; do not bake a `.env`
into an image.

Settings are cached process-wide (`get_settings` is `lru_cache`d). A
configuration change requires a process restart.

---

## Core process

| Variable | Default | Effect |
|----------|---------|--------|
| `AKUNAKI_SERVICE_NAME` | `akunaki-api` | Identity string reported in `/healthz`. Cosmetic. |
| `AKUNAKI_DATABASE_URL` | `sqlite+libsql:///.local/akunaki.db` | The operational store. **Local `sqlite+libsql:` URLs only.** |
| `AKUNAKI_ECHO_SQL` | `false` | Echo SQL to logs. Development only — it logs query text. |
| `AKUNAKI_API_HOST` | `127.0.0.1` | Bind interface. **Containers must set `0.0.0.0`.** |
| `AKUNAKI_API_PORT` | `8000` | Bind port. Validated to 1–65535. |

### `AKUNAKI_DATABASE_URL` accepted forms

A validator rejects anything else **at settings load**, so a bad value stops
the process rather than degrading it:

| Form | Example |
|------|---------|
| Relative file | `sqlite+libsql:///.local/akunaki.db` |
| Absolute file | `sqlite+libsql:////var/lib/akunaki/akunaki.db` (four slashes) |
| In-memory (official) | `sqlite+libsql://` |
| In-memory (path form) | `sqlite+libsql:///:memory:` |

Rejected: any hostname, username, password, port, query string, or fragment —
which means remote Turso URLs and `?authToken=`/`?syncUrl=` parameters cannot
be configured through this variable at all. Use an absolute path in production;
a relative path resolves against the process working directory (`/app` in the
image), which is not the volume.

---

## Encryption at rest

OAuth tokens and other stored secrets are sealed with AES-256-GCM envelope
encryption. Losing the KEK means losing every stored connector credential.

| Variable | Default | Effect |
|----------|---------|--------|
| `AKUNAKI_SECRET_KEKS` | `""` (empty) | KEKs as comma-separated `version:base64key` pairs. Each key must base64-decode to **exactly 32 bytes**. |
| `AKUNAKI_ACTIVE_KEK_VERSION` | `""` (empty) | Which version new secrets are sealed under. |

**What empty does.** Empty `AKUNAKI_SECRET_KEKS` disables secret sealing. It is
not a silent degradation: any code path that needs to seal or unseal —
completing an OIDC login, linking a connector — raises `SecretConfigError` at
the point of use with a message naming the variable. The process starts; the
first login fails.

**`AKUNAKI_ACTIVE_KEK_VERSION` is conditionally required** (`adapters/crypto/config.py`):

- **One KEK configured:** optional. It defaults to that sole version.
- **Two or more KEKs configured:** **required.** Boot of the sealer fails with
  `AKUNAKI_ACTIVE_KEK_VERSION is required when multiple KEKs are configured`.
  This is the rotation case, so the trap fires exactly when you are mid-rotation.
- If set to a version not present in `AKUNAKI_SECRET_KEKS`, it fails with
  `active KEK version ... is not present`.

**Rotation.** Keep old versions in `AKUNAKI_SECRET_KEKS` after rotating —
existing ciphertext is sealed under the version it was written with, and
dropping a version makes that data permanently unreadable. Add the new key,
then point `AKUNAKI_ACTIVE_KEK_VERSION` at it. Error messages name the offending
*version* only, never key material.

Generate a key with any 32 random bytes, base64-encoded. Supply it from your
platform secret store; never commit one.

---

## OIDC login

| Variable | Default | Effect |
|----------|---------|--------|
| `AKUNAKI_OIDC_ISSUER` | `""` (empty) | Issuer URL, e.g. `https://auth.example.com`. **Empty means the login routes are not mounted at all.** |
| `AKUNAKI_OIDC_CLIENT_ID` | `""` | OIDC client id. |
| `AKUNAKI_OIDC_CLIENT_SECRET` | `""` | OIDC client secret. |
| `AKUNAKI_OIDC_REDIRECT_URI` | `""` | The exact callback URI registered at the IdP. Must match at the callback. |

**What empty `AKUNAKI_OIDC_ISSUER` does.** The `auth` router is conditionally
included in `create_app` — with an empty issuer it is never registered, so
`/auth/login` and `/auth/callback` are plain **404s**, not 500s or "not
configured" errors. There is no half-built auth surface. Note that only the
*issuer* gates mounting: setting the issuer while leaving the client id or
secret empty mounts the routes and fails at the IdP instead.

Because a user row can only be created by a completed login, this block is a
hard prerequisite for everything else. See [bootstrap.md](bootstrap.md).

---

## Browser access

| Variable | Default | Effect |
|----------|---------|--------|
| `AKUNAKI_SESSION_COOKIE_SECURE` | `true` | `Secure` attribute on the session cookie. Turn off **only** for local HTTP development. |
| `AKUNAKI_CORS_ALLOWED_ORIGINS` | `()` (empty) | Exact browser origins allowed to make credentialed cross-origin requests. |

**What empty `AKUNAKI_CORS_ALLOWED_ORIGINS` does.** No CORS middleware is added
— meaning no cross-origin browser access. That is correct for a same-origin
deployment (the PWA served from the same origin) or a server-to-server one. A
browser app on a different origin will fail its preflight.

**The value must be JSON, not a comma-separated list.** The field is a tuple, so
pydantic-settings parses it as JSON:

```
AKUNAKI_CORS_ALLOWED_ORIGINS=["https://app.example.com"]
```

A bare `https://a.example,https://b.example` raises a `SettingsError` and the
process does not start. When configured, credentials are allowed, methods are
limited to `GET` and `POST`, request headers to `content-type`,
`x-akunaki-csrf`, and `x-request-id`, and `x-request-id` is exposed to the
client. A wildcard origin is never used with credentials.

---

## Metrics

| Variable | Default | Effect |
|----------|---------|--------|
| `AKUNAKI_METRICS_ENABLED` | `false` | Mount `GET /metrics` (Prometheus text format). |

**What `false` does.** The route is not registered — `/metrics` is a 404.

The endpoint is **unauthenticated by design** (a scraper cannot hold a session
cookie). It is PHI-free by construction: counts and liveness gauges labelled
with bounded tokens, never a tenant id, user id, or health value. It still
describes internal operations, so expose the port to your scraper's network,
not the internet.

Only the **API** process serves it. The worker has a separate in-process
registry and no HTTP server; observe the worker through `/readyz` instead.

---

## Public training calendar

| Variable | Default | Effect |
|----------|---------|--------|
| `AKUNAKI_PUBLIC_TRAINING_TENANT_ID` | `""` (empty) | Mount `GET /v1/public/training` for exactly this tenant. |

**What empty does.** The route is not registered — `/v1/public/training` is a
404.

**What it discloses.** For the named tenant, the last 30 local days ending on
the tenant's local today (under its stated `primary_timezone`), each marked
`trained` or not — a day counts when at least one workout session was recorded
— plus the providers that recorded a session in the window. **Nothing else**:
no times, zone minutes, loads, counts, scores, or vitals. It exists for a personal public page and is
**unauthenticated by design**, served with `Cache-Control: public, max-age=3600`
and `Access-Control-Allow-Origin: *`, so a browser on any origin may read it and
an edge cache can absorb the traffic.

**Name the tenant explicitly.** The value is a tenant id (see `/v1/me`), not a
boolean. If it names a tenant that does not exist the route answers `503
public_training_unavailable` rather than an empty calendar — an empty calendar
would be a fabricated "never trains".

---

## History window

| Variable | Default | Effect |
|----------|---------|--------|
| `AKUNAKI_LOOKBACK_DAYS` | `30` | How far back a backfill reaches, in days. Validated to 1–3650; anything outside stops the process at settings load. |

**What it governs: first sync only.** The window applies to a connection's
initial backfill, and to any later sync for a stream that still has no cursor.
An established connection resumes from its stored cursor minus a 36-hour
overlap, so raising or lowering this value does not change what an already-
syncing connection fetches. To re-read further back on a connection that has
already synced, you must clear its cursor — changing this variable alone will
not do it.

**Effect on the first sync.** With the default, linking a connector fetches
roughly the last 30 days per stream (plus the overlap, and widened where a
vendor imposes a minimum window — Google Health v4 refuses ranges narrower than
14 days). Raising it to `365` makes that first sync proportionally larger: more
vendor calls, more pages, more raw revisions, and a longer first run. It is
safe to raise — re-fetched records deduplicate on content hash, so a wider
window never produces duplicate facts — but a provider serves only what it
retains, so a large value cannot conjure history the vendor no longer has
(Polar's `/v3/exercises`, for instance, returns its own 30-day retention set
and ignores the requested bounds entirely).

**It is a worker setting.** The value reaches sync behaviour through the worker's
handler wiring, so it must be set on the **worker** process. Setting it only on
the API has no effect. Like every setting, it is read once at boot; changing it
requires a restart.

---

## Connector OAuth

Nine variables in three symmetric groups. All three of a provider's variables
must be non-empty or the provider is treated as absent — see
[connectors.md](connectors.md) for portal URLs, scopes, and callback paths.

| Variable | Default |
|----------|---------|
| `AKUNAKI_OURA_CLIENT_ID` | `""` |
| `AKUNAKI_OURA_CLIENT_SECRET` | `""` |
| `AKUNAKI_OURA_REDIRECT_URI` | `""` |
| `AKUNAKI_POLAR_CLIENT_ID` | `""` |
| `AKUNAKI_POLAR_CLIENT_SECRET` | `""` |
| `AKUNAKI_POLAR_REDIRECT_URI` | `""` |
| `AKUNAKI_GOOGLE_HEALTH_CLIENT_ID` | `""` |
| `AKUNAKI_GOOGLE_HEALTH_CLIENT_SECRET` | `""` |
| `AKUNAKI_GOOGLE_HEALTH_REDIRECT_URI` | `""` |

Note the key is **`GOOGLE_HEALTH`**, not `GOOGLE`.

---

## Connector webhooks

| Variable | Default | Effect |
|----------|---------|--------|
| `AKUNAKI_OURA_WEBHOOK_SECRET` | `""` | Shared secret for HMAC-SHA256 verification. Empty → Oura webhooks 404. |
| `AKUNAKI_POLAR_WEBHOOK_SECRET` | `""` | Same, for Polar. Empty → Polar webhooks 404. |
| `AKUNAKI_GOOGLE_HEALTH_PUSH_AUDIENCE` | `""` | Expected `aud` claim on the Google Pub/Sub push OIDC token. |
| `AKUNAKI_GOOGLE_HEALTH_PUSH_SERVICE_ACCOUNT` | `""` | Expected service-account email on that token. |

Google Health needs **both** push variables; either one empty disables its
webhook path. See [connectors.md](connectors.md#webhooks).

---

## Minimum viable configuration

The smallest set that produces a working single-user deployment:

```
AKUNAKI_DATABASE_URL=sqlite+libsql:////var/lib/akunaki/akunaki.db
AKUNAKI_API_HOST=0.0.0.0
AKUNAKI_SECRET_KEKS=v1:<32 random bytes, base64>
AKUNAKI_OIDC_ISSUER=https://auth.example.com
AKUNAKI_OIDC_CLIENT_ID=<from your IdP>
AKUNAKI_OIDC_CLIENT_SECRET=<from your IdP>
AKUNAKI_OIDC_REDIRECT_URI=https://<your host>/auth/callback
```

`AKUNAKI_ACTIVE_KEK_VERSION` is omitted deliberately: with one KEK it is
optional. Add at least one connector group before any data will arrive.
