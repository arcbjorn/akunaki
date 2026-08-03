"""Tests for the component -> fact entity-type mapping.

Pure domain: the mapping decides which fact rows a component's provenance
input points at, so a wrong entry would attribute a score to facts it never
read.
"""

from __future__ import annotations

import pytest

from akunaki.domain.activity_normalizer import ENTITY_TYPE as ACTIVITY_ENTITY_TYPE
from akunaki.domain.derivation_roles import (
    COMPONENT_ENTITY_TYPES,
    entity_type_for_component,
)
from akunaki.domain.recovery import ComponentCode
from akunaki.domain.sleep_normalizer import ENTITY_TYPE as SLEEP_ENTITY_TYPE
from akunaki.domain.vitals_normalizer import ENTITY_TYPE as VITALS_ENTITY_TYPE
from akunaki.domain.workout_normalizer import ENTITY_TYPE as WORKOUT_ENTITY_TYPE


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (ComponentCode.HRV, VITALS_ENTITY_TYPE),
        (ComponentCode.RESTING_HR, VITALS_ENTITY_TYPE),
        (ComponentCode.TEMPERATURE, VITALS_ENTITY_TYPE),
        (ComponentCode.RESPIRATORY, VITALS_ENTITY_TYPE),
        (ComponentCode.SLEEP_ADHERENCE, SLEEP_ENTITY_TYPE),
        (ComponentCode.SLEEP_EFFICIENCY, SLEEP_ENTITY_TYPE),
        (ComponentCode.SLEEP_CONSISTENCY, SLEEP_ENTITY_TYPE),
        (ComponentCode.PRIOR_LOAD_BALANCE, WORKOUT_ENTITY_TYPE),
    ],
)
def test_each_fact_sourced_component_maps_to_its_entity_type(
    code: ComponentCode, expected: str
) -> None:
    assert entity_type_for_component(code) == expected


def test_subjective_is_not_fact_sourced() -> None:
    """A check-in is not a fact record, and there is no typed FK for one.

    Mapping it to any fact table would assert an input the component never
    read, so it is absent rather than approximated.
    """
    assert entity_type_for_component(ComponentCode.SUBJECTIVE) is None
    assert ComponentCode.SUBJECTIVE not in COMPONENT_ENTITY_TYPES


def test_every_component_is_decided() -> None:
    """No component may be silently unmapped.

    A new component must either declare its entity type or be explicitly
    excluded here, so adding one cannot quietly drop its provenance.
    """
    not_fact_sourced = {ComponentCode.SUBJECTIVE}
    for code in ComponentCode:
        if code in not_fact_sourced:
            assert entity_type_for_component(code) is None
        else:
            assert entity_type_for_component(code) is not None, code


def test_mapped_entity_types_are_real_normalizer_outputs() -> None:
    """Every mapped type is one a normalizer actually writes."""
    known = {
        SLEEP_ENTITY_TYPE,
        VITALS_ENTITY_TYPE,
        WORKOUT_ENTITY_TYPE,
        ACTIVITY_ENTITY_TYPE,
    }
    assert set(COMPONENT_ENTITY_TYPES.values()) <= known
