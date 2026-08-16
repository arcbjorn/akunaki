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

Three more fields exist on the request body, all for mutating tools and all
unusable with a service token: `confirmation_token`, `idempotency_key`, and
`run_id`.

### Response codes

| Status | Meaning |
|--------|---------|
| `200` | The tool ran; body is its output |
| `401` | Missing, malformed, unknown, expired, or revoked token — one generic body for all |
| `403` | A service token attempted a tool with a side effect, or a confirmation was required and absent/invalid |
| `404` | No such tool — **or** the tool ran and its subject does not exist for this tenant |
| `422` | Invalid input for that tool |

The `404` conflation is deliberate: unknown and cross-tenant must be
indistinguishable, so an id cannot be probed through a tool any more than
through a route.

---

## A service token is read-scoped by construction

This is the security property that makes handing a token to an agent
reasonable, and it is worth stating precisely.

`ServiceTokenScope` has exactly one member: `READ`. There is no write scope to
grant, no flag to widen a token, and no code path that issues anything else.

The enforcement is in the invoke route, and it is **positioned before the
confirmation machinery**:

```python
if isinstance(caller, AuthenticatedServiceToken) and tool.side_effect is not SideEffect.NONE:
    ... audit "refused" ...
    raise HTTPException(status_code=403, ...)
```

A bearer caller invoking any tool with a side effect is refused with `403`
*before* any confirmation token is examined. So a stolen service token cannot
even probe the confirmation system — it cannot discover whether a confirmation
would have been accepted, or which tokens are live.

Concretely, with a service token:

| Tool | `side_effect` | Bearer caller |
|------|---------------|---------------|
| `health.get_today` | `none` | Works |
| `health.get_recovery` | `none` | Works |
| `health.get_sleep` | `none` | Works |
| `health.find_anomalies` | `none` | Works |
| `health.get_recent_workouts` | `none` | Works |
| `health.get_workout` | `none` | Works |
| `connections.list` | `none` | Works |
| `connections.sync` | `enqueue_job` | **403** |
| `privacy.delete` | `destroy_data` | **403** |

Every refusal is written to the audit trail with `origin: service_token`,
because a refused mutation is what a confused-deputy attempt looks like from
the outside. The trail records *that* a tool ran and how it ended — never the
arguments.

**Consequence for integrations:** an agent given a service token can read
health data and list connections, and nothing else. To trigger a sync or an
erasure, a human must act through a browser session — with CSRF, and (for
`privacy.delete`) an explicit confirmation. Design the integration around that;
there is no configuration that relaxes it.

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
  enforce it against. `confirmation` and the read-only scope are what actually
  guard a mutation today. If you build an agent adapter, it must consult
  `model_exposure` itself.
