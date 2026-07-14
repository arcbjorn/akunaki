# API, tools, and agent

**Status:** Proposed

**Last reviewed:** 2026-07-13

Authoritative for **model provider**, **tool registry**, **MCP**, and **product API** (coverage matrix items 9–12).

No HTTP server exists in this repository yet. Paths and payloads are proposed contracts.

---

## REST conventions

| Topic | Convention |
|-------|------------|
| Base | **`/v1`** (public product API base; not `/api/v1`) |
| Format | JSON UTF-8 |
| Time | UTC ISO-8601 instants; local dates as `YYYY-MM-DD` |
| Errors | **Problem Details** JSON: `type`, `title`, `status`, `detail`, `code`, `request_id` (RFC 7807-shaped) |
| Pagination | **Cursor**: `?cursor=&limit=`; response `{ items, next_cursor }` |
| Idempotency | **`Idempotency-Key`** header on mutating POSTs that create jobs or side effects |
| Concurrency | Response **`ETag` header** on mutable resources; client sends **`If-Match`**; mismatch → **412** (not body-embedded ETag) |
| Auth | Session cookie after OIDC code+PKCE; `Authorization: Bearer` reserved for future MCP/service |
| Versioning | URL version; additive fields OK; breaking changes require `/v2` |
| Caching | Authenticated health responses: `Cache-Control: private, no-store`; never durable CDN cache |

### Error semantics

| HTTP | `code` (examples) | Meaning |
|------|-------------------|---------|
| 400 | `bad_request` | Malformed request outside validation model |
| 401 | `unauthenticated` | Missing/invalid session |
| 403 | `forbidden` | Tenant/authz denial |
| 404 | `not_found` | Unknown resource (or cross-tenant as 404) |
| **409** | **`agent_disabled`** | Agent intentionally disabled for tenant/platform |
| 409 | `conflict` | Business state conflict (not If-Match) |
| **412** | **`precondition_failed`** | **`If-Match` failure** |
| **422** | `validation_error`, business codes | **Validation or business content** errors (schema, rule violations) |
| **424** | `failed_dependency` | **Dependency failed** when this status is used (e.g. required upstream connection state) |
| 429 | `rate_limited` | |
| **503** | `agent_unavailable`, `upstream_unavailable` | **Agent outage** or temporary dependency outage—not intentional disable |

**Do not** use `503` with `models_disabled` / `agent_disabled` for intentional disable; that is **409 `agent_disabled`**.

Non-agent product routes never depend on agent-worker availability.

---

## Endpoint catalog (proposed)

All paths are under **`/v1`**.

### Auth and session

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/v1/auth/login` | Start OIDC (state, nonce, PKCE) |
| GET | `/v1/auth/callback` | OIDC callback |
| POST | `/v1/auth/logout` | Revoke session |
| GET | `/v1/me` | User + tenant stub |

### Connections, source policy, preferences

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/v1/providers` | Capability matrix + connection status |
| POST | `/v1/connections/{provider}/oauth/start` | Start provider OAuth (state/PKCE where supported) |
| GET | `/v1/connections/{provider}/oauth/callback` | Finish link |
| GET | `/v1/connections` | List connections + health |
| POST | `/v1/connections/{id}/sync` | Enqueue manual sync (`Idempotency-Key`) |
| DELETE | `/v1/connections/{id}` | Disconnect + revoke tokens |
| GET | `/v1/source-policies/effective` | Inspectable source policy |
| GET/PUT | `/v1/source-policies/override` | Tenant overrides when enabled (`ETag` / `If-Match`) |
| GET/PUT | `/v1/preferences` | User preferences (`ETag` / `If-Match`) |

### Today, days, recovery, sleep, metrics, trends

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/v1/today` | How am I **today** (composite day view) |
| GET | `/v1/days/{date}` | Specific local health day (`YYYY-MM-DD`) |
| GET | `/v1/recovery` | Recovery-focused view (default: today; optional `?date=`) |
| GET | `/v1/sleep` | Sleep-focused view |
| GET | `/v1/metrics/{metric}` | Single metric series / detail |
| GET | `/v1/trends` | Multi-metric trends (cursor/window params) |

### Workouts and swimming

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/v1/workouts` | Workout list (cursor pagination) |
| GET | `/v1/workouts/{id}` | Workout detail |
| GET | `/v1/workouts/{id}/swim` | Swim detail (lengths/distances when present) |

### Anomalies, recommendations, data quality, sync

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/v1/anomalies` | Active/recent anomalies |
| GET | `/v1/recommendations` | Recommendations (default today; optional date) |
| GET | `/v1/data-quality` | Data-quality findings |
| GET | `/v1/sync/status` | Sync/connection freshness and run status |

### Nutrition

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/v1/nutrition/meals` | Create meal (`Idempotency-Key`) |
| GET | `/v1/nutrition/days/{date}` | Nutrition for a day |
| GET | `/v1/nutrition/trends` | Nutrition trends |
| GET | `/v1/nutrition/insights` | Descriptive insights (non-causal) |

### Conversations / agent (optional)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/v1/conversations` | Create conversation |
| POST | `/v1/conversations/{id}/messages` | User message; enqueues agent run |
| GET | `/v1/conversations/{id}/events` | **SSE** stream |
| POST | `/v1/conversations/{id}/confirmations/{cid}` | Confirm/deny tool mutation |
| POST | `/v1/conversations/{id}/cancel` | Cancel in-flight run when supported |

### Model provider connection and config (optional; API stores config only)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/v1/model-providers` | Connected providers + capabilities |
| POST | `/v1/model-providers/{provider}/connect` | Connect / store envelope-encrypted user key |
| DELETE | `/v1/model-providers/{provider}` | Disconnect / revoke stored key |
| GET/PUT | `/v1/model-config` | Default model, per-task selection, disable flags (`ETag`) |
| GET | `/v1/model-config/capabilities` | Capability matrix for connected providers |
| PUT | `/v1/model-config/default` | Set default model (no silent fallback elsewhere) |
| PUT | `/v1/model-config/tasks/{task}` | Per-task model selection or **disable** |

### Export, deletion, privacy

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/v1/exports` | Start export job (`Idempotency-Key`) |
| GET | `/v1/exports/{id}` | Status / time-limited download |
| POST | `/v1/privacy/delete` | Start deletion pipeline |
| GET | `/v1/privacy/delete/{id}` | Deletion status |

### Provenance (opaque)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/v1/provenance/{token}` | Lineage detail for an opaque token from day responses |

If the agent is **intentionally disabled**, agent routes return **409** `agent_disabled` (or UI hides entry). If agent-worker is down or model upstream fails, return **503** with appropriate code. **Non-agent product remains fully usable** either way.

---

## Representative `GET /v1/today` response

### Body rules (v0.1.0)

- **Only recovery** is a **0–100 score** (`score_code = recovery`) under `general_recovery_v0.1.0`. Non-recovery score codes **do not ship** until accepted formula versions exist ([health-engine.md](health-engine.md)).
- **Sleep** is a **deterministic summary**: duration, target adherence / debt / status—not an unspecified daily sleep score.
- **Strain** is a **deterministic load summary** (daily/acute/chronic load, ACWR when defined)—not an unspecified strain score.
- **Activity** is **measurements** (e.g. steps, active minutes)—not an unspecified activity score.
- **Readiness** is the **deterministic training label** (and confidence/ruleset)—not an unspecified daily readiness score. Prefer the `training_recommendation` object; do not invent a parallel numeric readiness score.
- Include signed recovery factors, confidence, freshness, formula/policy versions, **exactly one primary recommendation** plus supporting items, and an **opaque provenance URL**.
- **Later accepted formula versions** may add sleep/strain/activity/readiness **scores additively**; until then, clients and UI must not imply those scores exist.
- **Do not** expose table/raw ids or **ETag in body** (ETag, if any for other resources, is a response header only).

```json
{
  "local_date": "2026-07-13",
  "timezone": "America/Los_Angeles",
  "status": "ok",
  "recovery": {
    "score_code": "recovery",
    "status": "ok",
    "score": 72,
    "confidence": 0.81,
    "available_weight": 0.90
  },
  "sleep": {
    "status": "ok",
    "duration_min": 412,
    "target_min": 480,
    "adherence_pct": 85.8,
    "debt_14d_min": 132,
    "debt_known_days": 14
  },
  "strain": {
    "status": "ok",
    "load": {
      "daily_strain_load": 120.0,
      "acute_load": 640.0,
      "chronic_weekly_load": 610.0,
      "acwr": 1.05
    }
  },
  "activity": {
    "status": "ok",
    "steps": 8420,
    "active_minutes": 48
  },
  "training_recommendation": {
    "label": "moderate",
    "ruleset_version": "training_label_v0.1.0",
    "confidence": 0.75
  },
  "confidence": 0.81,
  "freshness_at": "2026-07-13T14:22:10Z",
  "formula_version": "general_recovery_v0.1.0",
  "source_policy_version": "spol_v3",
  "factors": [
    {
      "factor_code": "sleep_duration",
      "sign": -1,
      "magnitude": 0.22,
      "label_key": "factor.sleep_duration.short",
      "source_provider": "oura"
    },
    {
      "factor_code": "hrv",
      "sign": 1,
      "magnitude": 0.18,
      "label_key": "factor.hrv.above_baseline",
      "source_provider": "oura"
    }
  ],
  "primary_recommendation": {
    "rule_id": "sleep_extend_window",
    "priority": 100,
    "title_key": "rec.sleep_extend_window.title",
    "body_key": "rec.sleep_extend_window.body",
    "params": { "debt_min": 132, "known_days": 14, "adherence_pct": 85.8 }
  },
  "supporting_recommendations": [],
  "provenance_url": "/v1/provenance/opaque_tok_8f3a…",
  "data_gaps": []
}
```

Insufficient example: `"status": "insufficient"`, recovery `score` null, `"confidence": 0.0`, `"data_gaps": [{"code": "missing_authoritative_sleep", "provider": "oura"}]`, still **exactly zero or one** primary recommendation per ruleset (often none when insufficient), never fabricated midpoint scores. Sleep/strain/activity remain summaries or partial measurements with gaps disclosed—not invented scores.

---

## SSE conversation events

### Persistence and identity

- Every event persists with a **monotonic `event_id`** (stream position) and **`run_id`** for the agent run that produced it.
- Events are tenant-scoped rows; reconnect replays from durable storage, not process memory only.

### Connect / reconnect

- Authorize **tenant** (and conversation ownership) on **connect and reconnect**.
- Support **`Last-Event-ID`** (or equivalent) for replay of missed events after the last received `event_id`.
- Send **`Cache-Control: private, no-store`** (and equivalent no-store headers) on the SSE response.
- **Heartbeat** events (or comment frames) at a configured interval to keep intermediaries honest.
- Support **cancellation** of the active run and **terminal states** (`done`, terminal `error`, `cancelled`).

### Event types (proposed)

| `event` | Data purpose |
|---------|--------------|
| `heartbeat` | Keep-alive; may carry last `event_id` |
| `run.started` | `run_id` accepted |
| `message.delta` | Token or text chunk |
| `message.completed` | Final assistant message id |
| `tool.planned` | Tool name + args summary (no secrets) |
| `tool.confirmation_required` | Mutation gate + confirmation id |
| `tool.result` | Structured tool output (already authorized) |
| `run.cancelled` | Cancellation terminal |
| `error` | Recoverable or terminal error |
| `done` | Stream complete for the run |

API **stores and streams** events and **queues** agent runs. Generation happens in **agent-worker**. Missing agent-worker does not break non-agent APIs.

---

## Application services and tool registry

### Boundaries

```text
REST handlers ────────┐
Scheduled reports ────┤
Agent tool runner ────┼──► application services ──► domain / ports
MCP adapter ──────────┘         ▲
                                │
                     tool registry (typed facade
                     over selected services)
```

| Layer | Owns |
|-------|------|
| **Application services** | Use cases (connect, sync, recompute, day views, export, delete, nutrition, …) |
| **Tool registry** | Typed capability facade over **selected** services; phase **two**, **independent of AI** |
| **REST / report / agent / MCP** | Adapters that **reuse** the registry (or call services directly for OAuth/lifecycle when a tool wrapper is unnecessary) |

Forbidden: formulas in handlers, prompt-only scoring, per-channel divergent authz, agent-only business rules.

### Tool metadata (required)

| Field | Meaning |
|-------|---------|
| `name` | stable dotted name e.g. `health.get_today` |
| `input_model` | **Pydantic** input |
| `output_model` | **Pydantic** output |
| `version` | tool contract version |
| `scopes` | e.g. `read:health`, `write:nutrition` |
| `sensitivity` | `low`, `health_read`, `health_export`, `destructive` |
| `side_effect` | `none`, `enqueue_job`, `mutate_prefs`, `external_call` |
| `idempotency` | how duplicate calls are deduped |
| `timeout` | max execution bound |
| `audit` | audit action name / level |
| `model_exposure` | whether models may invoke; default deny for destructive |
| `requires_confirmation` | bool (true for mutations from agent) |

### Confirmation (mutating tools)

Confirmation is **one-time and expiring**, bound to:

`tenant_id` + `user_id` + `run_id` + `tool_name` + **canonical args hash** + **idempotency key**

Rules:

1. User must confirm out-of-band via API (not the model).
2. On execute, **reauthorize** the confirmation token against the same binding; arg substitution fails.
3. **Model cannot confirm.**
4. Replay of a consumed confirmation fails.

### Example tools (canonical registry)

Names below are the **brief-aligned canonical registry**. Lifecycle/export tools are retained separately after the domain set.

#### Health

| Name | Side effect | Confirmation |
|------|-------------|--------------|
| `health.get_today` | none | no |
| `health.get_day` | none | no |
| `health.get_recovery` | none | no |
| `health.get_recovery_factors` | none | no |
| `health.get_sleep` | none | no |
| `health.get_sleep_trend` | none | no |
| `health.get_metric_trend` | none | no |
| `health.compare_periods` | none | no |
| `health.get_recent_workouts` | none | no |
| `health.get_workout` | none | no |
| `health.get_swim` | none | no |
| `health.get_training_load` | none | no |
| `health.find_anomalies` | none | no |
| `health.get_data_freshness` | none | no |

#### Nutrition

| Name | Side effect | Confirmation | Notes |
|------|-------------|--------------|-------|
| `nutrition.log_meal` | mutate | yes if agent | |
| `nutrition.get_day` | none | no | |
| `nutrition.get_macros` | none | no | |
| `nutrition.get_nutrient_trends` | none | no | |
| `nutrition.compare_food_and_sleep` | none | no | **Descriptive / non-causal**; future-phase where not yet implemented |
| `nutrition.compare_food_and_recovery` | none | no | **Descriptive / non-causal**; future-phase where not yet implemented |

#### Body

| Name | Side effect | Confirmation |
|------|-------------|--------------|
| `body.get_weight_trend` | none | no |
| `body.get_composition_trend` | none | no |

#### Journal

| Name | Side effect | Confirmation | Notes |
|------|-------------|--------------|-------|
| `journal.log_symptom` | mutate | yes if agent | |
| `journal.log_energy` | mutate | yes if agent | |
| `journal.log_mood` | mutate | yes if agent | |
| `journal.get_correlations` | none | no | **Descriptive / non-causal** associations only; future-phase where not yet implemented |

#### Lifecycle / export (retained separately)

| Name | Side effect | Confirmation |
|------|-------------|--------------|
| `connections.list` | none | no |
| `connections.sync` | enqueue_job | yes if agent |
| `exports.create` | enqueue_job | yes |
| `privacy.delete` | enqueue_job | yes always |

Tools call **application services** only; **must not** contain scoring formulas. Numeric scores only come from tools/context that read accepted formula outputs (v0.1.0: recovery only), never model invention.

---

## Model provider interface

### Design goals

- Support OpenAI, Anthropic, Gemini, xAI, OpenRouter, and **local endpoints**
- **Canonical** request/response objects; adapters map to vendor wire formats
- Fully optional; core install/boot and CI run with **no** model SDK
- Multiple connected providers; **default** and **per-task** model selection; **disable** per task or globally
- Switch models **without rewriting** canonical conversation history
- **No silent fallback** to another model when the selected model fails
- User keys **envelope-encrypted** at rest
- Consent and persisted **context manifest** are **provider / model / purpose / data-scope** specific
- Review provider **no-training / data-use** policy before enable
- Minimize egress; handle **prompt injection** and **local endpoint SSRF**
- Scores only from tools/context

### Canonical model objects

```text
ModelRequest
  purpose: explain | chat | …
  messages: list[ConversationMessage]
  tools: list[ToolDefinition]     # registry subset
  structured_context: HealthSummaryDTO  # scores/factors keys; not raw payloads
  max_tokens: int
  tenant_policy: { consents, redaction_level, allowed_data_scopes }
  model_ref: { provider_id, model_id }  # explicit; no silent fallback

ModelResponse
  message: ConversationMessage
  tool_calls: list[ToolCall]
  usage: Usage
  provider_id: str
  model_id: str
  finish_reason: str

ConversationMessage
  role: system | user | assistant | tool
  content_parts: list[…]
  tool_call_id: optional
  created_at: …

ToolDefinition
  name, description, input_schema, version, scopes, …

ToolCall
  id, name, arguments (canonical JSON), …

ToolResult
  tool_call_id, status, output (typed), error: optional ProviderError

Usage
  input_tokens, output_tokens, … provider-normalized

ModelCapability
  provider_id, model_id, supports_tools, supports_stream,
  context_window, modalities, …

ProviderError (typed)
  class: auth | rate_limit | timeout | content_policy | ssrf_blocked | …
  retryable: bool
  message_redacted: str
```

### Async provider port

```text
ModelProvider.port
  id: str
  async generate(request: ModelRequest) -> ModelResponse
  async stream(request: ModelRequest) -> AsyncIterator[ModelEvent]
```

Adapters **must not leak** vendor-specific types into application or domain layers.

### Safety

| Rule | Detail |
|------|--------|
| No score invention | Numeric scores only from tools/context with accepted formulas (v0.1.0: recovery only); validate outputs |
| No override | Reject tool calls that write scores |
| Egress consent | Purpose- and scope-specific consent + manifest |
| Minimization | Structured summaries; raw revisions never attached by default |
| Prompt injection | Tool results and user content treated as untrusted; confused-deputy checks on tool authz |
| Local endpoints | Outbound **allowlist**; SSRF protections (no link-local/metadata IPs) |
| Disable | Platform or tenant disable → **409 `agent_disabled`** on agent routes |
| Isolation | Agent-worker outage → **503**; core product unaffected |
| No silent fallback | Failed selected model surfaces typed error; does not switch models quietly |

### Optional vector retrieval (phase four / future only)

Vector search is **not** required for any MVP score, recommendation, dashboard, or export path. When implemented later:

- May use Turso native vector columns/indexes (`F32_BLOB` + vector index as an **implementation option**, not MVP schema)
- Parent **`retrieval_documents`** + child **`vector_embeddings`** with real FK; typed source links/CHECK—**no** generic `source_kind`/`source_id`
- Store embeddings only of approved derived summaries, user-authored journal/conversation content, curated knowledge
- **Never** default-embed canonical raw measurements
- Metadata: model/provider/version, dimension, source content hash, sensitivity, consent, created time, delete/rebuild lineage
- **Tenant predicate mandatory** before/with retrieval; spike filtered ANN; rebuild on embedding-version change; delete with source

See [data-model.md](data-model.md) and [ADR 0003](../adr/0003-libsql-operational-store.md).

---

## MCP (phase four)

MCP is an **optional adapter process**, not a second business layer.

| Topic | Proposal |
|-------|----------|
| Phase | Four; after product API and tools are stable |
| Process | Separately deployable from the same modular-monolith package |
| Default mode | **Read-only** tool subset first |
| Local transport | **stdio** |
| Remote transport | Authenticated **Streamable HTTP** |
| Auth | Align with MCP authorization specs; session or token bound to tenant |
| Origin validation | Required for HTTP transports |
| Protocol | **Pin** supported MCP protocol version(s); reject unknown |
| Implementation | Map MCP tools → same registry → same application services |

Write tools via MCP require explicit product decision and confirmation semantics equivalent to the agent path. MCP outage does not affect core product.

---

## Related

- [health-engine.md](health-engine.md)
- [frontend.md](frontend.md)
- [security.md](security.md)
- [repository-and-services.md](repository-and-services.md)
- [../adr/0001-modular-monolith.md](../adr/0001-modular-monolith.md)
- [../references.md](../references.md) (MCP specs)
