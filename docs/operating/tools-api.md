# The `/v1/tools` registry

**Status:** Describes shipped code (`backend/src/akunaki/api/routes/tools.py`, `backend/scripts/service_token.py`)

**Last reviewed:** 2026-08-15

`/v1/tools` is the surface designed for **non-browser callers** — a personal
agent, an MCP adapter, a script. It exposes the same typed registry a browser
session uses, so a tool can no more cross tenants than a direct route can.

It is the **only** surface that accepts a bearer token. Everything else under
`/v1` is session-only.

---

## The contract

### List the catalog

```
GET /v1/tools
Authorization: Bearer <service token>
```

```json
{
  "tools": [
    {
      "name": "health.get_today",
      "version": "v0.1.0",
      "scopes": ["read:health"],
      "sensitivity": "health_read",
      "side_effect": "none",
      "model_exposure": true,
      "requires_confirmation": false
    }
  ]
}
```

`side_effect` and `requires_confirmation` are the two fields a caller should
branch on — see below.

### Invoke a tool

```
POST /v1/tools/{tool_name}
Authorization: Bearer <service token>
Content-Type: application/json

{"input": {"day": "2026-08-15"}}
```

The arguments go **inside an `input` object**, not at the top level. An empty
`{"input": {}}` is valid for tools that take no arguments; the field defaults to
an empty object if omitted entirely. The response body is the tool's typed
output model, returned directly.

Three more fields exist on the request body, all for mutating tools:
`confirmation_token`, `idempotency_key`, and `run_id`.

### Response codes

| Status | Meaning |
|--------|---------|
| `200` | The tool ran; body is its output |
| `401` | Missing, malformed, unknown, expired, or revoked token — one generic body for all |
| `403` | A service token attempted a tool its scope does not admit, or a confirmation was required and absent/invalid |
| `404` | No such tool — **or** the tool ran and its subject does not exist for this tenant |
| `422` | Invalid input for that tool |

The `404` conflation is deliberate: unknown and cross-tenant must be
indistinguishable, so an id cannot be probed through a tool any more than
through a route.

---

## What a service token may reach

A token's **scope** bounds which tools it can invoke, and the boundary follows
each tool's declared `ConfirmationPolicy` rather than a blanket "does it have a
side effect" test. Those are not the same question. `connections.sync` enqueues
the same job a webhook already queues — idempotent, deduplicated, and something
the user can trigger themselves from their own session. `privacy.delete`
destroys data inline and cannot be undone. Treating them identically meant the
benign one was unreachable so that the irreversible one would be.

### The two scopes

| Scope | Admits |
|-------|--------|
| `read` (default) | Tools whose policy is `never` — every read tool |
| `read_sync` | The same, plus tools whose policy is `if_agent` — today `connections.sync` |

**No scope admits an `always` tool.** `privacy.delete` is refused for every
service token however it was minted. A confirmation is what guards a
destructive action, and a bearer credential that could carry one would turn a
stolen token into an erasure.

The check sits in the invoke route, **before the confirmation machinery**, so a
token that may not reach a tool cannot probe whether a confirmation would have
been accepted or which tokens are live. Concretely:

| Tool | `ConfirmationPolicy` | `read` token | `read_sync` token |
|------|----------------------|--------------|-------------------|
| `health.get_today` | `never` | Works | Works |
| `health.get_recovery` | `never` | Works | Works |
| `health.get_sleep` | `never` | Works | Works |
| `health.find_anomalies` | `never` | Works | Works |
| `health.get_recent_workouts` | `never` | Works | Works |
| `health.get_workout` | `never` | Works | Works |
| `connections.list` | `never` | Works | Works |
| `connections.sync` | `if_agent` | **403** | Works |
| `privacy.delete` | `always` | **403** | **403** |

### The capability is opt-in at mint time

`read` is the default, and there is no way to widen a token that already
exists. A token minted before `read_sync` existed keeps exactly the authority it
was granted; to grant more, mint a new token and revoke the old one. That is
also why the scope is a stored column with a CHECK constraint — a row edited
out of band cannot invent a grant the code does not understand.

### Confirmations still apply

Scope decides which tools a token may reach. The tool's policy still decides
what a **particular call** needs. A `read_sync` token invoking
`connections.sync`:

- **without `run_id`** — runs. The token is itself the deliberate, operator-issued
  authorization, and the enqueue is idempotent and deduplicated.
- **with `run_id`** — needs a confirmation bound to that exact call, exactly as a
  session caller does. Confirmations are issued from a browser session
  (`POST /v1/confirmations`) and bind `(tenant, user, run_id, tool, args hash,
  idempotency key)`; because a service token acts for the same user, it can
  redeem one the user granted.

Every invocation — refused or successful — is written to the audit trail with
`origin: service_token`, so a reviewer can always tell which credential asked.
The trail records *that* a tool ran and how it ended, never the arguments.

**Consequence for integrations:** an agent with a `read` token can read health
data and list connections, and nothing else. An agent with a `read_sync` token
can additionally ask for fresh data. Neither can erase anything; that requires
a browser session and an explicit confirmation, and no configuration relaxes it.

### Triggering a sync: list first

`connections.sync` takes a `connection_id`, and `connections.list` is where you
get one — each entry carries the `connection_id` alongside its provider and
health. Resolve it from the listing rather than storing it; a re-linked
connector keeps its id, but the listing is the surface that stays correct.

```
POST /v1/tools/connections.list   {"input": {}}
  -> {"connections": [{"connection_id": "…", "provider": "polar", "status": "active", …}]}

POST /v1/tools/connections.sync   {"input": {"connection_id": "…"}}
  -> {"job_id": "…", "created": true}
```

`created: false` means an identical sync was already in flight and yours
deduplicated onto it — a normal outcome, not an error. The `connection_id` is
an opaque per-tenant uuid; every call re-checks tenant ownership, so a stale or
guessed one is a `404`, indistinguishable from a connection that does not exist.

---

## Mutations from a session (for completeness)

A cookie-authenticated caller can invoke mutating tools, subject to each tool's
declared `ConfirmationPolicy`:

| Policy | Meaning |
|--------|---------|
| `never` | Reads. The session's own authorization is the whole check |
| `if_agent` | Needs a confirmation only when the call carries a `run_id` (an agent run). `connections.sync` |
| `always` | Needs a confirmation from every caller, including a direct human one. `privacy.delete` |

A confirmation is obtained out-of-band from `POST /v1/confirmations`, naming the
tool, the exact arguments, and an idempotency key. The returned token is shown
once, expires in **5 minutes**, is single-use, and is bound to
`(tenant, user, run_id, tool, args hash, idempotency key)` — so it authorizes
that one call and nothing else. Requesting a confirmation for a tool that does
not need one is a `409`, deliberately, so the step does not become a reflex.

---

## Minting, listing, and revoking tokens

There is **no HTTP route for minting**. Creating a credential is an operator act
against the database, so a stolen session can never mint itself a durable
token. `scripts/` ships inside the container image for exactly this.

Run with the same `AKUNAKI_DATABASE_URL` as the deployment, on a host that can
write the database file:

### Mint

```
python /app/scripts/service_token.py issue --name my-agent
python /app/scripts/service_token.py issue --name my-agent --scope read_sync
python /app/scripts/service_token.py issue --name ci-probe --ttl-days 7
```

Output:

```
token_id:   <uuid>
tenant_id:  <uuid>
scope:      read
expires_at: never (revoke to retire)

The token below is shown once and not stored. Put it in the
caller's secret store now:

<the raw token>
```

The raw token is printed once and **never stored** — only its SHA-256 hash
lands in the database. There is no recovery path; a lost token is replaced by
minting a new one and revoking the old.

`--scope` is optional and defaults to `read`. Pass `--scope read_sync` only for
a consumer that genuinely needs to trigger syncs; see
[what a service token may reach](#what-a-service-token-may-reach). The scope is
fixed at mint time and cannot be changed afterwards.

`--ttl-days` is optional. Omitted, the token does not expire and is retired only
by revocation — appropriate for a personal agent's long-lived credential, but it
means revocation is your only expiry.

`--user-id` is optional and defaults to the sole human user. **This requires
that a user exists**, which requires a completed OIDC login — see
[bootstrap.md](bootstrap.md). On an empty database it exits with:

```
expected exactly one user, found 0; pass --user-id explicitly
```

### List

```
python /app/scripts/service_token.py list
```

Prints `token_id  name  scope  active|revoked` per token. It never shows secrets
— they are not stored.

### Revoke

```
python /app/scripts/service_token.py revoke --id <token-id>
```

Exits `0` on success, `1` with `no live token with id ...` if the id is unknown
or already revoked. Revocation is immediate: the next request presenting that
token gets a `401`.

---

## Operational guidance

- **One token per consumer.** Revocation is per-token, so a shared token cannot
  be rotated for one caller without breaking the others.
- **Mint the narrowest scope that works.** `read` is the default for a reason.
  A consumer that only reads a day view should not hold a credential that can
  cause vendor calls, and you cannot narrow a token later — only revoke it.
- **Rotate by overlap.** Mint the new token, deploy it to the consumer, confirm
  traffic, then revoke the old one. There is no built-in grace period.
- **Bearer requests skip CSRF**, deliberately — CSRF defends ambient cookie
  authority, which a header the caller must attach does not have. Do not add a
  CSRF header to a bearer call; it is ignored.
- **Never send both credentials.** An `Authorization` header commits the request
  to the bearer path. A request with both a bad bearer token and a good cookie
  is rejected, not downgraded to the cookie.
- **`model_exposure` is declared, not enforced.** The field states whether a
  model may invoke a tool, but no agent caller exists in this codebase to
  enforce it against. The tool's `ConfirmationPolicy` and the token's scope are
  what actually guard a mutation today. If you build an agent adapter, it must
  consult `model_exposure` itself — note that `privacy.delete` sets it to
  `false`, and a bearer token cannot reach it under any scope regardless.
