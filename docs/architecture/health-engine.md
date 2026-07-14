# Health engine

**Status:** Proposed

**Last reviewed:** 2026-07-13

Authoritative for **deterministic scoring** (coverage matrix item 8). Companion ADR: [../adr/0002-deterministic-core.md](../adr/0002-deterministic-core.md).

All formulas below for **`general_recovery_v0.1.0`** are **executable initial specifications** and are **explicitly unvalidated** against clinical or population outcomes. They exist so implementation and tests have a concrete target. Governance may replace any formula via versioned changes without silent reinterpretation of old scores.

**Ship rule:** non-recovery `score_code` values (`sleep`, `strain`, `activity`, `readiness`) **cannot ship** until their formula specs and golden fixtures are accepted. Do not treat unspecified weights as implementable.

---

## Pipeline (pure staged functions)

```text
source_selections (+ source_selection_candidates for Why only)
        ↓
  fact_records (+ typed details) via selected_fact_record_id
        ↓
  daily_health_features
        ↓
    baselines
        ↓
 daily_health_scores + factors   (per score_code with accepted formula)
        ↓
    anomalies
        ↓
 rule recommendations + training label
```

Each stage is recorded as a **`derivation_run`** with **`derivation_inputs`** (typed nullable FKs—no polymorphic pointers—formula version, source-policy version, dependency hash, confidence, freshness, `as_of_at`, supersession). See [data-model.md](data-model.md).

Properties:

- Each stage is a **pure function** of its inputs + `formula_version` + clock-free config.
- Wall clock is not read inside domain functions; `as_of_day`, `as_of_at`, and sample timestamps are inputs.
- Models, HTTP, and DB I/O are forbidden inside domain stages.
- Insufficient critical data → status `insufficient` (score null). **Never** invent a neutral midpoint score.
- Weighted means use **present components only**; expose `available_weight`; renormalization **must disclose coverage**.
- Alternatives in `source_selection_candidates` are **never averaged** and **never auto-fallback** into features.

---

## Stage 1: Daily features

Inputs: current `source_selections` with non-null `selected_fact_record_id` + those `fact_records` for `tenant_id` + `local_health_day` (+ prior days for multi-day features). Selections with `selection_reason = missing_authoritative` contribute no fact for that grain.

### Feature codes (MVP; recovery path)

| Code | Definition | Critical for recovery v0.1.0? |
|------|------------|-------------------------------|
| `sleep_duration_min` | Total sleep minutes across sessions assigned by wake-date (naps/split allowed) | Yes (sleep group) |
| `sleep_efficiency_pct` | Sleep / time in bed × 100 if available | Component |
| `sleep_consistency` | Mean resultant length score over principal-sleep midpoints (see Sleep) | Component |
| `sleep_target_adherence` | Bounded adherence vs explicit sleep target | Component |
| `sleep_debt_daily_min` | Daily shortfall vs target | Supporting |
| `sleep_debt_14d_min` | Rolling 14-calendar-day accumulated debt (see Sleep) | Supporting |
| `resting_hr_bpm` | Overnight RHR authoritative (Oura) | Yes (with HRV gate) |
| `hrv_ms` | Overnight HRV (RMSSD preferred when policy says so) | Yes (with RHR gate) |
| `temperature_dev` | Overnight temperature vs baseline input series | Component |
| `respiratory_rate` | Overnight respiration | Component |
| `steps_count` | Daytime steps (google-wearables); **INTEGER** | Activity scores |
| `active_minutes` | Moderate+ activity minutes | Activity scores |
| `zone_load` / `daily_strain_load` | Internally calculated daily load from Polar zones | Load path |
| `acute_load` | 7-day sum of daily load (strict coverage) | Load path |
| `chronic_weekly_load` | 28-day sum / 4 (strict coverage) | Load path |
| `acwr` | acute / chronic_weekly (descriptive only) | No |
| `monotony` | 7/7 monotony (see Load) | No |
| `prior_load_balance` | Recovery-balance feature from prior load vs capacity | Component |
| `subjective_modifier` | From **completed** check-in only (see Scores) | Component |
| `spo2_day_pct` | Daytime SpO2 if present (`oxygen_saturation_samples`) | No |

Missing non-critical features reduce confidence and omit related factors. Recovery v0.1.0 critical gate is defined under Scores.

---

## Stage 2: Baselines

### Window and maturity (`general_recovery_v0.1.0`)

For feature \(x\) over prior **calendar** days ending at day \(D-1\) (exclude current day from baseline center):

| Parameter | Value |
|-----------|-------|
| Window length | **42** calendar days prior |
| Minimum samples | **14** present, quality-eligible points |
| Mature samples | **28** present points → maturity `mature`; between 14 and 27 → `min`; below 14 → baseline `insufficient` |

### Statistics stored

- `center = median(S)`
- `mad = median(|s - center|)` (unscaled MAD)
- `robust_scale` = σ-equivalent scale used for z (below)
- Percentiles `p25`, `p75` (IQR = p75 − p25)
- Optional `ewma` for trend displays (scoring path below uses median/`robust_scale` primary path)

### Dispersion → `robust_scale` (exact)

Compute in order; stop at first usable scale:

1. If `mad` is non-null and `mad > 0`:

   \(\mathrm{robust\_scale} = 1.4826 \times \mathrm{mad}\)

2. Else if IQR is non-null and IQR > 0:

   \(\mathrm{robust\_scale} = \mathrm{IQR} / 1.349\); set `fallback_dispersion_used = 1`

3. Else use **metric floor** as `robust_scale`; set `fallback_dispersion_used = 1`:

| Feature family | Floor (`robust_scale` minimum) |
|----------------|--------------------------------|
| HRV (ms) | 1.0 |
| RHR (bpm) | 0.5 |
| Sleep duration (min) | 5.0 |
| Temperature (°C) | 0.05 |
| Respiratory (breaths/min) | 0.2 |
| Steps / activity counts | 100.0 |
| Other / unspecified in v0.1.0 | 1.0 |

**No imputation** of missing days; skip missing; if count &lt; 14, baseline `insufficient` (`robust_scale` may be null; component omitted).

### z-score (exact)

\[
z = \frac{x - \mathrm{center}}{\mathrm{robust\_scale}}
\]

Clamp \(z\) to \([-3, 3]\) before directed mapping when used in recovery components.

### Trend EWMA (exact for v0.1.0 displays / multi-day features that use it)

| Parameter | Value |
|-----------|-------|
| α | **0.25** |
| Initialization | First present sample: \(\mathrm{ewma}_0 = x_0\); no synthetic prior |
| Missing day | **Skip** that day (do not update EWMA; do not treat missing as zero) |
| Update when present | \(\mathrm{ewma}_t = 0.25 \cdot x_t + 0.75 \cdot \mathrm{ewma}_{t-1}\) |

### Stratification and reset

Baselines are stratified / reset when any of these change for the series:

| Key | Examples |
|-----|----------|
| Measurement method | wearable vs user_entered |
| HRV statistic / window | RMSSD vs SDNN; window_seconds |
| Source-policy generation | policy mapping change |
| Material device change | new device_id / DataSource family material to the metric |

Context field `context_code` encodes the stratification tuple. MVP implements base context plus required reset keys; richer contexts are **sample-gated future** (not required for v0.1.0):

| Future context (sample-gated) | Notes |
|-------------------------------|-------|
| Weekday vs weekend | Need sufficient per-bucket n |
| Travel | When travel detection exists |
| Training phase | When phase labels exist |
| Season | |
| Menstrual phase | Opt-in only |
| Illness | When symptom flags present |

Do not activate a bucket until min sample counts are met; otherwise fall back to base context with disclosed lower specificity—not silent mix.

---

## Stage 3: Scores and factors

### Score codes

`daily_health_scores.score_code` is required. MVP codes reserved:

| `score_code` | Role | Ship status |
|--------------|------|-------------|
| `recovery` | Primary general recovery composite | **Executable** under `general_recovery_v0.1.0` (unvalidated) |
| `sleep` | Sleep-focused composite | **Blocked** until formula + golden fixtures accepted |
| `strain` | Load/strain-focused | **Blocked** until formula + golden fixtures accepted |
| `activity` | Daytime activity | **Blocked** until formula + golden fixtures accepted |
| `readiness` | Optional readiness-style view | **Blocked** until formula + golden fixtures accepted |

### Recovery v0.1.0 component weights (exact)

Identity: **`formula_version = general_recovery_v0.1.0`**. Weights sum to **1.00** when all present:

| Component | Weight | Directed mapping (higher \(c\) = better recovery contribution) |
|-----------|--------|------------------------------------------------------------------|
| HRV (`hrv_ms`) | **0.25** | \(z_{\mathrm{dir}} = +z\) |
| Overnight RHR (`resting_hr_bpm`) | **0.15** | \(z_{\mathrm{dir}} = -z\) |
| Sleep-target adherence | **0.20** | Direct 0–100 adherence (below); already better-is-higher |
| Sleep efficiency | **0.05** | Baseline: \(z_{\mathrm{dir}} = +z\) on efficiency % |
| Sleep consistency | **0.05** | Direct 0–100 from consistency feature (below) |
| Temperature | **0.10** | \(z_{\mathrm{dir}} = -|z|\) |
| Respiratory | **0.05** | \(z_{\mathrm{dir}} = -\max(z, 0)\) (elevated worse; low not rewarded) |
| Prior-load balance | **0.10** | Direct 0–100 from load mapping (below); **omit** if ACWR undefined |
| Subjective energy/symptom modifier | **0.05** | Direct 0–100 from completed check-in only (below) |

### Baseline component score mapping (exact)

For components that use baselines: compute \(z\), apply direction to get \(z_{\mathrm{dir}}\), clamp \(z_{\mathrm{dir}}\) to \([-3, 3]\), then:

\[
c = \mathrm{clamp}\big(50 + 50 \cdot \tanh(z_{\mathrm{dir}} / 2),\; 0,\; 100\big)
\]

**Baseline-dependent components are omitted** (not present; weight not in \(W\)) when their baseline maturity is `insufficient` or baseline is missing. They do **not** invent a midpoint \(c\).

### Bounded sleep-target adherence (exact)

Let `target = user_preferences.sleep_target_min` if set, else provisional default **480** minutes. Default is **explicitly provisional**, never a chronically short personal median.

\[
\mathrm{shortfall} = \max(0,\; \mathrm{target} - \mathrm{sleep\_duration\_min})
\]

\[
\mathrm{adherence} = \mathrm{clamp}\big(100 \cdot (1 - \mathrm{shortfall} / \mathrm{target}),\; 0,\; 100\big)
\]

Oversleep does not increase adherence above 100 in v0.1.0 (no bonus); surplus handling belongs to debt credit caps, not adherence. Adherence is already on 0–100; use as \(c\) directly.

### Prior-load balance mapping (exact for v0.1.0)

Uses descriptive ACWR only when ACWR is defined (strict 7/7 and 28/28 coverage—see Load). **If ACWR is undefined, omit this component** (not present; do not invent 50).

Descriptive band centers on **1.0** (acute ≈ chronic weekly equivalent):

| ACWR \(a\) | Component score \(c\) |
|------------|------------------------|
| \(a &lt; 0.8\) | \(c = 100 \cdot (a / 0.8)\) clamped to \([0, 100]\) (under-load pulls down toward 0 as \(a \to 0\)) |
| \(0.8 \le a \le 1.3\) | \(c = 100\) (explicit **descriptive balance band**) |
| \(a &gt; 1.3\) | \(c = \mathrm{clamp}\big(100 \cdot (1 - (a - 1.3) / 0.7),\; 0,\; 100\big)\) so \(c = 0\) at \(a \ge 2.0\) |

Absent/zero-denominator behavior:

- Chronic weekly equivalent is **0** and acute is **0** with full 7/7 and 28/28 **known** rest → treat as balanced: \(c = 100\), ACWR may be stored as null with reason `all_zero_rest` or as 1.0 with the same balance score—**pin storage as ACWR null + `prior_load_balance = 100`** with flag `all_zero_rest`.
- Chronic weekly equivalent is **0** and acute **&gt; 0** → ACWR **undefined**; **omit** prior-load component.
- Any required day unknown → ACWR undefined; **omit** prior-load component.

### Subjective modifier (exact)

Use **only** an explicit **completed** `subjective_check_ins` row for that local health day (`completed_at` non-null). **No inference** that absence of a check-in or blank symptom fields means no symptoms.

Normalized scales (after normalizer; each on \([0, 1]\)):

- `energy_n` higher = better
- `stress_n` higher = worse
- `symptom_burden_n` higher = worse (from completed check-in symptom fields only)
  - **`symptom_burden_n = 0` is allowed only when the completed check-in explicitly records no symptoms** (e.g. confirmed empty list / explicit none flag).
  - **Blank / unanswered symptom fields are missing**—do **not** infer absence; **omit the entire subjective component**.
  - Missing check-in ⇒ omit subjective component (not present). Missing check-in ≠ “no symptoms” and ≠ neutral 50.

\[
c_{\mathrm{subj}} = \mathrm{clamp}\big(
  100 \cdot \big(0.5 \cdot \mathrm{energy\_n} + 0.25 \cdot (1 - \mathrm{stress\_n}) + 0.25 \cdot (1 - \mathrm{symptom\_burden\_n})\big),
\; 0,\; 100\big)
\]

Compute \(c_{\mathrm{subj}}\) only when energy, stress, and symptom burden are all present under the rules above; otherwise **omit** subjective from \(W\).

### Weighted mean and coverage

Let \(W\) be the set of **present** components with weights \(w_i\) and component scores \(c_i\).

\[
\mathrm{available\_weight} = \sum_{i \in W} w_i
\]

\[
\mathrm{score} = \mathrm{round}\Big(\sum_{i \in W} \frac{w_i}{\mathrm{available\_weight}} \cdot c_i\Big)
\quad\text{only if gates pass}
\]

**Renormalization** over present weights is allowed **only with disclosed coverage** (`available_weight`, present factor flags, UI copy keys). Never hide that optional components were missing.

### Recovery v0.1.0 sufficiency gate

Require **all** of:

1. Sleep component group present (at least sleep-target adherence input = authoritative sleep duration).
2. **HRV or overnight RHR** present (at least one).
3. \(\mathrm{available\_weight} \ge 0.60\).

Else `status = insufficient`, `score = null`.

If gates pass but confidence low or some non-critical components missing: `status = partial` with numeric score and disclosed coverage.

### Confidence (exact relative to `as_of_at`)

Evaluation inputs include explicit **`as_of_at`** (UTC RFC3339). Freshness is **not** relative to end-of-current-local-day alone.

For each critical input, let \(h\) be hours between that input’s freshness timestamp (`freshness_at` / last confirmation) and `as_of_at` (non-negative; if future, treat as 0).

**Piecewise freshness** for one input:

\[
c_{\mathrm{fresh,one}}(h) =
\begin{cases}
1 & 0 \le h \le 24 \\
1 - 0.5 \cdot \dfrac{h - 24}{48} & 24 &lt; h \le 72 \\
0.5 \cdot \dfrac{168 - h}{96} & 72 &lt; h \le 168 \\
0 & h &gt; 168
\end{cases}
\]

(Linear from 1 at 24h to **0.5** at 72h; linear from 0.5 at 72h to **0** at 168h.)

\[
c_{\mathrm{freshness}} = \min_{i \in \mathrm{critical\_present}} c_{\mathrm{fresh,one}}(h_i)
\]

Critical inputs for recovery v0.1.0 freshness min: present members of {sleep duration, HRV if present, RHR if present}.

\[
\mathrm{confidence} = c_{\mathrm{coverage}} \cdot c_{\mathrm{freshness}} \cdot c_{\mathrm{quality}} \cdot c_{\mathrm{baseline\_maturity}}
\]

| Factor | Definition |
|--------|------------|
| `c_coverage` | `available_weight` (0–1 scale of full weight set 1.00) |
| `c_freshness` | Minimum piecewise freshness across critical present inputs vs `as_of_at` |
| `c_quality` | Mean quality weight of present critical inputs (`high=1`, `medium=0.75`, `low=0.5`, `unknown=0.5`) |
| `c_baseline_maturity` | **Weighted** combination over **present baseline-dependent components only**: map each to `min→0.85`, `mature→1.0`; weight by that component’s score weight; if no baseline-dependent component is present, use **1.0**. Baseline-insufficient components are already omitted from \(W\) and do not contribute 0 via this factor alone. |

**Low confidence is partial**, not a fake `ok`. Prefer `partial` with score when gates pass; `insufficient` when gates fail. When \(c_{\mathrm{freshness}} = 0\), suppress anomalies and cap confidence at 0 for anomaly/recommendation freshness gates.

### Signed factors

Each contributor emits `factor_code`, `sign`, `magnitude`, `weight`, `present`, `display_label_key`, linked to the score's derivation run.

---

## Sleep: target, debt, timing, stages

### Sleep target

- Explicit **user preference** `sleep_target_min`.
- Provisional default **480** minutes until set.
- **Never** use a chronically short personal median as the target.
- Personal **median sleep duration** is retained for **comparison and Why UI only**.

### Daily shortfall and rolling 14-calendar-day debt (exact)

Let `target` as above. For each local health day \(d\):

\[
\mathrm{shortfall}_d = \max(0,\; \mathrm{target} - \mathrm{sleep\_duration\_min}_d)
\quad\text{if duration known; else day is unknown}
\]

\[
\mathrm{surplus}_d = \max(0,\; \mathrm{sleep\_duration\_min}_d - \mathrm{target})
\quad\text{if duration known}
\]

**Daily surplus credit cap** = **60** minutes.

**Total debt cap** = \(14 \times \mathrm{target}\).

Rolling window: the **current local health day and the previous 13 calendar days** (14 days total). Debt is **not** an indefinite recurrence from account creation.

Algorithm for `sleep_debt_14d_min` on day \(D\):

1. Let days \(D-13, \ldots, D\) be the window.
2. Initialize `debt = 0`.
3. For each day \(d\) in chronological order:
   - If sleep duration **unknown**: skip update (do not impute 0 shortfall or 0 surplus); mark window **partial**.
   - If known:

     `credit = min(surplus_d, 60)`

     `debt = clamp(debt + shortfall_d - credit, 0, 14 * target)`
4. Persist:
   - `sleep_debt_14d_min` = final `debt` as a **disclosed lower bound** when any day unknown (actual debt could be higher if unknown days had shortfall).
   - Coverage: `known_days` out of 14; status `complete` if 14/14 known, else `partial`.
5. **New users** (series shorter than 14 calendar days since first known sleep day): compute over available days only with the same caps; mark `partial`; `known_days` counts known days in the truncated window; do not invent pre-history zeros.

**Debt recommendation gate:** emit debt-related recommendations only if `known_days >= 12` within the 14-day window (truncated windows for new users use the same absolute bar: need ≥12 known days in the considered window). Otherwise withhold debt recommendation (may still show partial lower-bound debt as informational feature).

Median sleep is **not** used to shrink debt.

### Timing and consistency (exact)

- Principal sleep session per local health day: the non-nap session with longest duration; if only naps, skip that night for consistency.
- Sleep **midpoint** on circle \([0, 1440)\) minutes local time: midpoint of principal session start–end.
- Window: **current local health day + previous 13 calendar days** (14 days).
- Valid night: principal midpoint known.
- **Minimum 7 valid nights** in the window; else `sleep_consistency` feature is omitted / insufficient for component use.
- Mean resultant length \(R\): unit vectors at angles \(\theta_i = 2\pi \cdot \mathrm{midpoint}_i / 1440\).

  \(R = \sqrt{\bar{x}^2 + \bar{y}^2}\) where \(\bar{x} = \mathrm{mean}(\cos \theta_i)\), \(\bar{y} = \mathrm{mean}(\sin \theta_i)\).
- Consistency score: \(\mathrm{sleep\_consistency} = 100 \cdot R\) (range 0–100). Use as component \(c\) directly.

### Efficiency and stages

- Efficiency: `sleep_duration / time_in_bed` × 100 when both known.
- Fragmentation: awakenings / stage transitions when available; soft negative for future sleep score only—not in recovery v0.1.0 weights beyond efficiency/adherence/consistency.
- Stages: light/deep/rem minutes as supporting features.
- Multiple sessions: sum into daily sleep duration; sessions remain individual facts; selections use `session` granularity with distinct provider-independent `source_grains` / `grain_key`s (never vendor session ids).

---

## Load, strain, ACWR, swimming (`general_recovery_v0.1.0` load path)

### Canonical load (always internal)

**Canonical load is always calculated internally** from **Polar HR-zone durations** under **versioned individualized zone boundaries**. Vendor-provided load/training load fields are **comparison only**, never the engine's ACWR/strain authority.

\[
\mathrm{session\_load} = \sum_{z} \mathrm{minutes}_z \cdot \mathrm{weight}_z
\]

Default weights (unvalidated): Z1=1, Z2=2, Z3=3, Z4=4, Z5=5. Individualized boundaries live in formula/config version.

**Exclude** overlapping **Google Health / Fitbit-origin** workout samples from load when a Polar workout covers the interval (`exclude_from_load=1`).

### Daily strain-load

Sum of included session loads for the local health day.

| Day state | Daily load |
|-----------|------------|
| Confirmed complete rest with Polar coverage | **0** |
| Unknown / incomplete Polar coverage | **missing** (never treat as zero) |
| Workouts present | Sum of included session loads |

### Acute and chronic (ACWR)

| Quantity | Definition |
|----------|------------|
| **Acute load** | Sum of daily strain-load over last **7** local days |
| **Chronic weekly equivalent** | (Sum of daily strain-load over last **28** local days) **/ 4** |
| **ACWR** | \(\mathrm{acute\_load} / \mathrm{chronic\_weekly\_equivalent}\) when defined |

**v0.1.0 coverage (strict):**

- ACWR requires **7/7** known daily-load days in the acute window **and** **28/28** known daily-load days in the chronic window.
- **Unknown is never zero.** Confirmed rest is zero.
- If any required day is unknown → ACWR **undefined**; omit `acwr` feature and omit prior-load balance component.

ACWR is **descriptive only**—**never injury prediction** or causation. UI copy keys must not say "injury risk."

### Monotony (exact)

Requires **7/7** known daily loads in the last 7 local days; else omit monotony.

Let \(\mu\) = population mean of the 7 daily loads; \(\sigma\) = **population** standard deviation (divide by \(n=7\), not \(n-1\)).

| Case | Monotony value |
|------|----------------|
| All seven days load = 0 | **0** |
| All seven days equal and **&gt; 0** | **10** (capped) with flag `equal_positive_days` |
| Otherwise | \(\min(\mu / \sigma,\; 10)\) when \(\sigma &gt; 0\) |

High monotony is a soft descriptive factor only; unvalidated; **not** injury prediction.

### Swimming

Swim sessions link to parent workouts (`workout_fact_record_id`). Lengths contribute when Polar swim validation passes. Intensity distribution remains descriptive.

### Non-recovery scores

Separate `score_code` formulas **must not be implemented or shipped** until formula specs and golden fixtures are accepted. Unspecified weights are **not** implementable. They will share derivation infrastructure and no-imputation / disclosure rules when specified.

---

## Stage 4: Anomalies (non-diagnostic)

Anomalies are deterministic, versioned, **non-diagnostic** wellness flags. Suppress when inputs are **stale** (\(c_{\mathrm{freshness}} = 0\)) or **low quality**.

### Enabled v0.1.0 detectors (exact)

| Detector | Open condition | Notes |
|----------|----------------|-------|
| Low HRV | \(z_{\mathrm{hrv}} \le -2.5\) | \(z = (x-\mathrm{center})/\mathrm{robust\_scale}\) |
| Elevated RHR | \(z_{\mathrm{rhr}} \ge 2.5\) | |
| Elevated/deviant temperature | \(\|z_{\mathrm{temp}}\| \ge 2.5\) | absolute |
| Elevated respiration | \(z_{\mathrm{resp}} \ge 2.5\) | |
| Low activity | \(z_{\mathrm{activity}} \le -2.5\) | steps or active minutes series as selected |
| Short sleep | shortfall vs target \(\ge 120\) min **or** \(z_{\mathrm{sleep\_duration}} \le -2.5\) | either condition opens |

### Clear threshold (direction-aware)

Open when open condition holds. **Clear** when the metric has returned to \(|z_{\mathrm{dir\_open}}| &lt; 1.5\) (using the same directed sense as the open rule: e.g. HRV clear when \(z &gt; -1.5\); RHR clear when \(z &lt; 1.5\); temperature clear when \(|z| &lt; 1.5\); resp clear when \(z &lt; 1.5\); activity clear when \(z &gt; -1.5\); short sleep clear when shortfall &lt; 120 **and** \(z &gt; -1.5\)) for **2 consecutive** local health days.

Severity: `moderate` if open threshold to 3.0 exclusive of clamp region; `high` if at clamp \(|z|=3\).

### Deferred detectors (not blank—explicitly deferred)

| Detector | Status |
|----------|--------|
| Abnormal workout HR at comparable effort | **Deferred** until comparator / similar-session spec exists |
| Multi-day recovery decline | **Deferred** until multi-day trend detector spec exists |
| Sudden change (large day-over-day jump) | **Deferred** until sudden-change comparator spec exists |

Do not implement deferred detectors in v0.1.0; do not leave empty normative thresholds.

---

## Stage 5: Recommendations and training label

### Nature

Wellness and performance guidance **only**. Not diagnosis, not treatment, not medical advice. **No injury prediction.**

### Structure

- Choose **one global primary** recommendation (`role=primary`).
- Attach **supporting** detail recommendations (`role=supporting`).
- Conflict resolution: priority desc, rule_id asc within group; suppress losers with `suppressed_by`.
- **Remove** contradictory "maintain consistency" wording that fights load-ease or rest guidance. Monotony may support a **variety** or **deload** supporting tip only when not conflicting with a higher-priority rest/ease primary.
- **Missing data must not cause a rest recommendation.** Missing/incomplete data → `insufficient` training label and/or `data_gap_reconnect` guidance—not rest.

### Normative v0.1.0 rules (not illustrative)

Exact predicates (not illustrative ranges):

| Rule id | When (all conditions required) | Conflict group | Typical role |
|---------|--------------------------------|----------------|--------------|
| `sleep_extend_window` | `sleep_debt_14d_min` present; `known_days >= 12`; debt \(\ge 120\) minutes; sleep-target adherence \(\lt 90\) | `sleep` | primary or supporting |
| `load_ease` | ACWR defined; \(a &gt; 1.3\); \(c_{\mathrm{hrv}} &lt; 40\) | `load` | primary |
| `rest_day` | Training label path selects `rest` (score/symptom rules below)—**not** for missing data | `load` | primary |
| `data_gap_reconnect` | `missing_authoritative` or recovery `insufficient` due to data gaps | `data` | primary if no health primary; else supporting |
| `symptom_downshift` | completed check-in with **high symptom burden** (\(\mathrm{symptom\_burden\_n} \ge 0.75\)) **or** explicit severe symptom flag | `health_modifier` | modifies training label |

### Deterministic training recommendation (`ruleset_version` pinned with formula)

Label ∈ {`hard`, `moderate`, `light`, `rest`, `insufficient`}.

**Base label from recovery score bands** (when recovery is numeric and not insufficient by gates):

| Label | Exact conditions |
|-------|------------------|
| `insufficient` | Recovery score is **null** **or** `status = insufficient` **or** `confidence &lt; 0.4` |
| `rest` | Recovery `score &lt; 40` **or** explicit **severe symptom flag** on a completed check-in—**not** because data is missing, and **not** solely from a high-severity anomaly |
| `light` | \(40 \le \mathrm{score} \le 54\), and not rest/insufficient |
| `moderate` | \(55 \le \mathrm{score} \le 74\), and not rest/insufficient |
| `hard` | \(\mathrm{score} \ge 75\) **and** `status = ok` **and** `confidence \ge 0.7` **and** no active high-severity anomaly **and** no high symptom burden (\(\mathrm{symptom\_burden\_n} \ge 0.75\)) / severe flag **and** not blocked by ACWR red band (ACWR defined and \(a &gt; 1.3\) blocks hard) |

### Deterministic downshift behavior (single outcome path)

Apply after the base label from score bands. Steps are ordered; each can only lower the label (never raise). **Missing data never produces `rest`.**

1. If base is already `insufficient` or `rest`, stop (no further downshift needed for anomaly/symptom paths that would only lower further into rest via the rest predicate above).
2. If any active **high**-severity anomaly → set label to **at most** `light` (high-severity anomaly alone does **not** produce `rest`).
3. If completed check-in has **high symptom burden** (\(\mathrm{symptom\_burden\_n} \ge 0.75\)) or explicit severe flag → set label to **at most** `light`; if the **rest** predicate also holds (score &lt; 40 or severe flag that the ruleset treats as rest) → `rest`.
4. If base would be `hard` but `confidence &lt; 0.7` or `status != ok` → set to **at most** `moderate` (or lower if prior steps already did).
5. If ACWR defined and \(a &gt; 1.3\) and current label is `hard` → set to **at most** `moderate`; if also \(c_{\mathrm{hrv}} &lt; 40\), set to **at most** `light` (and emit `load_ease` when that rule’s full predicate holds).
6. **Never** downshift into `rest` solely from missing data or solely from high-severity anomaly; missing data yields `insufficient` and reconnect guidance.

**Unambiguous high-anomaly outcome:** active high-severity anomaly ⇒ final label ∈ {`light`, `rest`, `insufficient`} only if other predicates force rest/insufficient; **default floor from anomaly alone is `light`.**

Persist `ruleset_version` on the recommendation row.

---

## Insufficient handling

| Situation | Result |
|-----------|--------|
| Recovery gates fail | `status=insufficient`, score null; training `insufficient`; reconnect guidance if gap-driven |
| Policy source missing, alternatives exist | Still `missing_authoritative` for that grain; alternatives visible in Why via candidates; not silent fallback |
| Partial optional streams | `partial` with lower confidence and disclosed `available_weight` |
| Stale critical inputs | Cap confidence via freshness; suppress anomalies; may partial |
| Engine bug / unknown feature | Fail job; do not write fabricated score |

---

## Formula governance

| Topic | Rule |
|-------|------|
| Identity | `formula_version` string **`general_recovery_v0.1.0`** for this recovery spec |
| Immutability | Published versions never change semantics |
| Change process | New version + golden tests + recompute jobs |
| Pinning | Every derivation/score stores version + dependency hash + policy version + `as_of_at` |
| Rollback | Recompute with prior version; new version rows, no silent in-place rewrite |
| Validation status | All v0.1.0 formulas **unvalidated**; docs and UI must not claim clinical validation |
| Schema vs formula | Alembic for tables; formula changes do not require dropping score tables |
| Non-recovery scores | No ship until accepted formula + golden fixtures |

---

## What models must not do

- Invent scores, factors, or baselines
- Override formula outputs
- Average provider conflicts
- Present ACWR as injury prediction
- Infer “no symptoms” from missing check-in rows or blank symptom fields

Models may only **narrate** structured engine outputs the user already has rights to see. See [api-tools-and-agent.md](api-tools-and-agent.md).

---

## Related

- [data-model.md](data-model.md)
- [ingestion-and-sync.md](ingestion-and-sync.md)
- [../testing.md](../testing.md) (property and golden formula tests)
- [../adr/0002-deterministic-core.md](../adr/0002-deterministic-core.md)
