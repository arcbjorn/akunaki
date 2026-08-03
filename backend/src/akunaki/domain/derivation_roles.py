"""Which fact entity types back each recovery component.

Provenance discloses the facts a score was derived *from*. A component names a
role (``hrv``, ``sleep_efficiency``, …); the fact rows behind it live in a
typed detail table identified by its ``entity_type``. This module is the pure,
inspectable mapping between the two — no I/O, no persistence.

Only components sourced from **facts** appear here. ``subjective`` comes from a
check-in, not a fact record, and ``derivation_inputs`` has no typed FK for one
yet, so it is deliberately absent rather than mapped to a wrong table: an input
row must point at the thing it actually came from.
"""

from __future__ import annotations

from akunaki.domain.activity_normalizer import ENTITY_TYPE as ACTIVITY_ENTITY_TYPE
from akunaki.domain.recovery import ComponentCode
from akunaki.domain.sleep_normalizer import ENTITY_TYPE as SLEEP_ENTITY_TYPE
from akunaki.domain.vitals_normalizer import ENTITY_TYPE as VITALS_ENTITY_TYPE
from akunaki.domain.workout_normalizer import ENTITY_TYPE as WORKOUT_ENTITY_TYPE

__all__ = ["COMPONENT_ENTITY_TYPES", "entity_type_for_component"]

# A component may read more than one entity type only if its formula genuinely
# combines them; today each maps to exactly one.
COMPONENT_ENTITY_TYPES: dict[ComponentCode, str] = {
    ComponentCode.HRV: VITALS_ENTITY_TYPE,
    ComponentCode.RESTING_HR: VITALS_ENTITY_TYPE,
    ComponentCode.TEMPERATURE: VITALS_ENTITY_TYPE,
    ComponentCode.RESPIRATORY: VITALS_ENTITY_TYPE,
    ComponentCode.SLEEP_ADHERENCE: SLEEP_ENTITY_TYPE,
    ComponentCode.SLEEP_EFFICIENCY: SLEEP_ENTITY_TYPE,
    ComponentCode.SLEEP_CONSISTENCY: SLEEP_ENTITY_TYPE,
    ComponentCode.PRIOR_LOAD_BALANCE: WORKOUT_ENTITY_TYPE,
}

# Not a recovery component, but an anomaly feature sourced from facts.
LOW_ACTIVITY_ENTITY_TYPE = ACTIVITY_ENTITY_TYPE


def entity_type_for_component(code: ComponentCode) -> str | None:
    """Return the fact entity type backing ``code``, or None when not fact-sourced."""
    return COMPONENT_ENTITY_TYPES.get(code)
