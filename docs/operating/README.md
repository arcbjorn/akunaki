# Operating akunaki

**Status:** Describes shipped code in `backend/`

**Last reviewed:** 2026-08-15

Operator documentation for running the akunaki backend on your own
infrastructure. Everything here was verified against the source at the date
above; where a statement is load-bearing, it names the file that enforces it.

This section describes the **contract** — the paths, variables, container
commands, and failure modes the code implements. It deliberately describes no
particular deployment: no hostnames, no cluster topology, no orchestration
tooling. Your deployment describes itself.

For the proposed target architecture (which is broader than what is built),
see [../architecture/operations.md](../architecture/operations.md). Where the
two disagree, this section is what the code does.

---

## Read in this order

| # | Document | Answers |
|---|----------|---------|
| 1 | [deployment.md](deployment.md) | What processes exist, how the image is run, why one replica |
| 2 | [configuration.md](configuration.md) | Every `AKUNAKI_` variable, its default, what empty does |
| 3 | [bootstrap.md](bootstrap.md) | The mandatory order: migrate → OIDC login → mint tokens |
| 4 | [http-surface.md](http-surface.md) | The full public path list, including the `/auth` exception |
| 5 | [connectors.md](connectors.md) | Per-provider OAuth and webhook setup |
| 6 | [tools-api.md](tools-api.md) | The `/v1/tools` contract for non-browser callers |
| 7 | [health-and-probes.md](health-and-probes.md) | `/healthz` vs `/readyz`, and probe wiring |

---

## The five things that surprise operators

Each is expanded in the linked page. They are collected here because each one
fails only at runtime, and only after something else looks like it worked.

1. **Login is not a redirect.** `GET /auth/login` returns a JSON body
   `{"authorize_url": "..."}` with status 200. There is no `302` anywhere in
   the login path. A client that follows redirects sees nothing happen and
   concludes auth is broken. See [http-surface.md](http-surface.md#login-is-a-two-leg-json-flow-not-a-redirect).

2. **The login routes are not under `/v1`.** They mount at `/auth/login` and
   `/auth/callback`. The OIDC redirect URI you register at your IdP must match
   exactly, and a wrong path is accepted by every config check and fails only
   when a human first tries to log in. See [http-surface.md](http-surface.md#path-prefixes).

3. **OIDC is a hard prerequisite for a service token.** User rows are created
   only by a completed OIDC login. Until one human has logged in, the token
   minting script exits with `expected exactly one user, found 0`. There is no
   bootstrap path, no seed command, and no admin user. See [bootstrap.md](bootstrap.md).

4. **An unconfigured provider is an indistinguishable `404`.** Partial
   credentials do not produce a helpful error — a provider missing any one of
   client id, secret, or redirect URI behaves exactly like a provider that does
   not exist. See [connectors.md](connectors.md#partial-credentials-are-invisible).

5. **The database is a single-writer file.** One API replica, one worker
   replica, no rolling deploys. See [deployment.md](deployment.md#the-database-is-a-single-writer-file).
