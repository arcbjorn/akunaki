# Bootstrap order

**Status:** Describes shipped code (`backend/src/akunaki/application/login.py`, `backend/scripts/service_token.py`)

**Last reviewed:** 2026-08-15

There is exactly one path from an empty database to a working deployment, and
it cannot be reordered. This page states it as a prerequisite chain because
skipping a step produces a failure several steps later that does not name the
real cause.

---

## The chain

```
1. alembic upgrade head
2. Configure OIDC and start the API
3. A human completes an OIDC login through a browser  ← creates the only user row
4. Mint a service token with scripts/service_token.py
5. Link connectors (session-authenticated, through the browser)
```

Steps 3 and 4 cannot be swapped, and step 3 has no non-browser substitute.

---

## Why OIDC is a hard prerequisite, not a convenience

**User rows are created in exactly one place in the codebase.**
`LoginService.complete` (`application/login.py`) calls
`UserRepository.upsert_from_identity`, which provisions a `Tenant` and its sole
`User` together on first login. Nothing else in `src/` or `scripts/` constructs
a `User`. There is:

- no seed command,
- no fixture or bootstrap script,
- no admin user baked into a migration,
- no HTTP route that creates a user.

**And the token minting script requires a user to exist.**
`scripts/service_token.py` binds each token to a user. With no `--user-id`, it
calls `_sole_user_id()`, which selects users outside the reserved system tenant
and raises when the count is not exactly one:

```
expected exactly one user, found 0; pass --user-id explicitly
```

Passing `--user-id` explicitly does not rescue you on an empty database — the
token would reference a user row that does not exist. The message describes the
mechanism, not the fix. **The fix is always: complete an OIDC login first.**

Therefore: if you cannot complete an OIDC login, you cannot mint an agent token,
and the `/v1/tools` surface is unreachable by any non-browser caller. Budget for
the IdP setup before the agent integration, not after.

---

## Step 3 in detail: the first login

The two legs are described in
[http-surface.md](http-surface.md#login-is-a-two-leg-json-flow-not-a-redirect).
What matters for bootstrap:

1. `GET /auth/login` returns `{"authorize_url": "..."}` — **JSON, status 200**,
   not a redirect. The client navigates to that URL itself.
2. The IdP sends the browser back to `AKUNAKI_OIDC_REDIRECT_URI`, which must be
   the deployment's `/auth/callback` with `state` and `code` query parameters.
3. `GET /auth/callback` verifies the token, provisions the user and tenant, sets
   the session cookie, and returns the CSRF secret in the body.

Preconditions for this to succeed, all of which fail at runtime rather than at
boot:

| Precondition | Failure if wrong |
|--------------|------------------|
| `AKUNAKI_OIDC_ISSUER` non-empty | `/auth/login` is a **404** — the router is not mounted |
| `AKUNAKI_OIDC_REDIRECT_URI` exactly matches the URI registered at the IdP | The IdP refuses the authorize request, or the callback's token exchange fails |
| The registered redirect URI path is `/auth/callback` | Same — and this is the easiest to get wrong, see [http-surface.md](http-surface.md#path-prefixes) |
| `AKUNAKI_SECRET_KEKS` configured with a valid 32-byte key | The sealed PKCE verifier cannot be opened; the callback returns **401** |
| Schema at migration head | Writes fail; `/readyz` was already 503 |

The callback returns a single generic `401 {"code": "unauthenticated"}` for
every rejection reason — state, nonce, token, and verifier failures are
deliberately indistinguishable to the caller. Which check failed is server-side
information only, so **read the API logs** when diagnosing a failed first login.

---

## Identity is `(issuer, subject)` — keep it stable

A user is looked up by the OIDC issuer plus subject claim. The email is
refreshed on each login but is never treated as identity and never merges
accounts.

The consequence for a single-user deployment: **changing your IdP, or changing
the subject a user is issued, creates a second user and a second tenant.** The
old tenant's data stays attached to the old user and is not visible to the new
one. It also breaks `_sole_user_id()` — the script then reports
`expected exactly one user, found 2` and you must pass `--user-id` explicitly to
mint any further token.

Pick the issuer and subject mapping before the first login.

---

## Step 4: minting the first service token

Run inside a container built from the deployed image, with the same
`AKUNAKI_DATABASE_URL` and on a host that can write the database file:

```
python /app/scripts/service_token.py issue --name my-agent
```

The raw token is printed **once** and is not stored — only its hash lands in
the database. Capture it into your secret store immediately; a lost token can
only be replaced by minting a new one and revoking the old.

The token is `read`-scoped unless you pass `--scope read_sync`, which
additionally lets it trigger `connections.sync`. The scope is fixed at mint
time, so start narrow — widening means minting a replacement.

Full usage, scope semantics, and revocation are in
[tools-api.md](tools-api.md#minting-listing-and-revoking-tokens).

---

## Step 5: linking connectors

Connector linking is **session-authenticated** — it runs through the same
browser session the login established, at
`GET /v1/connections/{provider}/authorize`. A service token cannot link a
connector at any scope: linking requires a browser to walk the provider's
consent screen, and the callback is session-authenticated.

Completing a link queues the connection's history backfill immediately, one job
per stream the provider serves, reaching back `AKUNAKI_LOOKBACK_DAYS` (default
30) — see [configuration.md](configuration.md#history-window). Data should
begin appearing within a worker cycle rather than at the next reconcile sweep.

Per-provider portal setup, redirect URIs, and scopes are in
[connectors.md](connectors.md).
