"""Deterministic recommendation selection (``training_label_v0.1.0`` ruleset).

Pure: no I/O, no clock. These are the exact v0.1.0 rules from health-engine.md.

Guidance is wellness/performance only — never diagnosis, treatment, or injury
prediction. The engine evaluates each rule's predicate, then resolves conflicts:
one global **primary** recommendation, the rest **supporting** or suppressed.

Conflict resolution (health-engine.md): within a conflict group, the highest
priority wins and lower-priority rules in that group are suppressed (recorded
with ``suppressed_by``). Across groups, the single highest-priority candidate
becomes the primary. Ties break by ``rule_id`` ascending.

Two invariants the design stresses:
- **Missing data must not cause a rest recommendation.** A data gap yields
  ``data_gap_reconnect``, not rest.
- There is **exactly zero or one** primary recommendation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

RULESET_VERSION = "training_label_v0.1.0"

# Component-score / debt thresholds the rule predicates use.
_DEBT_MIN_MINUTES = 120.0
_DEBT_MIN_KNOWN_DAYS = 12
_ADHERENCE_MAX = 90.0
_ACWR_RED = 1.3
_HRV_COMPONENT_LOW = 40.0


class RuleId(StrEnum):
    """The v0.1.0 recommendation rules."""

    SLEEP_EXTEND_WINDOW = "sleep_extend_window"
    LOAD_EASE = "load_ease"
    REST_DAY = "rest_day"
    DATA_GAP_RECONNECT = "data_gap_reconnect"


class ConflictGroup(StrEnum):
    """Conflict groups; one winner per group, then one global primary."""

    SLEEP = "sleep"
    LOAD = "load"
    DATA = "data"


# Priority (desc) and group for each rule. Rest/load guidance outranks sleep,
# which outranks the data-gap fallback (`data_gap_reconnect` is primary only
# when no health rule fired).
_RULE_META: dict[RuleId, tuple[int, ConflictGroup]] = {
    RuleId.REST_DAY: (100, ConflictGroup.LOAD),
    RuleId.LOAD_EASE: (90, ConflictGroup.LOAD),
    RuleId.SLEEP_EXTEND_WINDOW: (80, ConflictGroup.SLEEP),
    RuleId.DATA_GAP_RECONNECT: (10, ConflictGroup.DATA),
}


class Role(StrEnum):
    """A recommendation's role in the final set."""

    PRIMARY = "primary"
    SUPPORTING = "supporting"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True, slots=True)
class RecommendationInputs:
    """Everything the recommendation predicates read."""

    sleep_debt_min: float | None
    debt_known_days: int
    sleep_adherence_pct: float | None
    acwr: float | None
    hrv_component_c: float | None
    training_label_is_rest: bool
    has_data_gap: bool


@dataclass(frozen=True, slots=True)
class Recommendation:
    """One selected recommendation with its resolved role."""

    rule_id: RuleId
    role: Role
    priority: int
    conflict_group: ConflictGroup
    suppressed_by: RuleId | None = None


@dataclass(frozen=True, slots=True)
class RecommendationSet:
    """The resolved recommendations: at most one primary."""

    primary: Recommendation | None
    supporting: tuple[Recommendation, ...]
    suppressed: tuple[Recommendation, ...]


def select_recommendations(inputs: RecommendationInputs) -> RecommendationSet:
    """Evaluate the rules and resolve to one primary + supporting/suppressed."""
    fired = [rule for rule in RuleId if _predicate_holds(rule, inputs)]
    if not fired:
        return RecommendationSet(primary=None, supporting=(), suppressed=())

    # Sort by priority desc, then rule_id asc — the global resolution order.
    ordered = sorted(fired, key=lambda r: (-_RULE_META[r][0], r.value))

    # Within each conflict group, only the top rule survives; the rest are
    # suppressed by that winner.
    group_winner: dict[ConflictGroup, RuleId] = {}
    suppressed: list[Recommendation] = []
    survivors: list[RuleId] = []
    for rule in ordered:
        priority, group = _RULE_META[rule]
        if group in group_winner:
            suppressed.append(
                Recommendation(
                    rule_id=rule,
                    role=Role.SUPPRESSED,
                    priority=priority,
                    conflict_group=group,
                    suppressed_by=group_winner[group],
                )
            )
            continue
        group_winner[group] = rule
        survivors.append(rule)

    # The single highest-priority survivor is the primary; the rest support it.
    primary_rule = survivors[0]
    primary = Recommendation(
        rule_id=primary_rule,
        role=Role.PRIMARY,
        priority=_RULE_META[primary_rule][0],
        conflict_group=_RULE_META[primary_rule][1],
    )
    supporting = tuple(
        Recommendation(
            rule_id=rule,
            role=Role.SUPPORTING,
            priority=_RULE_META[rule][0],
            conflict_group=_RULE_META[rule][1],
        )
        for rule in survivors[1:]
    )
    return RecommendationSet(primary=primary, supporting=supporting, suppressed=tuple(suppressed))


def _predicate_holds(rule: RuleId, inputs: RecommendationInputs) -> bool:
    """Whether a rule's exact v0.1.0 predicate fires."""
    if rule is RuleId.SLEEP_EXTEND_WINDOW:
        return (
            inputs.sleep_debt_min is not None
            and inputs.debt_known_days >= _DEBT_MIN_KNOWN_DAYS
            and inputs.sleep_debt_min >= _DEBT_MIN_MINUTES
            and inputs.sleep_adherence_pct is not None
            and inputs.sleep_adherence_pct < _ADHERENCE_MAX
        )
    if rule is RuleId.LOAD_EASE:
        return (
            inputs.acwr is not None
            and inputs.acwr > _ACWR_RED
            and inputs.hrv_component_c is not None
            and inputs.hrv_component_c < _HRV_COMPONENT_LOW
        )
    if rule is RuleId.REST_DAY:
        # Rest guidance follows the training label's rest path — never a data gap.
        return inputs.training_label_is_rest
    # DATA_GAP_RECONNECT: a data gap or gap-driven insufficient recovery.
    return inputs.has_data_gap
