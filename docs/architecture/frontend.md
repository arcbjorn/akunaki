# Frontend

**Status:** Proposed

**Last reviewed:** 2026-07-13

Authoritative for **frontend** architecture (coverage matrix item 19). Target: **Next.js TypeScript** **mobile-first responsive PWA**. No UI code exists in this repository.

---

## Stack and architecture

| Topic | Proposal |
|-------|----------|
| Framework | **Next.js** + **TypeScript** |
| API client | **Generated OpenAPI client** from the FastAPI `/v1` OpenAPI document |
| Server / client boundary | Server components for shell/auth gates where appropriate; client components for interactive charts, live forms, SSE; no domain formulas in the browser |
| UI system | **Design tokens** + **custom primitives** (not a generic admin card-grid template) |
| Charts | **Interpretable** charts: axes, source labels, confidence/baseline context; avoid chartjunk |
| Themes | Dark and light via tokens |
| a11y | WCAG 2.2 AA target for core flows |

---

## Information architecture

Primary hierarchy on **Today** answers **How / Why / What** with supporting detail panels (not three disconnected micro-apps).

| Pillar | User question | Primary data |
|--------|---------------|--------------|
| **How am I** | What is my status today? | `GET /v1/today`: recovery **score** (0–100); sleep **summary** (duration/adherence/debt); strain **load summary**; activity **measurements**; training **label**/confidence—not unspecified sleep/strain/activity/readiness scores |
| **Why** | What drives that status? | signed factors, provenance URL, data-quality, baselines context |
| **What should I do** | What are the next actions? | exactly one primary recommendation + supporting items |

### Proposed routes

| Route | Purpose |
|-------|---------|
| `/` | Redirect to Today |
| `/today` | How / Why / What hierarchy for today |
| `/recovery` | Recovery-focused deep view |
| `/sleep` | Sleep-focused deep view |
| `/trends` | Trends and multi-metric exploration |
| `/metrics/[metric]` | Single metric detail |
| `/workouts` | Workout list |
| `/workouts/[id]` | Workout detail |
| `/workouts/[id]/swim` | Swim detail when applicable |
| `/data-quality` | Data-quality findings |
| `/connections` | Providers, health, reauth |
| `/settings` | Preferences, theme |
| `/settings/sources` | Source policy inspection / overrides when enabled |
| `/settings/privacy` | Export, disconnect, delete |
| `/settings/models` | Optional model provider connect, capabilities, default/per-task selection, disable |
| `/chat` | Optional agent (hidden or static unavailable when models/agent disabled) |

Deep links must work with models disabled; chat is additive. Complete **models-off** experience is first-class (CI and UX review).

---

## Product completeness without models

When model providers or agent-worker are off:

- Today, recovery, sleep, trends, metrics, workouts, swimming, data quality, connections, source settings, privacy/export/delete work fully.
- Copy comes from **deterministic** `label_key` / `title_key` catalogs—not LLM text.
- `/chat` is omitted or shows a static assistant-unavailable state without blocking navigation.
- No UI path requires a model SDK or model config in the web bundle.

See [../testing.md](../testing.md).

---

## Visual system

| Topic | Proposal |
|-------|----------|
| Tone | Calm, premium, non-clinical alarmism |
| Themes | Dark and light via design tokens |
| Color | Custom palette (not stock purple-gradient AI cliché); semantic tokens for recovery score bands, factors, warnings |
| Typography | Distinctive but readable; tabular nums for recovery scores and measurement summaries |
| Motion | Subtle; respect **`prefers-reduced-motion`** |
| Layout | Mobile-first single column; progressive disclosure on Why; **avoid generic admin card grids** |
| Charts | Interpretable first; confidence, source, and baseline context visible |

Score and summary presentation (v0.1.0):

- **Recovery only** as large numeric 0–100 score **or** explicit `Insufficient data` (null score)—never a fabricated midpoint
- Sleep, strain/load, and activity as **deterministic summaries/measurements**, not as unspecified daily scores
- Readiness / training as the **deterministic label** (hard/moderate/light/rest/insufficient) with ruleset and confidence—not an unspecified readiness score
- Later accepted formula versions may add non-recovery scores **additively**; UI must not imply those scores ship before then
- Confidence as text + meter, not hidden
- Freshness timestamp always visible on How am I

---

## Accessibility

- WCAG 2.2 AA target for core flows
- Keyboard operable Today hierarchy and chart alternatives (data tables)
- Screen reader labels for recovery score (when present), confidence, factor signs, training label
- Color never sole encoding of sign (icons/text for + / −)
- Focus management on route changes
- Reduced motion respected for all non-essential animation

---

## Source and confidence disclosure

Today / Why must surface:

- Authoritative provider per factor
- When alternatives exist but were not selected
- Policy link to `/settings/sources`
- Confidence, freshness, formula/policy versions, and data gaps without blameful copy

Never imply medical certainty.

---

## Deterministic copy

| Source of words | Allowed |
|-----------------|---------|
| Engine `label_key` resolved via catalog | Yes |
| Static product microcopy | Yes |
| Model free text in chat only | Yes, labeled as assistant |
| Model free text replacing score labels on Today | **No** |

---

## Data handling in the browser

| Store | Health JSON | Notes |
|-------|-------------|-------|
| Memory / React state | Ephemeral OK | |
| `sessionStorage` | Avoid for health payloads by default | |
| `localStorage` / persistent IndexedDB | **No persistent caching of health JSON by default** | |
| PWA cache | App shell, static assets only | |
| Service worker | Authenticated **`/v1`** data: **NetworkOnly** | **Never** network-first durable health caching; never treat API health as cacheable CDN content |
| HTTP | Expect / send respect for `Cache-Control: private, no-store` on authenticated health | **CDN bypass** for authenticated API |

User may opt into limited offline **summary** later; not MVP default.

---

## Auth UX

- Login via OIDC; session cookie httpOnly
- CSRF token for cookie-authenticated mutations
- Connection OAuth in same browser session
- Reauth banners from `connections` / `GET /v1/sync/status`
- Model keys never enter the client bundle; only connection status and non-secret config

---

## Responsive PWA behaviors

- Installability via web app manifest
- Safe-area insets for notched devices
- Touch targets ≥ 44px on primary actions
- Offline shell with "reconnect to refresh health" empty state—not stale scores presented as current
- Mobile-first; complete models-off experience on small screens

---

## State management (proposed)

- Server state via generated client + request-level memory cache only—not persistent health dumps
- **`If-Match` / ETag headers** on preferences and similar mutable resources (not ETag in JSON bodies)
- Optimistic UI only for non-health preference toggles
- SSE for agent: authorize on connect; honor `Last-Event-ID` replay; no durable health event cache in SW

---

## Related

- [api-tools-and-agent.md](api-tools-and-agent.md)
- [../product-principles.md](../product-principles.md)
- [security.md](security.md)
