# References

**Status:** Proposed

**Last reviewed:** 2026-07-13

Primary official sources only. These links support the proposed architecture; they do not imply integration code exists.

Relative links point at this documentation set. External links are vendor or standards documentation.

---

## Internal documentation

| Document | Path |
|----------|------|
| Index | [README.md](README.md) |
| Product principles | [product-principles.md](product-principles.md) |
| Glossary | [glossary.md](glossary.md) |
| Architecture overview | [architecture/overview.md](architecture/overview.md) |
| Repository and services | [architecture/repository-and-services.md](architecture/repository-and-services.md) |
| Data model | [architecture/data-model.md](architecture/data-model.md) |
| Ingestion and sync | [architecture/ingestion-and-sync.md](architecture/ingestion-and-sync.md) |
| Health engine | [architecture/health-engine.md](architecture/health-engine.md) |
| API, tools, and agent | [architecture/api-tools-and-agent.md](architecture/api-tools-and-agent.md) |
| Frontend | [architecture/frontend.md](architecture/frontend.md) |
| Security | [architecture/security.md](architecture/security.md) |
| Operations | [architecture/operations.md](architecture/operations.md) |
| Testing | [testing.md](testing.md) |
| Roadmap | [roadmap.md](roadmap.md) |
| ADR index | [adr/README.md](adr/README.md) |

---

## Oura

| Topic | URL |
|-------|-----|
| Oura API (Cloud API V2) documentation home | https://cloud.ouraring.com/v2/docs |
| Oura API authentication | https://cloud.ouraring.com/docs/authentication |
| Oura webhooks | https://cloud.ouraring.com/docs/webhooks |

---

## Google Health API (primary Fitbit-origin / daytime cloud path)

MVP connector id `google_health`. Google Health API v4 is the cloud successor to the legacy Fitbit Web API. Prefer these official docs for all new design and implementation.

| Topic | URL |
|-------|-----|
| About Google Health API | https://developers.google.com/health/about |
| Setup | https://developers.google.com/health/setup |
| Data types | https://developers.google.com/health/data-types |
| Endpoints | https://developers.google.com/health/endpoints |
| Webhooks | https://developers.google.com/health/webhooks |
| Rate limits | https://developers.google.com/health/rate-limits |
| Release notes | https://developers.google.com/health/release-notes |
| v4 dataPoints.reconcile (REST) | https://developers.google.com/health/reference/rest/v4/users.dataTypes.dataPoints/reconcile |

---

## Legacy Fitbit Web API (archival / migration-only)

**Archival and migration reference only.** The legacy Fitbit Web API is **not** an MVP connector and **stops syncing in September 2026**. Do not design new integration against it. Current Fitbit-origin path is **Google Health API** above.

| Topic | URL |
|-------|-----|
| Fitbit Web API | https://dev.fitbit.com/build/reference/web-api/ |
| Fitbit OAuth 2.0 | https://dev.fitbit.com/build/reference/web-api/developer-guide/authorization/ |
| Fitbit intraday | https://dev.fitbit.com/build/reference/web-api/intraday/ |
| Fitbit sleep API | https://dev.fitbit.com/build/reference/web-api/sleep/ |
| Fitbit heart rate | https://dev.fitbit.com/build/reference/web-api/heartrate-timeseries/ |

---

## Android Health Connect (future companion; not MVP server connector)

| Topic | URL |
|-------|-----|
| Health Connect overview | https://developer.android.com/health-and-fitness/guides/health-connect |
| Health Connect data and data types | https://developer.android.com/health-and-fitness/guides/health-connect/develop/data-types |
| Health Connect sync considerations | https://developer.android.com/health-and-fitness/guides/health-connect/develop/sync-data |

Distinct from Apple Health/HealthKit. On-device only; future Android companion bridge—not an MVP server connector.

---

## Apple Health / HealthKit (future native iOS bridge; not MVP server connector)

| Topic | URL |
|-------|-----|
| HealthKit framework | https://developer.apple.com/documentation/healthkit |
| HealthKit data types | https://developer.apple.com/documentation/healthkit/data-types |
| Authorizing access to health data | https://developer.apple.com/documentation/healthkit/authorizing-access-to-health-data |
| Reading data from HealthKit | https://developer.apple.com/documentation/healthkit/reading-data-from-healthkit |

Device-local, fine-grained user-authorized. Architecture requires a future **native iOS bridge** that syncs typed, provenance-preserving records to the backend. **Not** a server connector. **No** native mobile app in MVP. Distinct from Android Health Connect. See [architecture/overview.md](architecture/overview.md), [architecture/ingestion-and-sync.md](architecture/ingestion-and-sync.md).

---

## Google Fit deprecation

| Topic | URL |
|-------|-----|
| Google Fit REST API deprecation | https://developers.google.com/fit/rest |
| Migrate from Google Fit to Health Connect | https://developer.android.com/health-and-fitness/guides/health-connect/migrate/migration-guide |

---

## Polar AccessLink

| Topic | URL |
|-------|-----|
| Polar AccessLink documentation | https://www.polar.com/accesslink-api/ |
| Polar AccessLink API v4 (dynamic / data base URL path) | https://www.polar.com/polar-api-v4 |
| Polar AccessLink GitHub (official examples/docs entry) | https://github.com/polarofficial/accesslink-example-python |
| Polar interactive API / swagger entry (AccessLink) | https://www.polar.com/accesslink-api/#/ |
| Polar Verity Sense product information | https://www.polar.com/en/sensors/verity-sense |

Note: Confirm v3 versus v4 endpoint coverage and Verity Sense swimming field availability during phase-zero spikes ([roadmap.md](roadmap.md)). Prefer the live OpenAPI/docs surface Polar publishes for the registered application tier.

---

## Turso / libSQL / SQLAlchemy

| Topic | URL |
|-------|-----|
| Turso documentation | https://docs.turso.tech/ |
| Turso Python SDK quickstart | https://docs.turso.tech/sdk/python/quickstart |
| Turso Python SDK reference | https://docs.turso.tech/sdk/python/reference |
| libSQL | https://docs.turso.tech/libsql |
| Turso vector / AI (later-ready; not MVP schema) | https://docs.turso.tech/features/ai-and-embeddings |
| Turso vector-search guide (later-ready; not MVP schema) | https://docs.turso.tech/guides/vector-search |
| SQLAlchemy 2.0 documentation | https://docs.sqlalchemy.org/en/20/ |
| SQLAlchemy 2.0 ORM | https://docs.sqlalchemy.org/en/20/orm/ |
| Alembic documentation | https://alembic.sqlalchemy.org/en/latest/ |

**Turso is the selected production operational store.** Phase zero validates the exact Python/SQLAlchemy/Alembic, concurrency, migration, encryption, volume, and later vector path; only a proven blocker reopens [adr/0003-libsql-operational-store.md](adr/0003-libsql-operational-store.md). Optional `F32_BLOB`/vector index is an implementation option for future embeddings, not an MVP schema requirement.

---

## MCP (Model Context Protocol)

| Topic | URL |
|-------|-----|
| MCP specification | https://modelcontextprotocol.io/specification |
| MCP transports (2025-11-25) | https://modelcontextprotocol.io/specification/2025-11-25/basic/transports |
| MCP authorization (2025-11-25) | https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization |

Pin exact protocol versions in implementation; links above are the official docs roots/versioned transports to validate during phase four.

---

## Web and security standards

| Topic | URL |
|-------|-----|
| OAuth 2.1 (draft/spec track) | https://datatracker.ietf.org/doc/html/draft-ietf-oauth-v2-1-13 |
| OAuth 2.0 PKCE (RFC 7636) | https://datatracker.ietf.org/doc/html/rfc7636 |
| OpenID Connect Core | https://openid.net/specs/openid-connect-core-1_0.html |
| W3C WCAG 2.2 | https://www.w3.org/TR/WCAG22/ |
| Problem Details for HTTP APIs (RFC 9457) | https://datatracker.ietf.org/doc/html/rfc9457 |

---

## Framework docs (implementation targets)

| Topic | URL |
|-------|-----|
| FastAPI | https://fastapi.tiangolo.com/ |
| Next.js | https://nextjs.org/docs |
| Pydantic | https://docs.pydantic.dev/ |
| OpenTelemetry | https://opentelemetry.io/docs/ |

---

## How references should be used

1. Spike notes must cite the primary doc section exercised.
2. Architecture pages prefer relative links for internal decisions; this page owns external URLs.
3. If a vendor moves docs, update this file in the same change set as architecture claims.
