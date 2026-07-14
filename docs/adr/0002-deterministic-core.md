# ADR 0002: Deterministic core

**Status:** Proposed

**Last reviewed:** 2026-07-13

## Context

Health scores and recommendations strongly influence user trust. LLM-only scoring is non-reproducible, hard to audit, and unsafe for wellness products that must avoid invented medical certainty. Users and operators need to answer *why* a day scored as it did, including which inputs, baselines, formula version, and coverage were used.

## Decision

- All scores, baselines, anomalies, rule recommendations, and training labels are computed by **pure staged functions** in a domain core.
- Inputs are current `source_selections` → `fact_records` (via nullable real `selected_fact_record_id`), formula versions, and source policy versions/generations. Candidates are never averaged or auto-fallback.
- Every derived artifact is produced under a reproducible **`derivation_run`** with typed **`derivation_inputs`**, `formula_version`, source-policy pin, `dependency_hash`, confidence, freshness relative to **`as_of_at`**, and supersession.
- `daily_health_scores` requires **`score_code`**. **`general_recovery_v0.1.0`** is the executable recovery formula and is **explicitly unvalidated**. Other score codes **cannot ship** until formula specs and golden fixtures are accepted.
- Recovery v0.1.0 uses **exact published weights** (sum 1.00) and gates (sleep + HRV or RHR, `available_weight >= 0.60`); directed z mappings; `robust_scale = 1.4826*MAD` then IQR/1.349 then metric floor; weighted means only over present components with **disclosed coverage**; baseline-insufficient components omitted; low confidence is **partial**, not a fake full score.
- Baselines use prior **42** calendar days, min **14** / mature **28**, median/`robust_scale`, EWMA α=**0.25**, **no imputation**, with stratification resets on method, HRV statistic/window, source-policy generation, and material device change.
- Canonical load is **always internal** from Polar HR zones; ACWR requires strict **7/7** and **28/28** known days (unknown ≠ 0); descriptive only—**no injury prediction**.
- Models are **optional**, consume structured summaries, and **cannot invent or override** scores.
- REST, jobs, agent tools, and future MCP call the same application services; none embed formulas.
- Insufficient critical data returns `insufficient`, never a fabricated neutral score; missing data is not a rest recommendation.
- Formula changes require a new `formula_version` and tests ([../testing.md](../testing.md)).

Executable recovery formulas are **explicitly unvalidated** engineering proposals ([../architecture/health-engine.md](../architecture/health-engine.md)).

## Consequences

### Positive

- Bit-stable golden tests and refactor safety
- Clear provenance, coverage disclosure, and UI honesty
- Product works with models disabled (CI-enforced)
- Safer marketing boundary: wellness guidance, not diagnosis
- Implementable recovery v0 math with explicit gates

### Negative

- Formula design requires deliberate product work; cannot "just prompt"
- Recompute jobs needed when formulas or policies change
- Some nuanced narrative lives only in optional agent layer
- Strict gates increase `insufficient` rates when connectors are sparse (by design)

### Neutral

- Rule recommendations use catalog keys rather than free text on core pages
- One global primary recommendation plus supporting detail

## Reversal conditions

Revisit only with explicit product and safety review if:

1. A regulated clinical product path requires certified algorithms with different governance (still deterministic, but different process)—not a switch to LLM scoring.
2. Pure functions become unmaintainably large; then split packages **without** moving scoring into models.

**Non-reversal:** Moving primary scoring into non-deterministic model output is out of policy unless a future ADR explicitly supersedes this one with documented safety rationale.

## Related

- [../architecture/health-engine.md](../architecture/health-engine.md)
- [../architecture/data-model.md](../architecture/data-model.md)
- [../architecture/api-tools-and-agent.md](../architecture/api-tools-and-agent.md)
- [../product-principles.md](../product-principles.md)
