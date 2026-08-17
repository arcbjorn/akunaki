# HTTP surface

**Status:** Describes shipped code (`backend/src/akunaki/api/app.py`, `backend/src/akunaki/api/routes/`)

**Last reviewed:** 2026-08-16

The complete public path list, what authenticates each one, and which paths
exist only under certain configuration.

---

## Path prefixes

Most routers mount under `/v1`. **Four groups do not**, and each is outside
`/v1` for a reason that matters operationally:

| Prefix | Routes | Why it is not under `/v1` |
|--------|--------|---------------------------|
| `/auth` | `/auth/login`, `/auth/callback` | The callback path is registered at your IdP |
| `/webhooks` | `/webhooks/{provider}/{connection_id}` | The path is registered at each vendor's portal |
| *(root)* | `/healthz`, `/readyz` | Probe targets, conventionally unversioned |
| *(root)* | `/metrics` | Scrape target, conventionally unversioned |

### The `/auth` exception is the one that bites

The OIDC redirect URI is registered **at your identity provider**, and both
sides must agree exactly. The login routes are at:

```
GET /auth/login
GET /auth/callback     ← register THIS path at the IdP
```

Not `/v1/auth/callback`. Register the wrong one and nothing complains: the
config loads, the process starts, `/healthz` is `ok`, `/readyz` is `ready`, and
the deployment looks entirely healthy. It fails the first time a human tries to
log in — with an error from the IdP, not from this service, which sends you
looking in the wrong logs.

Set `AKUNAKI_OIDC_REDIRECT_URI` to the same full URL you registered, e.g.
`https://your-host/auth/callback`. The value is sent on both legs of the flow
and must match at the IdP and at the callback.

**Connector callbacks are different** — those *are* under `/v1`, at
`/v1/connections/{provider}/callback`. So a deployment registers callback paths
in two different shapes with two different parties. See
[connectors.md](connectors.md).

---

## Login is a two-leg JSON flow, not a redirect

This is the single most common misdiagnosis of a working deployment.

```
GET /auth/login
→ 200 application/json
  {"authorize_url": "https://auth.example.com/authorize?..."}
```

**There is no `RedirectResponse` anywhere in the login path.** The route returns
a `LoginStartResponse` model with one field. The client — a browser app, or a
human with curl — reads `authorize_url` and navigates there itself.

Operators (and HTTP clients configured to follow redirects) expect a `302` here.
When they get a `200` with a JSON body, the usual conclusion is "auth is
broken" or "the IdP isn't wired up". Neither is true. A `200` with an
`authorize_url` is a **successful** first leg.

The second leg is the IdP redirecting the browser back to your registered
callback:

```
GET /auth/callback?state=...&code=...
→ 200, Set-Cookie: akunaki_session=...; HttpOnly; Secure; SameSite=Lax; Path=/
  {"tenant_id": "...", "user_id": "...", "csrf_secret": "...",
   "session_expires_at": "..."}
```

The cookie authenticates subsequent requests. The `csrf_secret` is returned in
the body — **shown once** — and a client echoes it in the `X-Akunaki-CSRF`
header on every state-changing request. Both legs send `Cache-Control:
private, no-store`.

Failures on the callback are a single generic `401 {"code":
"unauthenticated"}`, regardless of which check failed. Diagnose from the server
logs.

---

## Authentication modes

| Mode | Applies to | Mechanism |
|------|-----------|-----------|
| Session cookie | Everything under `/v1` except as noted | `akunaki_session` cookie; `X-Akunaki-CSRF` header additionally required on `POST`/`PUT`/`PATCH`/`DELETE` |
| Session **or** bearer | `/v1/tools` only | Cookie session, or `Authorization: Bearer <service token>`; which tools a token may invoke depends on its scope |
| Vendor signature | `/webhooks/*` | Per-provider HMAC or Google push OIDC token — no session |
| None | `/healthz`, `/readyz`, `/metrics`, `/auth/*`, `/v1/public/*` | Unauthenticated by design |

An `Authorization` header on `/v1/tools` **commits** the caller to the bearer
path: a request carrying both a bearer token and a cookie is not silently
downgraded to the cookie, so a rejected token cannot ride along on ambient
cookie auth. Bearer requests skip CSRF deliberately — CSRF defends ambient
cookie authority, which a header the caller must attach does not have.

CSRF failures return `403`, not `401`: the caller *is* authenticated; the
request just cannot be attributed to a deliberate action.

---

## Complete path list

### Always mounted, unauthenticated

| Method | Path | Notes |
|--------|------|-------|
| GET | `/healthz` | Liveness. Always `200`. See [health-and-probes.md](health-and-probes.md) |
| GET | `/readyz` | Readiness. `503` when not ready |
| POST | `/webhooks/{provider}/{connection_id}` | Vendor-signed. `404` when that provider's webhook is unconfigured |

### Mounted only when `AKUNAKI_OIDC_ISSUER` is set

| Method | Path | Notes |
|--------|------|-------|
| GET | `/auth/login` | Returns `{"authorize_url": ...}` — **not a redirect** |
| GET | `/auth/callback` | Query: `state`, `code`. Sets the session cookie |

Unset issuer → both are `404`.

### Mounted only when `AKUNAKI_METRICS_ENABLED=true`

| Method | Path | Notes |
|--------|------|-------|
| GET | `/metrics` | Prometheus text format, unauthenticated, PHI-free |

Disabled → `404`.

### Mounted only when `AKUNAKI_PUBLIC_TRAINING_TENANT_ID` is set

| Method | Path | Notes |
|--------|------|-------|
| GET | `/v1/public/training` | Unauthenticated. The named tenant's last 30 local days, each `trained` or not. No times, zones, loads, counts, or other measurement. `Cache-Control: public, max-age=3600`, `Access-Control-Allow-Origin: *`. `503 public_training_unavailable` when the named tenant does not exist |

This is the only `/v1` path that takes no session. It is the one surface that
answers *anyone* about one operator-named tenant, which is why it is unmounted
by default and discloses a boolean per day and nothing else. See
[configuration.md](configuration.md#public-training-calendar).

### Session-authenticated (`/v1`)

Always mounted — every endpoint requires a valid session, so mounting them
exposes nothing on its own.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/v1/session` | Current session |
| POST | `/v1/session/logout` | Revoke this session |
| POST | `/v1/session/logout-everywhere` | Revoke all of the user's sessions |
| GET | `/v1/me` | The caller's own account |
| GET | `/v1/today` | Today's summary |
| GET | `/v1/recovery` | Recovery score |
| GET | `/v1/sleep` | Sleep summary (deterministic, carries no score field) |
| GET | `/v1/trends` | Multi-metric trends |
| GET | `/v1/metrics` | Supported metric names |
| GET | `/v1/metrics/{metric}` | One metric's series |
| GET | `/v1/workouts` | Paginated workout list |
| GET | `/v1/workouts/{workout_id}` | One workout |
| GET | `/v1/anomalies` | Detected anomalies |
| GET | `/v1/recommendations` | Recommendations |
| POST | `/v1/checkin` | Record a subjective check-in |
| GET | `/v1/data-quality` | Data-quality findings |
| GET | `/v1/sync/status` | Recent sync runs |
| GET | `/v1/provenance/{token}` | Resolve a provenance token |
| GET | `/v1/source-policies/effective` | Effective source policy |
| GET | `/v1/source-policies/decisions` | A day's selection decision |
| GET | `/v1/providers` | Linkable providers — **configured ones only** |
| GET | `/v1/connections` | The caller's connections and sync status |
| GET | `/v1/connections/{provider}/authorize` | Begin a connector link |
| GET | `/v1/connections/{provider}/callback` | Complete a connector link |
| POST | `/v1/connections/{connection_id}/sync` | Queue a manual sync (requires `Idempotency-Key` header) |
| DELETE | `/v1/connections/{connection_id}` | Disconnect; drops tokens, **preserves history** |
| POST | `/v1/confirmations` | Issue a one-time confirmation for a mutating tool call |
| POST | `/v1/privacy/delete` | Start an erasure |
| GET | `/v1/privacy/delete/{deletion_request_id}` | Erasure status |

### Session **or** bearer service token

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/v1/tools` | List the tool catalog |
| POST | `/v1/tools/{tool_name}` | Invoke a tool |

See [tools-api.md](tools-api.md).

---

## Response headers on every response

Applied by middleware to all responses, including errors:

| Header | Value |
|--------|-------|
| `Content-Security-Policy` | `default-src 'none'; frame-ancestors 'none'` |
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `Referrer-Policy` | `no-referrer` |
| `Cross-Origin-Opener-Policy` | `same-origin` |
| `Cross-Origin-Resource-Policy` | `same-origin` |
| `X-Request-ID` | The request's correlation id |

`X-Request-ID` is echoed from the request when the inbound value is a safe
token (`[A-Za-z0-9._-]{1,128}`), otherwise a fresh UUID is minted. Have your
load balancer set it, and the same id appears in every log line for that
request.

This is a JSON-only API — it never returns HTML — which is why the CSP can deny
every resource type.

---

## Error conventions

Errors are JSON with a stable `code`, and are deliberately uninformative about
which internal check failed:

| Status | Meaning here |
|--------|--------------|
| `401` | Unauthenticated. One generic body for every cause |
| `403` | Authenticated but refused: CSRF missing/invalid, confirmation required or invalid, or a service token attempting a tool its scope does not admit |
| `404` | Not found **or** not configured **or** another tenant's — indistinguishable on purpose |
| `409` | Exists but not in a usable state (e.g. a connection needing re-consent) |
| `422` | Invalid input |
| `503` | Transient provider failure, or not ready |

The `404`-for-unconfigured rule is why so much of this documentation is about
configuration: at runtime you cannot tell "misconfigured" from "does not
exist", by design.
